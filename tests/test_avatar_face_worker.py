"""Real isolated worker, shared capacity, cancellation, and bounded responses."""
import base64
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_avatar_face_crop import jpeg, payload
from test_image_search import ImageSearchFixture
from server import ReaderError


class AvatarFaceWorkerTests(ImageSearchFixture):
    def test_disabled_and_paused_index_do_not_disable_real_avatar_worker(self):
        for enabled, paused in ((0, 0), (1, 1)):
            with self.subTest(enabled=enabled, paused=paused):
                with closing(self.app.connect()) as connection, connection:
                    connection.execute("UPDATE is_settings SET enabled=?,paused=?", (enabled, paused))
                result = self.manager.avatar_preview([jpeg("salmon")])
                self.assertEqual((result["index"], result["face_found"]), (0, False))
                with Image.open(BytesIO(result["avatar"])) as avatar:
                    self.assertEqual((avatar.size, avatar.format), ((256, 256), "JPEG"))
                    avatar.load()
                self.assertEqual(self.manager.processes, set())
                self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)

    def test_native_face_detection_on_official_qa_sample(self):
        sample = ROOT / "work" / "face-avatar-qa" / "opencv-lena-4.13.0.jpg"
        if not sample.is_file():
            self.skipTest("Optional, non-distributed OpenCV QA face fixture is unavailable")
        stream = BytesIO()
        with Image.open(sample) as image:
            image.thumbnail((400, 600))
            image.convert("RGB").save(stream, "JPEG", quality=90)
        result = self.manager.avatar_preview([jpeg("white"), stream.getvalue()])
        self.assertEqual((result["index"], result["face_found"]), (1, True))
        self.assertNotEqual(result["avatar"], self.manager.avatar_preview([jpeg("white")])["avatar"])

    def test_avatar_waits_for_shared_image_budget_then_releases_lease(self):
        entered, finished = threading.Event(), threading.Event()
        results, errors = [], []
        def decode(*args, **options):
            entered.set()
            return {"avatar": jpeg(size=(256, 256)), "index": 0, "face_found": False}
        def run():
            try:
                results.append(self.manager.avatar_preview([jpeg()]))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        with patch.object(self.manager, "_decode_exclusive", side_effect=decode):
            with self.app.task_scheduler.slot(resource="transcoding") as acquired:
                self.assertTrue(acquired)
                thread = threading.Thread(target=run)
                thread.start()
                deadline = time.monotonic() + 3
                while not self.manager.waiting_queries and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.manager.waiting_queries, 1)
                self.assertFalse(entered.is_set())
            self.assertTrue(finished.wait(3))
            thread.join(1)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)

    def sleepy(self, *args, **options):
        process = self.original_popen([sys.executable, "-B", "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"], **options)
        self.children.append(process)
        return process

    def test_timeout_reaps_child_and_returns_decoder_capacity(self):
        self.original_popen, self.children = subprocess.Popen, []
        with patch("image_search.subprocess.Popen", side_effect=self.sleepy), patch("image_search.AVATAR_TIMEOUT_SECONDS", .15):
            with self.assertRaisesRegex(ReaderError, "超时"):
                self.manager.avatar_preview([jpeg()])
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual(self.manager.processes, set())
        self.assertFalse(self.manager.decoder_active)
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)

    def test_shutdown_kills_child_without_waiting_full_timeout(self):
        self.original_popen, self.children = subprocess.Popen, []
        errors = []
        def run():
            try:
                self.manager.avatar_preview([jpeg()])
            except BaseException as error:
                errors.append(error)
        with patch("image_search.subprocess.Popen", side_effect=self.sleepy):
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 3
            while not self.manager.processes and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.manager.processes)
            self.manager.close()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReaderError)
        self.assertEqual(self.manager.processes, set())
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)

    def test_invalid_input_rejected_before_starting_worker(self):
        for covers in (None, [], (jpeg(),), [jpeg()] * 9, [b""], ["not bytes"], [b"x" * (512 * 1024 + 1)]):
            with self.subTest(kind=type(covers).__name__), patch("image_search.subprocess.Popen") as popen:
                with self.assertRaises(ReaderError):
                    self.manager.avatar_preview(covers)
                popen.assert_not_called()

    def test_response_rejects_bad_index_flags_dimensions_format_and_payload(self):
        valid = {"ok": True, "avatar": base64.b64encode(jpeg(size=(256, 256))).decode(), "index": 0, "face_found": True}
        cases = [{**valid, "index": True}, {**valid, "index": 8}, {**valid, "face_found": 1},
                 {**valid, "avatar": "%%%"}, {**valid, "avatar": base64.b64encode(jpeg(size=(255, 256))).decode()},
                 {**valid, "extra": True}, {**valid, "ok": 1}, []]
        for value in cases:
            with self.subTest(keys=list(value)), self.assertRaises(ReaderError):
                self.manager._parse_avatar_result(json.dumps(value).encode())
        self.assertEqual(self.manager._parse_avatar_result(json.dumps(valid).encode())["index"], 0)

    def test_invalid_worker_flag_combination_never_runs_face_detector(self):
        process = subprocess.run([sys.executable, str(ROOT / "image_search.py"), "--worker", "--stdin", "--avatar-crop", "--preview"],
                                 input=payload([jpeg()]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        result = json.loads(process.stdout)
        self.assertFalse(result["ok"])
        self.assertIn("输入无效", result["error"])

    def test_importing_server_does_not_import_native_face_libraries(self):
        result = subprocess.run([sys.executable, "-c", "import server, avatar_face_crop, sys; print(int('cv2' in sys.modules), int('numpy' in sys.modules))"],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True)
        self.assertEqual(result.stdout.strip(), b"0 0")

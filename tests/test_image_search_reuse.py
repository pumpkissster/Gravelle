"""Real bounded worker reuse and cancellation, using generated source images."""
from contextlib import closing
import base64
import hashlib
import json
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from test_image_search import ImageSearchFixture
from server import ReaderError


class ImageSearchReuseTests(ImageSearchFixture):
    def prepare(self):
        self.manager.configure({"enabled": True})
        self.assertTrue(self.manager.run_once())

    def wait_request(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            session = self.manager._index_worker
            if session and session.count == 1:
                return session
            time.sleep(.01)
        self.fail("Disposable worker did not receive a request")

    def sleepy_worker(self, *args, **options):
        # A real subprocess verifies pipe polling, kill/wait and thread cleanup.
        program = "import sys,time; print('{\"ready\":true,\"protocol\":1}',flush=True); sys.stdin.readline(); time.sleep(20)"
        return self.popen([sys.executable, "-B", "-c", program], **options)

    def test_two_pages_share_native_worker_and_keep_original_digest_then_idle_reaps(self):
        self.prepare()
        self.assertTrue(self.manager.run_once())
        session = self.manager._index_worker
        self.assertIsNotNone(session)
        self.assertEqual(session.count, 1)
        self.assertIsNone(session.process.poll())
        self.assertTrue(self.manager.run_once())
        self.assertIs(self.manager._index_worker, session)
        self.assertEqual(session.count, 2)
        rows = self.rows()
        self.assertEqual([row["digest"] for row in rows], [hashlib.sha256(value).hexdigest() for value in (self.source, self.second)])
        with closing(self.app.connect()) as connection:
            generation = connection.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]
        ordinary = self.manager._decode_exclusive(source_path=self.album / "1.png", preview=True, generation=generation)
        self.assertEqual(rows[0]["local_features"], ordinary["local_features"])
        self.assertEqual(rows[0]["local_sketch"], ordinary["local_sketch"])
        self.assertEqual(rows[0]["preview"], ordinary["preview"])
        self.assertFalse(self.manager.run_once())
        self.assertIsNone(self.manager._index_worker)
        self.assertIsNotNone(session.process.poll())
        self.assertFalse(session.reader.is_alive())
        self.assertEqual(self.manager.processes, set())
        self.assertEqual((self.album / "1.png").read_bytes(), self.source)
        self.assertEqual(list(self.manager.root.glob(".index-*")), [])

    def test_foreground_query_releases_idle_batch_without_reserving_an_extra_slot(self):
        self.prepare()
        self.manager.run_once()
        session = self.manager._index_worker
        matches = self.manager.query(self.source)
        self.assertEqual(matches["results"][0]["page"], 0)
        self.assertIsNone(self.manager._index_worker)
        self.assertIsNotNone(session.process.poll())
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)
        self.manager.run_once()
        self.assertIsNot(self.manager._index_worker, session)

    def test_page_limit_and_idle_timeout_both_recycle_real_workers(self):
        self.prepare()
        with patch("image_search.INDEX_WORKER_PAGES", 1):
            self.manager.run_once()
            self.assertIsNone(self.manager._index_worker)
        with patch("image_search.INDEX_WORKER_IDLE", .05):
            self.manager.run_once()
        session = self.manager._index_worker
        self.assertIsNotNone(session)
        deadline = time.monotonic() + 3
        while self.manager._index_worker and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(self.manager._index_worker)
        self.assertTrue(session.closed.wait(3))
        self.assertIsNotNone(session.process.poll())

    def test_each_request_timeout_kills_reusable_process_and_cleans_private_input(self):
        self.prepare()
        self.popen = subprocess.Popen
        with patch("image_search.subprocess.Popen", side_effect=self.sleepy_worker), patch("image_search.TIMEOUT_SECONDS", .4):
            self.manager.run_once()
        self.assertIsNone(self.manager._index_worker)
        self.assertEqual(self.manager.processes, set())
        self.assertIn("超时", self.rows()[0]["error"])
        self.assertEqual(list(self.manager.root.glob(".index-*")), [])

    def test_pause_or_clear_while_reusable_worker_decodes_reaps_without_old_commit(self):
        self.popen = subprocess.Popen
        for action in ("pause", "clear"):
            with self.subTest(action=action):
                self.prepare()
                errors = []
                with patch("image_search.subprocess.Popen", side_effect=self.sleepy_worker):
                    def run():
                        try:
                            self.manager.run_once()
                        except BaseException as error:
                            errors.append(error)
                    thread = threading.Thread(target=run)
                    thread.start()
                    session = self.wait_request()
                    try:
                        self.manager.configure({"action": action})
                    finally:
                        thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertIsNone(self.manager._index_worker)
                self.assertIsNotNone(session.process.poll())
                self.assertFalse(session.reader.is_alive())
                self.assertFalse(any(row["status"] == "ready" for row in self.rows()))
                self.assertEqual(list(self.manager.root.glob(".index-*")), [])

    def test_failed_or_dead_batch_is_retired_and_next_page_can_recover(self):
        self.prepare()
        self.manager.run_once()
        session = self.manager._index_worker
        session.process.kill()
        session.process.wait(3)
        self.manager.run_once()
        self.assertEqual(self.manager.snapshot()["summary"]["indexed"], 2)
        self.assertIsNot(self.manager._index_worker, session)

    def test_failed_reap_keeps_process_registered_and_blocks_claiming_another_page(self):
        self.prepare()
        process = Mock()
        process.poll.return_value = None
        session = SimpleNamespace(process=process, broken=False, busy=True,
                                  close=Mock(side_effect=subprocess.TimeoutExpired("disposable-child", 5)))
        self.manager._index_worker = session
        self.manager.processes.add(process)
        before = self.rows()
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.manager._stop_index_worker()
            self.assertIs(self.manager._index_worker, session)
            self.assertIn(process, self.manager.processes)
            self.assertTrue(session.broken)
            with patch("image_search.subprocess.Popen") as launch, self.assertRaises(subprocess.TimeoutExpired):
                self.manager.run_once()
            launch.assert_not_called()
            self.assertEqual(before, self.rows())
        finally:
            process.poll.return_value = 0
            session.close.side_effect = None
            self.manager._stop_index_worker()

    def test_new_protocol_rejects_bool_integer_aliases_and_invalid_feature_metadata(self):
        self.prepare()
        self.popen = subprocess.Popen
        def bad_handshake(*args, **options):
            return self.popen([sys.executable, "-B", "-c", "print('{\"ready\":true,\"protocol\":true}',flush=True)"], **options)
        with closing(self.app.connect()) as connection:
            generation = connection.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]
        with patch("image_search.subprocess.Popen", side_effect=bad_handshake):
            error = self.error(422, self.manager._decode_exclusive, source_path=self.album / "1.png", generation=generation, reusable=True)
        self.assertIn("启动失败", str(error))
        ordinary = self.manager._decode_exclusive(source_path=self.album / "1.png", preview=True, generation=generation)
        payload = dict(ordinary)
        payload["ok"] = True
        for name in ("preview", "local_features", "local_sketch"):
            payload[name] = base64.b64encode(payload[name]).decode()
        for name, value in (("width", -1), ("height", "bad"), ("mean", []), ("mean", [0, 0, True]), ("ok", "yes")):
            with self.subTest(name=name, value=value):
                changed = {**payload, name: value}
                self.error(422, self.manager._parse_worker_result, json.dumps(changed).encode(), preview=True, strict=True)
        changed = dict(payload)
        sketch = bytearray(ordinary["local_sketch"])
        sketch[0] ^= 1
        changed["local_sketch"] = base64.b64encode(sketch).decode()
        self.error(422, self.manager._parse_worker_result, json.dumps(changed).encode(), preview=True, strict=True)

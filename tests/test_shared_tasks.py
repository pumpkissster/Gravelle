"""Disposable real-manager checks for the shared image work budget."""
from contextlib import closing
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from server import Library, ReaderError
from image_search import _features
from transcoding import DEFAULT_RULES, _lower_background_priority


ROOT = Path(__file__).resolve().parents[1]


class SharedTasksTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "shared-task-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name) / "media"
        self.data = Path(self.temp.name) / "data"
        buffer = BytesIO()
        Image.new("RGB", (64, 96), "pink").save(buffer, "PNG")
        self.source = buffer.getvalue()
        for name in ("one", "two", "three"):
            album = self.media / name
            album.mkdir(parents=True)
            for page in (1, 2):
                (album / f"{page}.png").write_bytes(self.source)
        self.app = Library(self.media, self.data, "", 0)
        self.addCleanup(self.app.close)
        self.app.register({"username": "shared-admin", "password": "disposable-shared-admin-pass"}, "test")
        self.tc = self.app.transcoding
        self.index = self.app.image_search
        self.tc.auto_start = self.index.auto_start = False
        self.app.save_library({"name": "Shared fixture", "path": str(self.media), "media_type": "photo"})
        self.wait(lambda: not self.app.scanning)
        self.ids = [book["id"] for book in self.app.listing()["books"]]
        self.tc.save_rules({"rules": {**DEFAULT_RULES, "enabled": True, "max_source_pixels": 1000,
                                      "file_size_mb": 0, "max_source_side": 0, "min_free_space_mb": 0},
                            "expected_revision": self.tc.settings()[1]})
        self.tc.actions({"ids": self.ids, "action": "transcode"})
        self.index.configure({"enabled": True})
        self.index.run_once()  # catalogue header pass, not a decoder task

    def wait(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(condition(), "Disposable image task did not reach expected state")

    def sql(self, statement):
        with closing(self.app.connect()) as connection:
            return [dict(row) for row in connection.execute(statement)]

    def spawn(self, function, *, name=None):
        errors = []
        def call():
            try:
                function()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=call, daemon=True, name=name)
        thread.start()
        return thread, errors

    def joined(self, pair):
        thread, errors = pair
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_default_budget_serializes_optimization_and_index_query_goes_first(self):
        events = []
        entered, release = threading.Event(), threading.Event()
        original = self.tc._convert
        def convert(job, rules):
            events.append("optimization")
            entered.set()
            self.assertTrue(release.wait(10))
            return original(job, rules)
        def decode(data=None, **options):
            events.append("query" if data is not None else "index")
            return _features(BytesIO(self.source), preview=options.get("preview", False))
        with patch.object(self.tc, "_convert", side_effect=convert), patch.object(self.index, "_decode_exclusive", side_effect=decode):
            optimizer = self.spawn(self.tc.run_once)
            self.assertTrue(entered.wait(5))
            indexer = self.spawn(self.index.run_once)
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["queued"] == 1)
            query = self.spawn(lambda: self.index._decode(self.source))
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["queued"] == 2)
            self.assertTrue(all(row["status"] == "pending" for row in self.sql("SELECT status FROM is_pages")))
            self.assertEqual(events, ["optimization"])
            release.set()
            for pair in (optimizer, indexer, query):
                self.joined(pair)
        self.assertEqual(events, ["optimization", "query", "index"])
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)

    def test_waiting_optimization_stays_queued_and_pause_does_not_fail(self):
        with self.app.task_scheduler.slot() as acquired:
            self.assertTrue(acquired)
            pending = self.spawn(self.tc.run_once)
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["queued"] == 1)
            self.assertTrue(all(row["status"] == "queued" for row in self.sql("SELECT status FROM tc_books")))
            self.assertTrue(all(row["status"] == "pending" for row in self.sql("SELECT status FROM tc_pages")))
            with self.assertRaises(ReaderError) as failure:
                self.tc.cleanup({})
            self.assertEqual(failure.exception.status, 409)
            self.tc.actions({"ids": self.ids, "action": "pause"})
            self.joined(pending)
        self.assertEqual({row["status"] for row in self.sql("SELECT status FROM tc_books")}, {"paused"})
        self.assertTrue(all(not row["error"] for row in self.sql("SELECT error FROM tc_pages")))
        self.assertFalse(self.tc.active_books)

    def test_clear_and_close_wake_waiting_index_without_error_or_recreated_rows(self):
        with self.app.task_scheduler.slot() as acquired:
            self.assertTrue(acquired)
            waiting = self.spawn(self.index.run_once)
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["queued"] == 1)
            self.index.configure({"action": "clear"})
            self.joined(waiting)
            self.assertEqual(self.sql("SELECT * FROM is_pages"), [])
            self.assertEqual(self.index.snapshot()["error"], "")
            self.index.configure({"enabled": True})
            self.index.run_once()
            waiting = self.spawn(self.index.run_once)
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["queued"] == 1)
            self.index.close()
            self.joined(waiting)
        self.assertTrue(all(row["status"] == "pending" for row in self.sql("SELECT status FROM is_pages")))
        self.assertEqual(self.index.snapshot()["error"], "")

    def test_three_books_parallel_but_each_book_has_only_one_active_page(self):
        self.app.task_scheduler.configure({"taskConcurrency": 3})
        entered, release = set(), threading.Event()
        duplicate = []
        lock = threading.Lock()
        original = self.tc._convert
        def convert(job, rules):
            with lock:
                if job["id"] in entered:
                    duplicate.append(job["id"])
                entered.add(job["id"])
            self.assertTrue(release.wait(10))
            result = original(job, rules)
            with lock:
                entered.discard(job["id"])
            return result
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.media.rglob("*.png")}
        with patch.object(self.tc, "_convert", side_effect=convert):
            self.tc.start()
            self.wait(lambda: len(entered) == 3)
            self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 3)
            running = self.sql("SELECT book_id,COUNT(*) AS n FROM tc_pages WHERE status='running' GROUP BY book_id")
            self.assertEqual(len(running), 3)
            self.assertTrue(all(row["n"] == 1 for row in running))
            release.set()
            self.wait(lambda: all(row["status"] == "ready" for row in self.sql("SELECT status FROM tc_books")))
            self.wait(lambda: not self.tc.active_books)
        self.assertEqual(duplicate, [])
        self.assertEqual(len(self.sql("SELECT * FROM tc_outputs")), 6)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})
        self.assertEqual(self.tc.processes, set())
        self.assertEqual(self.tc.reserved_outputs, {})

    def test_lowering_limit_finishes_active_images_then_new_work_waits(self):
        self.app.task_scheduler.configure({"taskConcurrency": 2})
        entered, release_one, release_two = [], threading.Event(), threading.Event()
        original = self.tc._convert
        def convert(job, rules):
            number = len(entered)
            entered.append(job["id"])
            if number == 0:
                self.assertTrue(release_one.wait(10))
            elif number == 1:
                self.assertTrue(release_two.wait(10))
            return original(job, rules)
        with patch.object(self.tc, "_convert", side_effect=convert):
            self.tc.start()
            self.wait(lambda: len(entered) == 2)
            self.app.task_scheduler.configure({"taskConcurrency": 1})
            release_one.set()
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["active"] == 1)
            time.sleep(.3)
            self.assertEqual(len(entered), 2)
            release_two.set()
            self.wait(lambda: all(row["status"] == "ready" for row in self.sql("SELECT status FROM tc_books")))
        self.assertEqual(len(entered), 6)

    def test_best_effort_posix_priority_failure_does_not_fail_job(self):
        with patch("transcoding.os.name", "posix"), patch("transcoding.os.nice", create=True) as nice:
            _lower_background_priority()
            nice.assert_called_once_with(10)
        with patch("transcoding.os.name", "posix"), patch("transcoding.os.nice", side_effect=OSError("unsupported"), create=True):
            _lower_background_priority()

    def test_parallel_output_installation_respects_one_shared_storage_quota(self):
        self.tc.save_rules({"rules": {"storage_limit_mb": 1}, "expected_revision": self.tc.settings()[1]})
        self.tc.actions({"ids": self.ids, "action": "transcode"})
        self.app.task_scheduler.configure({"taskConcurrency": 2})
        ready = threading.Barrier(2)
        class Finished:
            returncode = 0
            def poll(self):
                return 0
            def wait(self, **options):
                return 0
        def process(command, **options):
            destination = Path(command[command.index("--output") + 1])
            destination.write_bytes(b"synthetic bounded output".ljust(600 * 1024, b"\0"))
            ready.wait(5)
            return Finished()
        first_job, second_job = self.tc._take_job(), self.tc._take_job()
        self.assertIsNotNone(first_job)
        self.assertIsNotNone(second_job)
        with patch("transcoding.subprocess.Popen", side_effect=process):
            first = self.spawn(lambda: self.tc._process_job(*first_job))
            second = self.spawn(lambda: self.tc._process_job(*second_job))
            self.joined(first)
            self.joined(second)
        outputs = self.sql("SELECT size FROM tc_outputs")
        self.assertEqual(len(outputs), 1)
        self.assertLessEqual(sum(row["size"] for row in outputs), 1024 * 1024)
        self.assertEqual(sum(row["status"] == "paused" for row in self.sql("SELECT status FROM tc_books")), 1)
        self.assertEqual(self.tc.reserved_outputs, {})

    def test_close_kills_all_parallel_processes_and_releases_slots(self):
        self.app.task_scheduler.configure({"taskConcurrency": 2})
        processes = []
        class Running:
            returncode = None
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9
            def wait(self, **options):
                return self.returncode
        def process(*args, **options):
            value = Running()
            processes.append(value)
            return value
        with patch("transcoding.subprocess.Popen", side_effect=process):
            self.tc.start()
            self.wait(lambda: len(self.tc.processes) == 2)
            self.tc.close()
        self.assertTrue(all(value.returncode == -9 for value in processes))
        self.assertFalse(self.tc.is_alive())
        self.assertEqual(self.tc.processes, set())
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)
        self.assertTrue(all(not row["error"] for row in self.sql("SELECT error FROM tc_pages")))

    def test_obsolete_failure_does_not_override_pause_and_resume(self):
        def obsolete(job, rules):
            self.tc.actions({"ids": [job["id"]], "action": "pause"})
            self.tc.actions({"ids": [job["id"]], "action": "resume"})
            raise ReaderError("old generation failure", 422)
        with patch.object(self.tc, "_convert", side_effect=obsolete):
            self.assertTrue(self.tc.run_once())
        self.assertTrue(all(row["status"] == "queued" and not row["error"] for row in self.sql("SELECT status,error FROM tc_books")))
        self.assertTrue(all(row["status"] == "pending" and not row["error"] for row in self.sql("SELECT status,error FROM tc_pages")))

    def test_query_process_started_at_close_boundary_is_still_killed(self):
        payload = json.dumps({"ok": True, **_features(BytesIO(self.source))}).encode()
        class Process:
            returncode = 0
            killed = False
            def poll(self):
                return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -9
            def communicate(self, *args, **options):
                return payload, b""
            def wait(self, **options):
                return self.returncode
        process = Process()
        def launch(*args, **options):
            self.index.close()
            return process
        with patch("image_search.subprocess.Popen", side_effect=launch):
            with self.assertRaises(ReaderError):
                self.index._decode(self.source)
        self.assertTrue(process.killed)
        self.assertEqual(self.index.processes, set())
        self.assertEqual(self.app.task_scheduler.snapshot_debug()["active"], 0)

    def test_waiting_query_signal_is_published_only_after_priority_registration(self):
        checking, allow_check = threading.Event(), threading.Event()
        entered, release = threading.Event(), threading.Event()
        original_settings = self.index._settings
        generation = self.sql("SELECT generation FROM is_settings")[0]["generation"]
        events = []
        def settings(connection):
            if threading.current_thread().name == "slow-query" and not checking.is_set():
                checking.set()
                self.assertTrue(allow_check.wait(10))
            return original_settings(connection)
        def decode(data=None, **options):
            name = "query" if data is not None else threading.current_thread().name
            events.append(name)
            if name == "held-decoder":
                entered.set()
                self.assertTrue(release.wait(10))
            return {}
        with patch.object(self.index, "_settings", side_effect=settings), patch.object(self.index, "_decode_exclusive", side_effect=decode):
            first = self.spawn(lambda: self.index._decode(source_path=Path("unused"), generation=generation), name="held-decoder")
            self.assertTrue(entered.wait(5))
            background = self.spawn(lambda: self.index._decode(source_path=Path("unused"), generation=generation), name="background")
            self.wait(lambda: self.app.task_scheduler.snapshot_debug()["queued"] == 1)
            query = self.spawn(lambda: self.index._decode(self.source), name="slow-query")
            try:
                self.assertTrue(checking.wait(5))
                self.assertEqual(self.index.waiting_queries, 0,
                                 "SQLite cancellation check has not registered a priority waiter yet")
                allow_check.set()
                self.wait(lambda: self.index.waiting_queries == 1)
                self.assertEqual(self.app.task_scheduler.waiting_count(priority=True, resource="image-search"), 1)
            finally:
                allow_check.set()
                release.set()
            for pair in (first, background, query):
                self.joined(pair)
        self.assertEqual(events, ["held-decoder", "query", "background"])
        self.assertEqual(self.index.waiting_queries, 0)


if __name__ == "__main__":
    unittest.main()

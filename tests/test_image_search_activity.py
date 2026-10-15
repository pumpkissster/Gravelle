"""Observable, permission-safe progress for disposable image-index jobs."""
from contextlib import closing, contextmanager
import base64
import hashlib
from io import BytesIO
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_search import _features
from server import Library


class ImageSearchActivityTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "image-search-activity-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="activity-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        album = self.media / "album"
        album.mkdir(parents=True)
        for index, color in enumerate(((130, 70, 110), (60, 130, 150))):
            Image.new("RGB", (96, 144), color).save(album / f"{index}.png")
        self.library = Library(self.media, self.root / "data", "", 0)
        self.addCleanup(self.library.close)
        self.library.register({"username": "activity-admin", "password": "disposable-activity-password"}, "test")
        self.manager = self.library.image_search
        self.manager.auto_start = False
        self.library.transcoding.auto_start = False
        self.library_id = self.library.save_library({"name": "Activity fixture", "path": str(self.media), "media_type": "photo"})["library"]["id"]
        deadline = time.monotonic() + 10
        while self.library.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.library.scanning)

    def generation(self):
        with closing(self.library.connect()) as connection:
            return connection.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]

    def enable(self):
        self.manager.configure({"enabled": True})
        return self.generation()

    def activity(self):
        return self.manager.snapshot()["activity"]

    def mark_at(self, timestamp, stage, generation, **progress):
        with patch("image_search.time.time", return_value=timestamp), patch("image_search.time.monotonic", return_value=timestamp):
            self.manager._set_activity(stage, generation, **progress)
            return self.activity()

    def decode_fixture(self, data=None, *, source_path=None, preview=False, **options):
        payload = source_path.read_bytes() if source_path is not None else data
        result = _features(BytesIO(payload), preview=preview)
        result["digest"] = hashlib.sha256(payload).hexdigest()
        for name in ("preview", "local_features", "local_sketch"):
            if name in result:
                result[name] = base64.b64decode(result[name], validate=True)
        return result

    def test_activity_is_administrator_only_and_includes_live_scheduler_state(self):
        activity = self.activity()
        self.assertEqual(set(activity), {"stage", "done", "total", "startedAt", "lastProgressAt", "heartbeatAt", "workerAlive", "background"})
        self.assertEqual(activity["background"], self.library.task_scheduler.snapshot_debug())
        self.assertFalse(activity["workerAlive"])
        child = self.library.save_user({"username": "activity-child", "password": "disposable-child-password", "enabled": True,
                                        "library_ids": [self.library_id]})["user"]["id"]
        with self.library.acting_as(child):
            self.assertEqual(set(self.manager.snapshot()), {"enabled", "available", "indexing"})

    def test_activity_advances_only_for_real_progress_and_polling_does_not_reset_timestamps(self):
        generation = self.enable()
        initial = self.mark_at(1000, "reconciling", generation, done=0, total=2)
        progressed = self.mark_at(1010, "reconciling", generation, done=1, total=2)
        self.assertEqual(progressed["startedAt"], initial["startedAt"])
        self.assertGreater(progressed["lastProgressAt"], initial["lastProgressAt"])
        repeated = self.mark_at(1020, "reconciling", generation, done=1, total=2)
        self.assertEqual(repeated["lastProgressAt"], progressed["lastProgressAt"])
        self.assertEqual(repeated["startedAt"], initial["startedAt"])
        with patch("image_search.time.time", return_value=2000), patch("image_search.time.monotonic", return_value=2000):
            polled = self.activity()
        self.assertEqual(polled["lastProgressAt"], progressed["lastProgressAt"])
        self.assertEqual(polled["startedAt"], initial["startedAt"])
        changed = self.mark_at(1030, "reading", generation, done=0, total=1)
        self.assertGreater(changed["startedAt"], repeated["startedAt"])
        self.assertGreater(changed["lastProgressAt"], repeated["lastProgressAt"])

    def test_activity_new_generation_resets_stage_timing_and_rejects_stale_updates(self):
        previous_generation = self.enable()
        previous = self.mark_at(1000, "reading", previous_generation, done=0, total=1)
        self.manager.configure({"action": "pause"})
        self.manager.configure({"action": "resume"})
        generation = self.generation()
        self.assertGreater(generation, previous_generation)
        current = self.mark_at(1010, "reading", generation, done=0, total=1)
        self.assertGreater(current["startedAt"], previous["startedAt"])
        stale = self.mark_at(1020, "decoding", previous_generation, done=999, total=999)
        for field in ("stage", "done", "total", "startedAt", "lastProgressAt"):
            self.assertEqual(stale[field], current[field], field)

    def test_disabled_paused_stopped_and_worker_alive_derive_from_actual_state(self):
        self.assertEqual(self.activity()["stage"], "disabled")
        generation = self.enable()
        self.manager._set_activity("reading", generation, done=0, total=1)
        self.manager.configure({"action": "pause"})
        self.assertEqual(self.activity()["stage"], "paused")
        self.manager.configure({"action": "resume"})
        with patch.object(self.manager, "thread", Mock(is_alive=Mock(return_value=True))):
            self.assertTrue(self.activity()["workerAlive"])
        with patch.object(self.manager, "thread", Mock(is_alive=Mock(return_value=False))):
            self.assertFalse(self.activity()["workerAlive"])
        self.manager.stop_event.set()
        self.assertEqual(self.activity()["stage"], "stopped")

    def test_catalogue_reconciliation_reports_measured_page_progress(self):
        self.enable()
        observed = []
        mark = self.manager._set_activity

        def recording(stage, generation, done=0, total=0):
            mark(stage, generation, done=done, total=total)
            if stage == "reconciling":
                observed.append((done, total))

        with patch.object(self.manager, "_set_activity", side_effect=recording):
            self.assertTrue(self.manager.run_once())
        self.assertIn((0, 2), observed)
        self.assertIn((2, 2), observed)
        done = [value for value, total in observed if total == 2]
        self.assertEqual(done, sorted(done))
        self.assertEqual(self.manager.snapshot()["summary"], {"total": 2, "indexed": 0, "pending": 2, "failed": 0})

    def test_index_reports_waiting_reading_decoding_and_idle_without_inventing_worker_liveness(self):
        self.enable()
        self.manager.run_once()
        stages = []
        open_source = self.library.transcoding._open_source

        @contextmanager
        def observing_source(*args, **options):
            stages.append(self.activity()["stage"])
            with open_source(*args, **options) as value:
                yield value

        def observing_decoder(*args, **options):
            stages.append(self.activity()["stage"])
            return self.decode_fixture(*args, **options)

        errors = []

        def index():
            try:
                self.manager.run_once()
            except BaseException as error:
                errors.append(error)

        with patch.object(self.library.transcoding, "_open_source", side_effect=observing_source), \
                patch.object(self.manager, "_decode_exclusive", side_effect=observing_decoder):
            with self.library.task_scheduler.slot(resource="other-image-task") as acquired:
                self.assertTrue(acquired)
                worker = threading.Thread(target=index)
                worker.start()
                self.addCleanup(lambda: worker.join(timeout=5))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    activity = self.activity()
                    if activity["stage"] == "waiting" and activity["background"]["queued"]:
                        break
                    time.sleep(.01)
                self.assertEqual(activity["stage"], "waiting")
                self.assertEqual(activity["background"]["active"], 1)
                self.assertGreaterEqual(activity["background"]["queued"], 1)
                self.assertFalse(activity["workerAlive"], "A manual run_once thread is not the manager's background thread")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.manager.run_once()
            self.assertFalse(self.manager.run_once())
        self.assertIn("reading", stages)
        self.assertIn("decoding", stages)
        self.assertEqual(self.activity()["stage"], "idle")

    def assert_stale_sync_failure_preserves_configuration(self, action, expected_stage):
        original_generation = self.enable()
        run_once = self.manager.run_once
        state = {"attempts": 0}
        sentinel = "Current-generation diagnostic"
        private = "/library/private-album/customer-name/stale-source.png"

        def failing_old_sync(generation):
            self.assertEqual(generation, original_generation)
            self.manager._set_activity("reconciling", generation, done=0, total=2)
            self.manager.configure({"action": action})
            with closing(self.library.connect()) as connection, connection:
                connection.execute("UPDATE is_settings SET error=?", (sentinel,))
                state["new_generation"] = connection.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]
            with self.manager.activity_lock:
                state["configured_activity"] = dict(self.manager.activity)
            raise RuntimeError(private)

        def attempts():
            state["attempts"] += 1
            if state["attempts"] == 1:
                return run_once()
            # Inspect after the old exception handler, before another attempt
            # can legitimately change the current generation's progress.
            with self.manager.activity_lock:
                state["after_failure"] = dict(self.manager.activity)
            with closing(self.library.connect()) as connection:
                state["saved_error"] = connection.execute("SELECT error FROM is_settings WHERE id=1").fetchone()[0]
            state["snapshot"] = self.activity()
            self.manager.stop_event.set()
            return False

        with patch.object(self.manager, "_sync_catalogue", side_effect=failing_old_sync), \
                patch.object(self.manager, "run_once", side_effect=attempts), \
                patch.object(self.manager.wake, "wait", return_value=True), \
                self.assertLogs("sumi", level="ERROR") as logs:
            self.manager._loop()
        self.assertEqual(state["attempts"], 2)
        self.assertGreater(state["new_generation"], original_generation)
        self.assertEqual(state["saved_error"], sentinel)
        self.assertEqual(state["after_failure"], state["configured_activity"])
        self.assertEqual(state["snapshot"]["stage"], expected_stage)
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn(private, "\n".join(logs.output))

    def test_old_generation_sync_failure_does_not_overwrite_new_pause_state(self):
        self.assert_stale_sync_failure_preserves_configuration("pause", "paused")

    def test_old_generation_sync_failure_does_not_overwrite_new_clear_state(self):
        self.assert_stale_sync_failure_preserves_configuration("clear", "disabled")

    def test_loop_retries_when_failure_persistence_also_fails_without_logging_private_details(self):
        self.enable()
        private = "/library/private-album/customer-name/image.png sensitive-decoder-detail"
        retried = threading.Event()
        state = {"calls": 0, "fail_write": False, "write_failed": False}
        connect = self.library.connect

        def failing_then_retrying():
            state["calls"] += 1
            if state["calls"] == 1:
                state["fail_write"] = True
                raise RuntimeError(private)
            retried.set()
            return False

        def failing_persistence():
            if threading.current_thread() is self.manager.thread and state["fail_write"]:
                state["fail_write"] = False
                state["write_failed"] = True
                raise sqlite3.OperationalError(private)
            return connect()

        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(self.format(record))

        logger = logging.getLogger()
        handler = Capture()
        logger.addHandler(handler)
        try:
            with patch.object(self.manager, "run_once", side_effect=failing_then_retrying), \
                    patch.object(self.library, "connect", side_effect=failing_persistence):
                self.manager.start()
                try:
                    self.assertTrue(retried.wait(5), "A failed attempt to save an error must not kill the indexing thread")
                    self.assertTrue(state["write_failed"])
                    self.assertTrue(self.manager.thread.is_alive())
                finally:
                    self.manager.close()
        finally:
            logger.removeHandler(handler)
        self.assertTrue(messages, "The background failure should emit a sanitized diagnostic")
        combined = "\n".join(messages)
        self.assertIn("RuntimeError", combined)
        self.assertNotIn(private, combined)
        self.assertNotIn("customer-name", combined)
        self.assertNotIn("sensitive-decoder-detail", combined)


if __name__ == "__main__":
    unittest.main()

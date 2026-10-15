"""Cancellation and bounded connection use for disposable per-page work."""
import base64
from contextlib import closing
import json
import math
import sqlite3
import struct
import subprocess
import threading
import unittest
from unittest.mock import patch

from test_image_search import ImageSearchFixture
from image_search import MIB
from image_search_features import SKETCH_BYTES
from server import ReaderError


def worker_response():
    return json.dumps({"ok": True, "digest": "a" * 64, "dhash": "0" * 16, "phash": "0" * 16,
                       "local_features": base64.b64encode(struct.pack("<4sHHH", b"GLF1", 16, 16, 0)).decode(),
                       "local_sketch": base64.b64encode(bytes(SKETCH_BYTES)).decode()}).encode()


class PollingProcess:
    def __init__(self, expirations=3, on_timeout=None):
        self.expirations = expirations
        self.on_timeout = on_timeout
        self.returncode = None
        self.killed = False
        self.waited = False
        self.calls = []

    def poll(self):
        return self.returncode

    def communicate(self, data=None, timeout=None):
        self.calls.append((data, timeout))
        if self.killed:
            return b"", None
        if self.expirations:
            self.expirations -= 1
            if self.on_timeout:
                self.on_timeout()
            raise subprocess.TimeoutExpired("disposable-worker", timeout)
        self.returncode = 0
        return worker_response(), None

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


class ImageSearchPageIOTests(ImageSearchFixture):
    def prepare(self, payload=None):
        if payload is not None:
            (self.album / "1.png").write_bytes(payload)
            self.app._scan_library(self.app._library_config(self.library_id))
        self.manager.configure({"enabled": True})
        with closing(self.app.connect()) as connection:
            generation = connection.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]
        self.manager._sync_catalogue(generation)
        with closing(self.app.connect()) as connection:
            job = dict(connection.execute("""SELECT b.*,p.page_key,p.page_index,p.source_version
                FROM books b JOIN is_pages p ON p.book_id=b.id
                WHERE b.id=? ORDER BY p.page_index LIMIT 1""", (self.book_id,)).fetchone())
        return job, generation

    def assert_closed(self, connections):
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_multiblock_copy_reuses_one_connection_without_retaining_transaction(self):
        payload = self.source + bytes(MIB * 3 + 17)
        job, generation = self.prepare(payload)
        connect = self.app.connect
        current = self.manager._current
        connections = []
        checks = []

        def tracked_connection():
            connection = connect()
            connections.append(connection)
            return connection

        def checked_generation(value, connection=None):
            self.assertIsNotNone(connection)
            self.assertFalse(connection.in_transaction)
            checks.append(connection)
            return current(value, connection)

        def inspect_copy(**options):
            self.assertEqual(options["source_path"].read_bytes(), payload)
            return {"copied": True}

        with patch.object(self.app, "connect", side_effect=tracked_connection) as connector, \
                patch.object(self.manager, "_current", side_effect=checked_generation), \
                patch.object(self.manager, "_decode", side_effect=inspect_copy):
            self.assertEqual(self.manager._index_page(job, generation), {"copied": True})
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(len(checks), math.ceil(len(payload) / MIB))
        self.assertTrue(all(connection is connections[0] for connection in checks))
        self.assert_closed(connections)
        self.assertEqual(list(self.manager.root.glob(".index-*")), [])

    def test_pause_or_clear_commits_during_copy_and_cancels_next_block(self):
        payload = self.source + bytes(MIB * 3)
        for action in ("pause", "clear"):
            with self.subTest(action=action):
                job, generation = self.prepare(payload)
                copied = threading.Event()
                release = threading.Event()
                errors = []
                activity = self.manager._set_activity

                def copy_progress(stage, value, **options):
                    activity(stage, value, **options)
                    if stage == "reading" and options.get("done") == MIB:
                        copied.set()
                        if not release.wait(5):
                            raise RuntimeError("Concurrent account action did not complete")

                def copy_page():
                    try:
                        self.manager._index_page(job, generation)
                    except BaseException as error:
                        errors.append(error)

                with patch.object(self.manager, "_set_activity", side_effect=copy_progress), \
                        patch.object(self.manager, "_decode") as decoder:
                    thread = threading.Thread(target=copy_page)
                    thread.start()
                    try:
                        self.assertTrue(copied.wait(5))
                        self.manager.configure({"action": action})
                    finally:
                        release.set()
                        thread.join(5)
                    self.assertFalse(thread.is_alive())
                    decoder.assert_not_called()
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ReaderError)
                self.assertEqual(errors[0].status, 409)
                self.assertEqual(list(self.manager.root.glob(".index-*")), [])
                if action == "clear":
                    self.assertEqual(self.rows(), [])

    def test_copy_size_limit_still_stops_before_decode_and_cleans_temporary_file(self):
        job, generation = self.prepare(self.source + bytes(MIB * 3))
        with patch("image_search.MAX_BYTES", MIB * 2), patch.object(self.manager, "_decode") as decoder:
            self.error(422, self.manager._index_page, job, generation)
            decoder.assert_not_called()
        self.assertEqual(list(self.manager.root.glob(".index-*")), [])

    def test_background_polling_reuses_one_connection_and_reaps_success(self):
        job, generation = self.prepare()
        process = PollingProcess(expirations=5)
        connect = self.app.connect
        connections = []
        current = self.manager._current

        def tracked_connection():
            connection = connect()
            connections.append(connection)
            return connection

        def checked_generation(value, connection=None):
            self.assertIsNotNone(connection)
            self.assertFalse(connection.in_transaction)
            return current(value, connection)

        with patch("image_search.subprocess.Popen", return_value=process), \
                patch.object(self.app, "connect", side_effect=tracked_connection) as connector, \
                patch.object(self.manager, "_current", side_effect=checked_generation) as checker:
            result = self.manager._decode_exclusive(source_path=self.album / "1.png", generation=generation)
        self.assertEqual(result["digest"], "a" * 64)
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(checker.call_count, 6)
        self.assertTrue(all(call.kwargs == {} and call.args[1] is connections[0] for call in checker.call_args_list))
        self.assertEqual([timeout for _, timeout in process.calls], [.1] * 6)
        self.assert_closed(connections)
        self.assertFalse(process.killed)
        self.assertTrue(process.waited)
        self.assertEqual(self.manager.processes, set())

    def test_background_timeout_still_kills_and_reaps_process(self):
        job, generation = self.prepare()
        process = PollingProcess(expirations=100)
        connect = self.app.connect
        with patch("image_search.subprocess.Popen", return_value=process), \
                patch("image_search.time.monotonic", side_effect=[0, .01, 61]), \
                patch.object(self.app, "connect", side_effect=connect) as connector:
            error = self.error(422, self.manager._decode_exclusive,
                               source_path=self.album / "1.png", generation=generation)
        self.assertIn("超时", str(error))
        self.assertEqual(connector.call_count, 1)
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)
        self.assertEqual(self.manager.processes, set())

    def test_background_polling_observes_pause_or_clear_through_reused_connection(self):
        for action in ("pause", "clear"):
            with self.subTest(action=action):
                job, generation = self.prepare()
                process = PollingProcess(expirations=1,
                    on_timeout=lambda: self.manager.configure({"action": action}))
                with patch("image_search.subprocess.Popen", return_value=process):
                    self.error(409, self.manager._decode_exclusive,
                               source_path=self.album / "1.png", generation=generation)
                self.assertTrue(process.killed)
                self.assertTrue(process.waited)
                self.assertEqual(self.manager.processes, set())

    def test_close_during_background_polling_reaps_registered_worker(self):
        job, generation = self.prepare()
        process = PollingProcess(expirations=1, on_timeout=self.manager.close)
        with patch("image_search.subprocess.Popen", return_value=process):
            with self.assertRaises(ReaderError):
                self.manager._decode_exclusive(source_path=self.album / "1.png", generation=generation)
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)
        self.assertEqual(self.manager.processes, set())

    def test_stdin_query_does_not_open_background_polling_connection(self):
        process = PollingProcess(expirations=0)
        with patch("image_search.subprocess.Popen", return_value=process), \
                patch.object(self.app, "connect", side_effect=AssertionError("Unexpected query connection")) as connector:
            result = self.manager._decode_exclusive(data=self.source)
        connector.assert_not_called()
        self.assertEqual(result["digest"], "a" * 64)
        self.assertEqual(process.calls, [(self.source, 60)])
        self.assertTrue(process.waited)
        self.assertEqual(self.manager.processes, set())


if __name__ == "__main__":
    unittest.main()

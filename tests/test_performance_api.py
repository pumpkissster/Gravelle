"""Authenticated shared task budgets, using disposable HTTP/data only."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import COOKIE_NAME, Library, ReaderServer


ROOT = Path(__file__).resolve().parents[1]


def snapshot(concurrency=1, interval=0):
    return {"taskConcurrency": concurrency, "maxTaskConcurrency": 8,
            "scanIntervalSeconds": interval, "maxScanIntervalSeconds": 604800}


class PerformanceAPITests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "performance-api-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="performance-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.data = self.root / "data"
        self.media.mkdir()
        self.app = Library(self.media, self.data, "", 0)
        self.app.register({"username": "performance-admin", "password": "temporary-performance-password"}, "test")
        child = self.app.save_user({"username": "performance-child", "password": "temporary-child-password",
                                   "library_ids": []})["user"]
        self.cookies = {"admin": f"{COOKIE_NAME}={self.app.make_session(1)}",
                        "child": f"{COOKIE_NAME}={self.app.make_session(child['id'])}"}
        self.server = None
        self.addCleanup(self.close_service)
        self.start_http()

    def start_http(self):
        self.server = ReaderServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close_service(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.server = None
        self.app.close()

    def request(self, *, method="GET", body=None, actor="admin", headers=None):
        request_headers = {"Cookie": self.cookies[actor]} if actor else {}
        content = json.dumps(body).encode() if method == "POST" else None
        if content is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, "/api/performance", body=content, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            payload = json.loads(data) if data and "application/json" in response.getheader("Content-Type", "") else data
            return response.status, payload
        finally:
            connection.close()

    def test_default_and_full_budget_range(self):
        self.assertEqual(self.request(), (200, snapshot()))
        for value in range(1, 9):
            with self.subTest(value=value):
                expected = snapshot(value)
                self.assertEqual(self.request(method="POST", body={"taskConcurrency": value}), (200, expected))
                self.assertEqual(self.request(), (200, expected))

    def test_scan_interval_range_and_partial_updates_preserve_the_other_setting(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 4}), (200, snapshot(4)))
        for value in (0, 1, 60, 300, 3600, 86400, 604800):
            with self.subTest(interval=value):
                self.assertEqual(self.request(method="POST", body={"scanIntervalSeconds": value}), (200, snapshot(4, value)))
                self.assertEqual(self.request(), (200, snapshot(4, value)))
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 2}), (200, snapshot(2, 604800)))
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 3, "scanIntervalSeconds": 17}),
                         (200, snapshot(3, 17)))

    def test_child_and_signed_out_cannot_read_or_change_global_budget(self):
        for actor, expected in ((None, 401), ("child", 403)):
            for method in ("GET", "HEAD", "POST"):
                with self.subTest(actor=actor, method=method):
                    status, _ = self.request(actor=actor, method=method,
                                             body={"taskConcurrency": 8, "scanIntervalSeconds": 300})
                    self.assertEqual(status, expected)
        self.assertEqual(self.request(), (200, snapshot()))

    def test_invalid_values_and_unknown_fields_are_atomic(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 4, "scanIntervalSeconds": 300})[0], 200)
        invalid = [{}, {"taskConcurrency": 0}, {"taskConcurrency": 9}, {"taskConcurrency": -1},
                   {"taskConcurrency": True}, {"taskConcurrency": False}, {"taskConcurrency": 2.0},
                   {"taskConcurrency": "2"}, {"taskConcurrency": None}, {"taskConcurrency": []},
                   {"taskConcurrency": {}}, {"taskConcurrency": 2, "workers": 8},
                   {"workers": 2}, {"maxScanIntervalSeconds": 100},
                   {"scanIntervalSeconds": 30, "workers": 2}, [], None, "invalid"]
        for value in (-1, 604801, True, False, 1.0, "1", None, [], {}):
            invalid.extend(({"scanIntervalSeconds": value}, {"taskConcurrency": 2, "scanIntervalSeconds": value}))
        invalid.extend(({"taskConcurrency": 0, "scanIntervalSeconds": 30},
                        {"taskConcurrency": "2", "scanIntervalSeconds": 30}))
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request(method="POST", body=body)[0], 400)
                self.assertEqual(self.request(), (200, snapshot(4, 300)))
                with closing(self.app.connect()) as connection:
                    row = connection.execute("SELECT task_concurrency,scan_interval_seconds FROM performance_settings WHERE id=1").fetchone()
                self.assertEqual(tuple(row), (4, 300))

    def test_post_rejects_cross_origin_and_accepts_matching_origin(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 8, "scanIntervalSeconds": 300},
                                     headers={"Origin": "https://unrelated.example"})[0], 403)
        self.assertEqual(self.request(), (200, snapshot()))
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 3, "scanIntervalSeconds": 60},
                                     headers={"Origin": origin})[0], 200)
        self.assertEqual(self.request(), (200, snapshot(3, 60)))

    def test_budget_survives_service_restart_with_accounts_and_integrity_preserved(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 6, "scanIntervalSeconds": 77})[0], 200)
        self.close_service()
        self.app = Library(self.media, self.data, "", 0)
        self.start_http()
        self.assertEqual(self.request(), (200, snapshot(6, 77)))
        with closing(self.app.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_unsaved_interval_follows_constructor_even_after_old_budget_only_save(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 4})[0], 200)
        for default in (17, 41):
            self.close_service()
            self.app = Library(self.media, self.data, "", default)
            self.start_http()
            self.assertEqual(self.request(), (200, snapshot(4, default)))
            with closing(self.app.connect()) as connection:
                self.assertIsNone(connection.execute("SELECT scan_interval_seconds FROM performance_settings WHERE id=1").fetchone()[0])

    def test_explicit_zero_survives_restart_and_overrides_nonzero_constructor(self):
        self.assertEqual(self.request(method="POST", body={"scanIntervalSeconds": 0})[0], 200)
        self.close_service()
        self.app = Library(self.media, self.data, "", 300)
        self.start_http()
        self.assertEqual(self.request(), (200, snapshot()))


class AutomaticScanTimerTests(unittest.TestCase):
    """Exercise real Condition wakeups without real scans or worker processes."""
    def setUp(self):
        scratch = ROOT / "work" / "performance-api-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="scan-timer-", dir=scratch)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.app = Library(self.media, self.root / "data", "", 0)
        self.patches = []
        self.addCleanup(self.close_timer)
        self.app.register({"username": "timer-admin", "password": "temporary-timer-password"}, "fixture")
        self.calls = []
        self.entered = threading.Event()

        def scan(*args, **kwargs):
            self.calls.append(time.monotonic())
            self.entered.set()
            return False

        for owner in (self.app.transcoding, self.app.image_search, self.app.library_covers, self.app.cache_manager):
            self.patches.append(patch.object(owner, "start"))
        self.patches.append(patch.object(self.app, "start_scan", scan))
        for item in self.patches:
            item.start()
        self.app.start()
        self.assertEqual(len(self.calls), 1)  # Existing startup scan still occurs.
        self.entered.clear()

    def close_timer(self):
        self.app.close()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def configure(self, interval):
        return self.app.task_scheduler.configure({"scanIntervalSeconds": interval})

    def wait_calls(self, minimum, timeout=2.5):
        deadline = time.monotonic() + timeout
        while len(self.calls) < minimum and time.monotonic() < deadline:
            self.entered.wait(max(0, deadline - time.monotonic()))
            self.entered.clear()
        self.assertGreaterEqual(len(self.calls), minimum, "Periodic scanner did not wake")

    def test_disabled_timer_can_enable_disable_and_enable_without_restart(self):
        self.assertFalse(self.entered.wait(.1))
        self.configure(1)
        self.wait_calls(2)
        self.configure(0)
        before = len(self.calls)
        self.entered.clear()
        self.assertFalse(self.entered.wait(1.15), "Disabled timer still scanned")
        self.assertEqual(len(self.calls), before)
        self.configure(1)
        self.wait_calls(before + 1)

    def test_shorter_setting_wakes_a_long_condition_wait(self):
        waiting = threading.Event()
        original = self.app.scan_timer_condition.wait

        def observed(timeout=None):
            if timeout is not None and timeout > 10:
                waiting.set()
            return original(timeout)

        with patch.object(self.app.scan_timer_condition, "wait", observed):
            self.configure(604800)
            self.assertTrue(waiting.wait(1), "Timer never entered its long wait")
            started = time.monotonic()
            self.configure(1)
            self.wait_calls(2)
            self.assertLess(time.monotonic() - started, 2.5)

    def test_extending_interval_restarts_the_due_time(self):
        self.configure(1)
        self.assertFalse(self.entered.wait(.25))
        self.configure(3)
        self.assertFalse(self.entered.wait(1.1), "Old shorter deadline was still used")
        self.configure(1)
        self.wait_calls(2)

    def test_timer_does_not_enqueue_a_duplicate_while_a_scan_is_running(self):
        with self.app.state_lock:
            self.app.scanning = True
        self.configure(1)
        self.assertFalse(self.entered.wait(1.15), "Timer queued a duplicate active scan")
        self.assertFalse(self.app.pending_scans)
        with self.app.state_lock:
            self.app.scanning = False
        self.configure(1)
        self.wait_calls(2)

    def test_close_wakes_disabled_timer_without_waiting_for_a_tick(self):
        started = time.monotonic()
        self.app.close()
        self.assertFalse(self.app.periodic_thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)

    def test_close_wakes_a_week_long_wait_without_leaking_the_thread(self):
        waiting = threading.Event()
        original = self.app.scan_timer_condition.wait

        def observed(timeout=None):
            if timeout is not None and timeout > 10:
                waiting.set()
            return original(timeout)

        with patch.object(self.app.scan_timer_condition, "wait", observed):
            self.configure(604800)
            self.assertTrue(waiting.wait(1))
            started = time.monotonic()
            self.app.close()
            self.assertFalse(self.app.periodic_thread.is_alive())
            self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()

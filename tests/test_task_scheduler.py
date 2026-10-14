"""Pure scheduling checks use only a disposable SQLite database and threads."""

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_scheduler import SharedTaskScheduler


class SettingsError(Exception):
    pass


class FakeLibrary:
    def __init__(self, path):
        self.path = path
        self.admin = True

    def connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def require_admin(self, connection):
        if not self.admin:
            raise SettingsError("admin required")


class SharedTaskSchedulerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "work" / "task-scheduler-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.library = FakeLibrary(Path(self.temp.name) / "test.sqlite3")
        self.scheduler = SharedTaskScheduler(self.library, SettingsError)
        self.addCleanup(self.scheduler.close)
        self.threads = []
        self.releases = []
        self.failures = []
        self.addCleanup(self.finish_threads)

    def finish_threads(self):
        self.scheduler.close()
        for event in self.releases:
            event.set()
        for thread in self.threads:
            thread.join(3)
        self.assertFalse([thread.name for thread in self.threads if thread.is_alive()])
        self.assertFalse(self.failures, str(self.failures))

    def spawn(self, function):
        def run():
            try:
                function()
            except BaseException as error:
                self.failures.append(error)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate())

    def holder(self, *, resource=None):
        entered = threading.Event()
        release = threading.Event()
        self.releases.append(release)
        def hold():
            with self.scheduler.slot(resource=resource) as acquired:
                self.assertTrue(acquired)
                entered.set()
                self.assertTrue(release.wait(3))
        self.spawn(hold)
        self.assertTrue(entered.wait(3))
        return release

    def test_default_validation_permission_and_persistence(self):
        self.assertEqual(self.scheduler.snapshot(), {"taskConcurrency": 1, "maxTaskConcurrency": 8})
        for body in ({}, None, [], {"taskConcurrency": True}, {"taskConcurrency": 1.0},
                     {"taskConcurrency": "2"}, {"taskConcurrency": 0}, {"taskConcurrency": 9},
                     {"taskConcurrency": 2, "other": 1}):
            with self.subTest(body=body), self.assertRaises(SettingsError):
                self.scheduler.configure(body)
        self.library.admin = False
        with self.assertRaisesRegex(SettingsError, "admin required"):
            self.scheduler.configure({"taskConcurrency": 2})
        self.library.admin = True
        self.scheduler.configure({"taskConcurrency": 8})
        other = SharedTaskScheduler(self.library, SettingsError)
        self.addCleanup(other.close)
        self.assertEqual(other.concurrency, 8)

    def test_default_serializes_background_families_fifo(self):
        release = self.holder(resource="optimization")
        order = []
        for number, resource in enumerate(("index", "optimization", "index")):
            def work(number=number, resource=resource):
                with self.scheduler.slot(resource=resource) as acquired:
                    self.assertTrue(acquired)
                    order.append(number)
            self.spawn(work)
            self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == number + 1)
        release.set()
        self.wait_for(lambda: len(order) == 3)
        self.assertEqual(order, [0, 1, 2])

    def test_eight_slots_and_ninth_waits(self):
        self.scheduler.configure({"taskConcurrency": 8})
        releases = [self.holder() for _ in range(8)]
        acquired = threading.Event()
        def ninth():
            with self.scheduler.slot() as value:
                self.assertTrue(value)
                acquired.set()
        self.spawn(ninth)
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        self.assertFalse(acquired.is_set())
        self.assertEqual(self.scheduler.snapshot_debug()["active"], 8)
        releases[0].set()
        self.assertTrue(acquired.wait(3))

    def test_queries_go_first_without_permanent_background_starvation(self):
        release = self.holder()
        order = []
        def work(label, priority):
            with self.scheduler.slot(priority=priority) as acquired:
                self.assertTrue(acquired)
                order.append(label)
        self.spawn(lambda: work("background", False))
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        for number in range(10):
            self.spawn(lambda number=number: work(number, True))
            self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == number + 2)
        release.set()
        self.wait_for(lambda: len(order) == 11)
        self.assertEqual(order[:8], list(range(8)))
        self.assertEqual(order[8], "background")
        self.assertEqual(order[9:], [8, 9])

    def test_priority_burst_does_not_starve_background_in_same_family(self):
        release = self.holder(resource="image-search")
        order = []
        def work(label, priority):
            with self.scheduler.slot(priority=priority, resource="image-search") as acquired:
                self.assertTrue(acquired)
                order.append(label)
        self.spawn(lambda: work("index", False))
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        for number in range(10):
            self.spawn(lambda number=number: work(number, True))
            self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == number + 2)
        release.set()
        self.wait_for(lambda: len(order) == 11)
        self.assertEqual(order[:8], list(range(8)))
        self.assertEqual(order[8], "index")
        self.assertEqual(order[9:], [8, 9])

    def test_cancel_waiter_does_not_hold_capacity_or_database_lock(self):
        release = self.holder()
        cancelled = threading.Event()
        done = threading.Event()
        values = []
        def cancel():
            # A callback can read SQLite and acquire scheduler state. Neither
            # operation may be called while the scheduler condition is held.
            with closing(self.library.connect()) as connection:
                connection.execute("SELECT 1").fetchone()
            other = threading.Thread(target=self.scheduler.snapshot_debug)
            other.start()
            other.join(.5)
            self.assertFalse(other.is_alive())
            return cancelled.is_set()
        def work():
            with self.scheduler.slot(cancel=cancel) as acquired:
                values.append(acquired)
                self.assertEqual(self.scheduler.snapshot_debug()["queued"], 0)
            done.set()
        self.spawn(work)
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        # A writer can commit while a task waits for its lease.
        self.scheduler.configure({"taskConcurrency": 1})
        cancelled.set()
        self.assertTrue(done.wait(.5))
        self.assertEqual(values, [False])
        self.assertEqual(self.scheduler.snapshot_debug()["queued"], 0)
        self.assertEqual(self.scheduler.snapshot_debug()["active"], 1)
        release.set()

    def test_cancelled_before_entry_and_close_wakes_waiters(self):
        with self.scheduler.slot(cancel=lambda: True) as value:
            self.assertFalse(value)
        release = self.holder()
        values = []
        def wait():
            with self.scheduler.slot() as value:
                values.append(value)
        self.spawn(wait)
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        self.scheduler.close()
        self.wait_for(lambda: values == [False])
        with self.scheduler.slot() as value:
            self.assertFalse(value)
        release.set()
        self.wait_for(lambda: self.scheduler.snapshot_debug()["active"] == 0)

    def test_lowering_limit_preserves_running_work(self):
        self.scheduler.configure({"taskConcurrency": 3})
        releases = [self.holder() for _ in range(3)]
        self.scheduler.configure({"taskConcurrency": 1})
        entered = threading.Event()
        def next_work():
            with self.scheduler.slot() as value:
                self.assertTrue(value)
                entered.set()
        self.spawn(next_work)
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        releases[0].set()
        self.wait_for(lambda: self.scheduler.snapshot_debug()["active"] == 2)
        releases[1].set()
        self.wait_for(lambda: self.scheduler.snapshot_debug()["active"] == 1)
        self.assertFalse(entered.is_set())
        releases[2].set()
        self.assertTrue(entered.wait(3))

    def test_increasing_limit_wakes_waiters(self):
        release = self.holder()
        entered = threading.Event()
        def next_work():
            with self.scheduler.slot() as value:
                self.assertTrue(value)
                entered.set()
        self.spawn(next_work)
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
        self.scheduler.configure({"taskConcurrency": 2})
        self.assertTrue(entered.wait(.5))
        release.set()

    def test_resource_reentrancy_and_family_serialization(self):
        self.scheduler.configure({"taskConcurrency": 2})
        order = []
        with self.scheduler.slot(resource="index") as outer:
            self.assertTrue(outer)
            with self.scheduler.slot(resource="index") as inner:
                self.assertTrue(inner)
                self.assertEqual(self.scheduler.snapshot_debug()["active"], 1)
                def same_family():
                    with self.scheduler.slot(resource="index") as value:
                        self.assertTrue(value)
                        order.append("index")
                self.spawn(same_family)
                self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 1)
                def other_family():
                    with self.scheduler.slot(resource="optimization") as value:
                        self.assertTrue(value)
                        order.append("optimization")
                self.spawn(other_family)
                self.wait_for(lambda: order == ["optimization"])
            self.assertEqual(self.scheduler.snapshot_debug()["active"], 1)
            self.assertEqual(order, ["optimization"])
        self.wait_for(lambda: order == ["optimization", "index"])

    def test_exception_releases_lease_and_cancel_callback_exception_removes_waiter(self):
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.scheduler.slot(resource="index"):
                raise RuntimeError("body failed")
        self.assertEqual(self.scheduler.snapshot_debug()["active"], 0)
        release = self.holder()
        calls = [0]
        def cancel():
            calls[0] += 1
            if calls[0] > 1:
                raise RuntimeError("callback failed")
            return False
        def wait():
            with self.assertRaisesRegex(RuntimeError, "callback failed"):
                with self.scheduler.slot(cancel=cancel):
                    self.fail("No lease should be acquired")
        self.spawn(wait)
        self.wait_for(lambda: calls[0] > 1)
        self.wait_for(lambda: self.scheduler.snapshot_debug()["queued"] == 0)
        release.set()


if __name__ == "__main__":
    unittest.main()

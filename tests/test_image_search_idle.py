"""Bound completed-index polling without delaying catalogue/configuration wakes."""
from contextlib import closing
import threading
import time
from unittest.mock import patch

from image_search import ALGORITHM_VERSION, LOCAL_VERSION
from test_image_search import ImageSearchFixture


class ImageSearchIdleTests(ImageSearchFixture):
    def seed_completed(self, count):
        self.manager.configure({'enabled': True})
        with closing(self.app.connect()) as connection, connection:
            connection.execute('DELETE FROM is_pages')
            connection.executemany('''INSERT INTO is_pages
                (book_id,page_key,page_index,source_version,book_signature,
                 algorithm,status,local_state,local_version,updated_at)
                VALUES(?,?,?,?,?,?,'ready','ready',?,?)''',
                [(self.book_id, f'completed-{index}.png', index, 'fixture-version',
                  'fixture-signature', ALGORITHM_VERSION, LOCAL_VERSION, 1)
                 for index in range(count)])
            connection.execute('UPDATE is_settings SET reconcile_pending=0')

    def measure_empty_queue(self):
        original = self.app.connect
        queries = []
        measured = {'active': False, 'steps': 0}

        def connect():
            connection = original()

            def trace(sql):
                measured['active'] = ('FROM is_pages p JOIN books b' in sql
                                      and "p.local_state='pending'" in sql
                                      and 'LIMIT 1' in sql)
                if measured['active']:
                    queries.append(sql)

            def progress():
                if measured['active']:
                    measured['steps'] += 1
                return 0

            connection.set_trace_callback(trace)
            connection.set_progress_handler(progress, 1)
            return connection

        with patch.object(self.app, 'connect', side_effect=connect):
            self.assertFalse(self.manager.run_once())
        self.assertEqual(len(queries), 1, 'Measure the actual worker queue query')
        with closing(original()) as connection:
            plan = [row['detail'] for row in connection.execute('EXPLAIN QUERY PLAN ' + queries[0])]
        return measured['steps'], plan

    def test_completed_catalogue_does_not_require_visiting_all_ready_pages(self):
        self.seed_completed(128)
        small, _ = self.measure_empty_queue()
        self.seed_completed(12232)
        large, plan = self.measure_empty_queue()
        self.assertLess(large, 500, plan)
        self.assertLessEqual(large, small + 40, 'Idle work must not scale with completed pages')
        self.assertTrue(any('is_pages_work_state' in row and 'local_state=?' in row for row in plan), plan)

    def test_index_is_added_after_legacy_local_state_column_migration(self):
        self.seed_completed(2)
        with closing(self.app.connect()) as connection, connection:
            connection.execute('DROP INDEX is_pages_work_state')
            connection.execute('ALTER TABLE is_pages DROP COLUMN local_state')
        self.manager._initialize()
        with closing(self.app.connect()) as connection:
            columns = [row['name'] for row in connection.execute('PRAGMA index_info(is_pages_work_state)')]
            rows = connection.execute('SELECT status,local_state,COUNT(*) AS total FROM is_pages GROUP BY status,local_state').fetchall()
        self.assertEqual(columns, ['status', 'local_state', 'book_id', 'page_index'])
        self.assertEqual([tuple(row) for row in rows], [('ready', 'pending', 2)])

    def test_completed_index_uses_long_idle_wait_instead_of_two_second_polling(self):
        self.seed_completed(2)
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.manager.stop_event.set()
            return False

        with patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.manager._loop()
        self.assertEqual(waits, [30])

    def start_loop(self):
        self.manager.thread = threading.Thread(target=self.manager._loop, name='fixture-index-idle', daemon=True)
        self.manager.thread.start()

    def exercise_wake(self, change):
        waiting, processed = threading.Event(), threading.Event()
        original_wait = self.manager.wake.wait
        calls = []

        def once():
            calls.append(time.monotonic())
            if len(calls) > 1:
                processed.set()
            return False

        def wait(seconds):
            if seconds >= 30:
                waiting.set()
            return original_wait(seconds)

        with patch.object(self.manager, 'run_once', side_effect=once), \
                patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.start_loop()
            try:
                self.assertTrue(waiting.wait(2), 'Fixture worker never reached its idle wait')
                change()
                self.assertTrue(processed.wait(2), 'A persisted change waited for the 30 second fallback')
            finally:
                self.manager.close()
        self.assertFalse(self.manager.thread.is_alive())

    def test_catalogue_reconciliation_wakes_an_idle_coordinator_immediately(self):
        self.exercise_wake(self.manager.reconcile)

    def test_administrator_resume_wakes_an_idle_coordinator_immediately(self):
        self.manager.configure({'enabled': True})
        self.manager.configure({'action': 'pause'})
        self.exercise_wake(lambda: self.manager.configure({'action': 'resume'}))
        with closing(self.app.connect()) as connection:
            settings = self.manager._settings(connection)
        self.assertTrue(settings['enabled'])
        self.assertFalse(settings['paused'])
        self.assertTrue(settings['reconcile_pending'])

    def test_signal_committed_during_queue_check_reaches_the_next_wait(self):
        calls, waits = [], []
        original_wait = self.manager.wake.wait

        def once():
            calls.append(True)
            if len(calls) == 1:
                self.manager.reconcile()
            else:
                self.manager.stop_event.set()
            return False

        def wait(seconds):
            self.assertTrue(self.manager.wake.is_set(), 'A wake emitted during the queue check was lost')
            waits.append((seconds, original_wait(0)))
            return True

        with patch.object(self.manager, 'run_once', side_effect=once), \
                patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.manager._loop()
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [(30, True)])

    def test_close_wakes_the_long_idle_wait_without_joining_for_the_timeout(self):
        waiting = threading.Event()
        original_wait = self.manager.wake.wait

        def wait(seconds):
            waiting.set()
            return original_wait(seconds)

        with patch.object(self.manager, 'run_once', return_value=False), \
                patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.start_loop()
            self.assertTrue(waiting.wait(2))
            started = time.monotonic()
            self.manager.close()
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(self.manager.thread.is_alive())

    def test_close_at_clear_window_does_not_enter_a_new_long_wait(self):
        original_clear = self.manager.wake.clear

        def close_at_clear():
            # Model close just after while's stop check and before clear.
            self.manager.stop_event.set()
            self.manager.wake.set()
            original_clear()

        with patch.object(self.manager.wake, 'clear', side_effect=close_at_clear), \
                patch.object(self.manager, 'run_once', return_value=False), \
                patch.object(self.manager.wake, 'wait') as waiter:
            self.manager._loop()
        waiter.assert_not_called()

    def test_error_retry_keeps_two_seconds_then_returns_to_idle_wait(self):
        self.manager.configure({'enabled': True})
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                self.manager.stop_event.set()
            return False

        with self.assertLogs('sumi', level='ERROR'), \
                patch.object(self.manager, 'run_once', side_effect=[RuntimeError('fixture failure'), False]), \
                patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.manager._loop()
        self.assertEqual(waits, [2, 30])
        with closing(self.app.connect()) as connection:
            error = connection.execute('SELECT error FROM is_settings').fetchone()[0]
        self.assertIn('图片索引任务失败', error)

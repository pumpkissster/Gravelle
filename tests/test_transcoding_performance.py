"""Bounded optimization list queries and prompt event-driven queue scheduling."""
from contextlib import closing
import json
import threading
import time
from unittest.mock import patch

from test_transcoding import TranscodingFixture


def seed_catalogue(fixture, books, pages):
    """Generate metadata only; no source images or decoding are required."""
    with closing(fixture.app.connect()) as connection, connection:
        template = dict(connection.execute('SELECT * FROM books WHERE id=?', (fixture.book_id,)).fetchone())
        connection.execute('DELETE FROM tc_pages')
        connection.execute('DELETE FROM tc_outputs')
        connection.execute('DELETE FROM tc_books')
        connection.execute("DELETE FROM books WHERE path LIKE 'generated-%'")
        connection.execute('UPDATE books SET active=0')
        identifiers = []
        for index in range(books):
            identifier = f'{index + 1:024x}'
            identifiers.append(identifier)
            start, end = index * pages // books, (index + 1) * pages // books
            book = {**template, 'id': identifier, 'title': f'Generated {index}',
                    'path': f'generated-{index}', 'active': 1, 'removed': 0,
                    'pages': json.dumps([f'page-{page}.png' for page in range(start, end)]),
                    'page_count': end - start}
            fields = list(book)
            connection.execute('INSERT INTO books(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')',
                               [book[key] for key in fields])
            connection.execute('''INSERT INTO tc_books
                (book_id,decision,status,published,rule_revision,source_signature,updated_at)
                VALUES(?,'original','original',1,1,'generated',?)''', (identifier, index))
            connection.executemany('''INSERT INTO tc_pages
                (book_id,page_key,page_index,source_version,size,width,height,
                 matched,reasons,desired_profile,status)
                VALUES(?,?,?,'generated',1000,100,150,1,'["pixels"]','generated','pending')''',
                [(identifier, f'page-{page}.png', page - start) for page in range(start, end)])
    return identifiers


class OptimizationPerformanceTests(TranscodingFixture):
    def statements(self, call):
        collected = []
        original = self.app.connect

        def connect():
            connection = original()
            connection.set_trace_callback(collected.append)
            return connection

        with patch.object(self.app, 'connect', side_effect=connect):
            result = call()
        return result, collected

    def test_list_query_count_does_not_grow_per_book_or_load_full_page_rows(self):
        self.enable()
        seed_catalogue(self, 1, 100)
        one, statements = self.statements(self.manager.snapshot)
        first = [sql for sql in statements if sql.lstrip().upper().startswith(('SELECT', 'WITH'))]
        seed_catalogue(self, 80, 2240)
        many, statements = self.statements(self.manager.snapshot)
        second = [sql for sql in statements if sql.lstrip().upper().startswith(('SELECT', 'WITH'))]
        self.assertEqual(len(one['books']), 1)
        self.assertEqual(len(many['books']), 80)
        self.assertEqual(len(first), len(second))
        self.assertLessEqual(len(second), 9)
        self.assertFalse(any('SELECT * FROM tc_pages' in sql for sql in second))
        self.assertEqual(many['summary']['matched_images'], 2240)
        self.assertTrue(all(book['pending_count'] == 28 for book in many['books']))

    def test_aggregates_preserve_mixed_status_reasons_distinct_outputs_and_old_mapping(self):
        self.enable()
        with closing(self.app.connect()) as connection, connection:
            pages = [dict(row) for row in connection.execute('SELECT * FROM tc_pages WHERE book_id=? ORDER BY page_index', (self.book_id,))]
            first, unmatched, second = pages
            names = [self.book_id + '/' + str(index) * 64 + '.webp' for index in (1, 2, 3)]
            for index, size in enumerate((11, 13, 17)):
                connection.execute('''INSERT INTO tc_outputs
                    (book_id,page_key,source_version,profile,file,size,mime,digest,created_at)
                    VALUES(?,?,?,?,?,?,'image/webp','generated',1)''',
                    (self.book_id, f'output-{index}', 'source', 'profile', names[index], size))
            connection.execute('''UPDATE tc_pages SET status='success',reasons='["size"]',
                active_file=?,desired_file=?,active_source=source_version,active_profile=desired_profile
                WHERE book_id=? AND page_key=?''', (names[0], names[0], self.book_id, first['page_key']))
            connection.execute('''UPDATE tc_pages SET status='failed',reasons='["size","pixels"]',
                active_file=?,desired_file=?,active_source=source_version,active_profile='old-profile'
                WHERE book_id=? AND page_key=?''', (names[0], names[0], self.book_id, second['page_key']))
            # Existing mappings can survive a rule change on an unmatched page.
            connection.execute('UPDATE tc_pages SET active_file=? WHERE book_id=? AND page_key=?',
                               (names[1], self.book_id, unmatched['page_key']))
            for index, status in ((3, 'pending'), (4, 'running')):
                row = {**first, 'page_key': f'generated-{index}.png', 'page_index': index,
                       'status': status, 'reasons': '["side"]' if index == 3 else '["pixels"]',
                       'active_file': '', 'desired_file': ''}
                fields = list(row)
                connection.execute('INSERT INTO tc_pages(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')',
                                   [row[key] for key in fields])
            connection.execute("UPDATE tc_books SET decision='transcode',status='queued' WHERE book_id=?", (self.book_id,))
        data = self.manager.snapshot()
        book = next(item for item in data['books'] if item['id'] == self.book_id)
        self.assertEqual({key: book[key] for key in ('matched_count', 'success_count', 'failed_count', 'running_count', 'pending_count')},
                         dict(matched_count=4, success_count=1, failed_count=1, running_count=1, pending_count=1))
        self.assertEqual(book['reasons'], ['pixels', 'side', 'size'])
        self.assertEqual((book['actual_bytes'], book['output_count']), (24, 2))
        self.assertEqual(book['output_path'], str(self.manager.root / self.book_id))
        self.assertTrue(book['needs_retranscode'])
        self.assertEqual(data['summary']['stored_bytes'], 41)
        self.assertEqual((data['summary']['reclaimable_files'], data['summary']['reclaimable_bytes']), (1, 17))

    def test_normal_empty_and_ordered_queue_selection_does_not_open_write_transaction(self):
        self.enable()
        result, statements = self.statements(self.manager._take_job)
        self.assertIsNone(result)
        self.assertFalse(any(sql.upper().startswith(('BEGIN', 'UPDATE', 'INSERT')) for sql in statements))
        self.action('transcode')
        selected, statements = self.statements(self.manager._take_job)
        try:
            self.assertIsNotNone(selected)
            self.assertFalse(any(sql.upper().startswith(('BEGIN', 'UPDATE', 'INSERT')) for sql in statements))
        finally:
            self.manager.active_books.clear()

    def test_missing_queue_priority_is_committed_once_then_future_reads_stay_read_only(self):
        self.enable()
        self.action('transcode')
        with closing(self.app.connect()) as connection, connection:
            connection.execute('UPDATE tc_books SET queue_order=0 WHERE book_id=?', (self.book_id,))
        selected, statements = self.statements(self.manager._take_job)
        self.assertIsNotNone(selected)
        self.manager.active_books.clear()
        self.assertTrue(any(sql.upper().startswith('BEGIN IMMEDIATE') for sql in statements))
        with closing(self.app.connect()) as connection:
            self.assertGreater(connection.execute('SELECT queue_order FROM tc_books WHERE book_id=?', (self.book_id,)).fetchone()[0], 0)
        selected, statements = self.statements(self.manager._take_job)
        self.manager.active_books.clear()
        self.assertIsNotNone(selected)
        self.assertFalse(any(sql.upper().startswith(('BEGIN', 'UPDATE')) for sql in statements))

    def test_idle_loop_waits_thirty_seconds_and_close_at_clear_does_not_wait(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.manager.stop_event.set()

        with patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.manager._loop()
        self.assertEqual(waits, [30])
        self.manager.stop_event.clear()
        original = self.manager.wake.clear

        def close_window():
            self.manager.stop_event.set()
            self.manager.wake.set()
            original()

        with patch.object(self.manager.wake, 'clear', side_effect=close_window), \
                patch.object(self.manager.wake, 'wait') as waiter:
            self.manager._loop()
        waiter.assert_not_called()

    def test_idle_queue_is_woken_by_administrator_action(self):
        self.enable()
        waiting, processed = threading.Event(), threading.Event()
        original = self.manager.wake.wait

        def wait(seconds):
            waiting.set()
            return original(seconds)

        def process(job, rules):
            self.manager.active_books.discard(job['id'])
            self.manager.stop_event.set()
            processed.set()

        with patch.object(self.manager.wake, 'wait', side_effect=wait), \
                patch.object(self.manager, '_process_job', side_effect=process):
            self.manager.start()
            try:
                self.assertTrue(waiting.wait(2))
                self.action('transcode')
                self.assertTrue(processed.wait(2), 'New work waited for the idle timeout')
            finally:
                self.manager.close()
        self.assertFalse(self.manager.is_alive())

    def test_increasing_concurrency_wakes_a_coordinator_with_an_inflight_book(self):
        self.enable(max_source_pixels=1)
        self.action('transcode', [self.book_id, self.small_id])
        entered, release = set(), threading.Event()
        lock = threading.Lock()

        def process(job, rules):
            with lock:
                entered.add(job['id'])
            try:
                release.wait(5)
            finally:
                self.manager.active_books.discard(job['id'])

        def wait_for_count(count):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with lock:
                    if len(entered) >= count:
                        return True
                time.sleep(.01)
            return False

        with patch.object(self.manager, '_process_job', side_effect=process):
            self.manager.start()
            try:
                self.assertTrue(wait_for_count(1))
                self.app.task_scheduler.configure({'taskConcurrency': 2})
                self.assertTrue(wait_for_count(2), 'Changing concurrency waited for a worker to finish')
            finally:
                self.manager.stop_event.set()
                self.manager.wake.set()
                release.set()
                self.manager.close()
        self.assertFalse(self.manager.is_alive())

    def test_queue_coordinator_errors_keep_short_retry_instead_of_idle_timeout(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.manager.stop_event.set()

        with self.assertLogs('sumi', level='ERROR'), \
                patch.object(self.manager, '_take_job', side_effect=RuntimeError('generated failure')), \
                patch.object(self.manager.wake, 'wait', side_effect=wait):
            self.manager._loop()
        self.assertEqual(waits, [2])

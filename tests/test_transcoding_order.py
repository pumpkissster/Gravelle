"""Stable whole-book optimization ordering without monopolizing image leases."""
from contextlib import closing
import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from server import Library, ReaderError
from transcoding import DEFAULT_RULES


ROOT = Path(__file__).resolve().parents[1]


class OptimizationOrderTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'work' / 'optimization-order-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media, self.data = self.root / 'media', self.root / 'data'
        self.counts = {'A': 3, 'B': 2, 'C': 4}
        self.source = BytesIO()
        Image.new('RGB', (16, 24), 'pink').save(self.source, 'PNG')
        output = BytesIO()
        Image.new('RGB', (16, 24), 'pink').save(output, 'WEBP')
        self.output = output.getvalue()
        for title, count in self.counts.items():
            folder = self.media / title
            folder.mkdir(parents=True)
            for index in range(count):
                (folder / f'{index + 1}.png').write_bytes(self.source.getvalue())
        self.open_library()
        self.addCleanup(lambda: self.app.close())
        self.app.register({'username': 'ordering-admin', 'password': 'generated-ordering-password'}, 'test')
        self.library_id = self.app.save_library({'name': 'Ordering fixture', 'path': str(self.media), 'media_type': 'photo'})['library']['id']
        self.wait(lambda: not self.app.scanning)
        self.ids = {book['title']: book['id'] for book in self.app.listing()['books']}
        self.titles = {identifier: title for title, identifier in self.ids.items()}
        self.tc.save_rules({'rules': {**DEFAULT_RULES, 'enabled': True,
                                    'file_size_mb': 0, 'max_source_pixels': 1,
                                    'max_source_side': 0, 'min_free_space_mb': 0},
                            'expected_revision': self.tc.settings()[1]})
        self.events = []

    def open_library(self):
        self.app = Library(self.media, self.data, '', 0)
        self.tc = self.app.transcoding
        self.tc.auto_start = self.app.image_search.auto_start = False

    def wait(self, condition, timeout=10):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            time.sleep(.01)
        self.assertTrue(condition(), 'Generated optimization did not reach expected state')

    def rows(self, statement, arguments=()):
        with closing(self.app.connect()) as connection:
            return [dict(row) for row in connection.execute(statement, arguments)]

    def action(self, action, titles):
        return self.tc.actions({'ids': [self.ids[title] for title in titles], 'action': action})

    def page_index(self, job):
        return self.rows('SELECT page_index FROM tc_pages WHERE book_id=? AND page_key=?',
                         (job['id'], job['page_key']))[0]['page_index']

    def convert(self, job, rules):
        """Use actual queue/publication/storage paths with a tiny valid output."""
        self.events.append((self.titles[job['id']], self.page_index(job)))
        name = hashlib.sha256((job['page_key'] + job['desired_profile']).encode()).hexdigest() + '.webp'
        folder = self.tc._storage_directory(job['id'])
        (folder / name).write_bytes(self.output)
        return {'file': job['id'] + '/' + name, 'size': len(self.output),
                'mime': 'image/webp', 'digest': hashlib.sha256(self.output).hexdigest()}

    def drain(self):
        for _ in range(30):
            if not self.tc.run_once():
                return
        self.fail('Generated work queue failed to drain')

    def expected(self, *titles):
        return [(title, index) for title in titles for index in range(self.counts[title])]

    def test_three_different_books_finish_in_administrator_start_order(self):
        self.action('transcode', ['C', 'A', 'B'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.drain()
        self.assertEqual(self.events, self.expected('C', 'A', 'B'))
        self.assertEqual({row['status'] for row in self.rows('SELECT status FROM tc_books')}, {'ready'})

    def test_page_updates_and_an_unchanged_scan_do_not_move_the_current_book(self):
        self.action('transcode', ['C', 'A', 'B'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.assertTrue(self.tc.run_once())
            self.app.start_scan(self.library_id)
            self.wait(lambda: not self.app.scanning)
            with closing(self.app.connect()) as connection, connection:
                connection.execute('UPDATE tc_books SET updated_at=0')
                connection.execute('UPDATE tc_books SET updated_at=9999999999 WHERE book_id=?', (self.ids['C'],))
            self.drain()
        self.assertEqual(self.events, self.expected('C', 'A', 'B'))

    def test_repeating_start_for_a_queued_book_does_not_move_or_promote_it(self):
        self.action('transcode', ['A', 'B', 'C'])
        self.action('transcode', ['C'])
        self.action('transcode', ['B'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.drain()
        self.assertEqual(self.events, self.expected('A', 'B', 'C'))

    def test_runtime_backfill_commits_priority_before_page_timestamps_change(self):
        self.action('transcode', ['C', 'A', 'B'])
        # Simulate queued rows without priority appearing after initialization.
        # Backfill must commit on selection, rather than vanish on close.
        with closing(self.app.connect()) as connection, connection:
            connection.execute('UPDATE tc_books SET queue_order=0')
            for title, stamp in [('C', 0), ('A', 1), ('B', 2)]:
                connection.execute('UPDATE tc_books SET updated_at=? WHERE book_id=?',
                                   (stamp, self.ids[title]))
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            selected = self.tc._take_job()
            self.assertIsNotNone(selected)
            self.assertEqual(selected[0]['id'], self.ids['C'])
            orders = {row['book_id']: row['queue_order']
                      for row in self.rows('SELECT book_id,queue_order FROM tc_books')}
            self.assertGreater(orders[self.ids['C']], 0)
            self.assertLess(orders[self.ids['C']], orders[self.ids['A']])
            self.assertLess(orders[self.ids['A']], orders[self.ids['B']])
            self.tc._process_job(*selected)
            with closing(self.app.connect()) as connection, connection:
                connection.execute('UPDATE tc_books SET updated_at=9999999999 WHERE book_id=?',
                                   (self.ids['C'],))
            self.drain()
        self.assertEqual(self.events, self.expected('C', 'A', 'B'))
        self.assertEqual({row['book_id']: row['queue_order']
                          for row in self.rows('SELECT book_id,queue_order FROM tc_books')}, orders)

    def test_a_completed_book_with_a_new_page_rejoins_at_the_tail_after_a_scan(self):
        initial_a = self.expected('A')
        self.action('transcode', ['A'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.drain()
            old_order = self.rows('SELECT queue_order FROM tc_books WHERE book_id=?',
                                  (self.ids['A'],))[0]['queue_order']
            self.action('transcode', ['C', 'B'])
            self.tc.run_once()
            (self.media / 'A' / '4.png').write_bytes(self.source.getvalue())
            self.app.start_scan(self.library_id)
            self.wait(lambda: not self.app.scanning)
            # A changed completed work requires another administrator decision;
            # its historical priority must not preempt the current work.
            scanned = self.rows('SELECT status,queue_order FROM tc_books WHERE book_id=?',
                                (self.ids['A'],))[0]
            self.assertEqual(scanned, {'status': 'pending', 'queue_order': old_order})
            self.action('transcode', ['A'])
            orders = {row['book_id']: row['queue_order']
                      for row in self.rows('SELECT book_id,queue_order FROM tc_books')}
            self.assertLess(orders[self.ids['C']], orders[self.ids['B']])
            self.assertLess(orders[self.ids['B']], orders[self.ids['A']])
            self.drain()
        self.assertEqual(self.events, initial_a + self.expected('C', 'B') + [('A', 3)])

    def test_pausing_and_resuming_goes_to_the_tail_without_preempting_next_book(self):
        self.action('transcode', ['A', 'B', 'C'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.tc.run_once()
            self.action('pause', ['A'])
            self.tc.run_once()
            self.action('resume', ['A'])
            self.drain()
        expected = [('A', 0), ('B', 0), ('B', 1)] + self.expected('C') + [('A', 1), ('A', 2)]
        self.assertEqual(self.events, expected)

    def test_cancelling_the_inflight_book_immediately_releases_the_next_book(self):
        self.action('transcode', ['A', 'B', 'C'])
        def cancelled(job, rules):
            if job['id'] == self.ids['A']:
                self.action('cancel', ['A'])
            return self.convert(job, rules)
        with patch.object(self.tc, '_convert', side_effect=cancelled):
            self.drain()
        self.assertEqual(self.events, [('A', 0)] + self.expected('B', 'C'))
        self.assertEqual(self.rows('SELECT status FROM tc_books WHERE book_id=?', (self.ids['A'],))[0]['status'], 'cancelled')
        self.assertFalse(self.tc.active_books)

    def test_a_failed_page_does_not_block_later_books(self):
        self.action('transcode', ['A', 'B', 'C'])
        def failed(job, rules):
            if job['id'] == self.ids['A'] and self.page_index(job) == 1:
                self.events.append(('A', 1))
                raise ReaderError('Generated page failure', 422)
            return self.convert(job, rules)
        with patch.object(self.tc, '_convert', side_effect=failed):
            self.drain()
        self.assertEqual(self.events, [('A', 0), ('A', 1)] + self.expected('B', 'C'))
        self.assertEqual(self.rows('SELECT status FROM tc_books WHERE book_id=?', (self.ids['A'],))[0]['status'], 'failed')

    def test_removing_a_queued_work_does_not_block_the_remaining_queue(self):
        self.action('original', ['A', 'B', 'C'])
        self.action('transcode', ['A', 'B', 'C'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.tc.run_once()
            self.app.batch_books({'ids': [self.ids['A']], 'action': 'remove'})
            self.drain()
        self.assertEqual(self.events, [('A', 0)] + self.expected('B', 'C'))
        self.assertEqual(self.rows('SELECT active,removed FROM books WHERE id=?', (self.ids['A'],))[0], {'active': 0, 'removed': 1})

    def test_restart_preserves_whole_book_priority_after_a_page_updated_its_timestamp(self):
        self.action('transcode', ['C', 'A', 'B'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.tc.run_once()
        self.app.close()
        self.open_library()
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.drain()
        self.assertEqual(self.events, self.expected('C', 'A', 'B'))

    def test_legacy_schema_recovers_running_head_and_stable_waiting_order(self):
        self.action('transcode', ['C', 'A', 'B'])
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.tc.run_once()
        self.app.close()
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as connection, connection:
            connection.execute('DROP INDEX tc_books_queue_order')
            connection.execute('DROP INDEX tc_books_work_state')
            connection.execute('ALTER TABLE tc_books DROP COLUMN queue_order')
            for title, stamp in [('A', 1), ('B', 2), ('C', 9)]:
                connection.execute('UPDATE tc_books SET updated_at=? WHERE book_id=?', (stamp, self.ids[title]))
            connection.execute("UPDATE tc_books SET status='running' WHERE book_id=?", (self.ids['C'],))
            connection.execute("UPDATE tc_pages SET status='running' WHERE book_id=? AND page_index=1", (self.ids['C'],))
        self.open_library()
        with patch.object(self.tc, '_convert', side_effect=self.convert):
            self.drain()
        self.assertEqual(self.events, self.expected('C', 'A', 'B'))
        self.assertTrue(all(row['queue_order'] > 0 for row in self.rows('SELECT queue_order FROM tc_books')))

    def test_parallel_workers_never_convert_two_pages_of_the_same_book(self):
        self.action('transcode', ['A', 'B', 'C'])
        self.app.task_scheduler.configure({'taskConcurrency': 3})
        gate = threading.Event()
        lock = threading.Lock()
        active, entered, duplicates = set(), set(), []
        def parallel(job, rules):
            with lock:
                if job['id'] in active:
                    duplicates.append(job['id'])
                active.add(job['id']); entered.add(job['id'])
            try:
                if self.page_index(job) == 0:
                    if not gate.wait(10):
                        raise AssertionError('Generated parallel gate timed out')
                return self.convert(job, rules)
            finally:
                with lock:
                    active.discard(job['id'])
        with patch.object(self.tc, '_convert', side_effect=parallel):
            self.tc.start()
            try:
                self.wait(lambda: len(entered) == 3)
                self.assertEqual(set(self.tc.active_books), set(self.ids.values()))
                self.assertTrue(all(row['count'] == 1 for row in self.rows("SELECT book_id,COUNT(*) AS count FROM tc_pages WHERE status='running' GROUP BY book_id")))
            finally:
                gate.set()
            self.wait(lambda: {row['status'] for row in self.rows('SELECT status FROM tc_books')} == {'ready'})
            self.tc.close()
        self.assertEqual(duplicates, [])
        for title, count in self.counts.items():
            self.assertEqual([index for value, index in self.events if value == title], list(range(count)))

    def test_search_can_acquire_the_shared_lease_between_pages_of_the_current_book(self):
        self.action('transcode', ['A', 'B', 'C'])
        entered, release = threading.Event(), threading.Event()
        errors = []
        def converted(job, rules):
            result = self.convert(job, rules)
            if job['id'] == self.ids['A'] and self.page_index(job) == 0:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Generated lease gate timed out')
            return result
        def query():
            try:
                with self.app.task_scheduler.slot(priority=True) as acquired:
                    self.assertTrue(acquired)
                    self.events.append(('query', -1))
            except BaseException as error:
                errors.append(error)
        with patch.object(self.tc, '_convert', side_effect=converted):
            self.tc.start()
            try:
                self.assertTrue(entered.wait(5))
                thread = threading.Thread(target=query, daemon=True)
                thread.start()
                self.wait(lambda: self.app.task_scheduler.snapshot_debug()['priorityQueued'] == 1)
            finally:
                release.set()
            self.wait(lambda: {row['status'] for row in self.rows('SELECT status FROM tc_books')} == {'ready'})
            thread.join(5)
            self.tc.close()
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.events, [('A', 0), ('query', -1)] + self.expected('A')[1:] + self.expected('B', 'C'))


if __name__ == '__main__':
    unittest.main()

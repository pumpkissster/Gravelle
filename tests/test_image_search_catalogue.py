"""Bounded catalogue reconciliation over disposable image sources."""
from contextlib import closing, contextmanager
import json
import math
import threading
import unittest
from unittest.mock import patch

from test_image_search import ImageSearchFixture, picture
from image_search import CATALOGUE_BATCH_SIZE


class ImageSearchCatalogueTests(ImageSearchFixture):
    def generation(self):
        with closing(self.app.connect()) as connection:
            return connection.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]

    def prepare(self, pages=2):
        for index in range(3, pages + 1):
            (self.album / f"{index}.png").write_bytes(self.source)
        if pages > 2:
            self.app._scan_library(self.app._library_config(self.library_id))
        self.manager.configure({"enabled": True})
        return self.generation()

    def synchronize(self, generation):
        # Activity rendering is independently tested by the status tests. The
        # catalogue's contract here is its bounded and cancellable database IO.
        with patch.object(self.manager, "_set_activity", create=True):
            self.manager._sync_catalogue(generation)

    def test_large_catalogue_reuses_connection_and_bounds_write_batches(self):
        count = CATALOGUE_BATCH_SIZE * 2 + 7
        generation = self.prepare(count)
        statements = []
        connections = []
        connect = self.app.connect
        open_source = self.app.transcoding._open_source

        def traced_connection():
            connection = connect()
            connections.append(connection)
            connection.set_trace_callback(statements.append)
            return connection

        @contextmanager
        def source_outside_transaction(book, key):
            self.assertEqual(len(connections), 1)
            self.assertFalse(connections[0].in_transaction,
                             "Filesystem access must not hold a database transaction")
            with open_source(book, key) as source:
                yield source

        with patch.object(self.app, "connect", side_effect=traced_connection) as connector, \
                patch.object(self.app.transcoding, "_open_source", side_effect=source_outside_transaction):
            self.synchronize(generation)
        self.assertEqual(connector.call_count, 1)
        transactions = [statement for statement in statements if statement.upper().startswith("BEGIN")]
        self.assertEqual(len(transactions), math.ceil(count / CATALOGUE_BATCH_SIZE) + 2)
        self.assertEqual(sum(statement.upper() == "COMMIT" for statement in statements), len(transactions))
        writes = 0
        largest = 0
        for statement in statements:
            command = statement.upper().lstrip()
            if command.startswith(("INSERT INTO IS_PAGES", "UPDATE IS_PAGES", "DELETE FROM IS_PAGES")):
                writes += 1
            elif command == "COMMIT":
                largest = max(largest, writes)
                writes = 0
        self.assertEqual(largest, CATALOGUE_BATCH_SIZE)
        self.assertEqual(len(self.rows()), count)

    def test_unchanged_catalogue_does_not_rewrite_page_rows_or_features(self):
        generation = self.prepare(CATALOGUE_BATCH_SIZE + 1)
        self.synchronize(generation)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("""UPDATE is_pages SET status='ready',local_state='ready',
                digest='retained',local_features=X'1234',preview=X'5678'""")
        initial = self.rows()
        statements = []
        connect = self.app.connect

        def traced_connection():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(self.app, "connect", side_effect=traced_connection) as connector:
            self.synchronize(generation)
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(initial, self.rows())
        writes = [statement for statement in statements if statement.upper().lstrip().startswith(
            ("INSERT INTO IS_PAGES", "UPDATE IS_PAGES", "DELETE FROM IS_PAGES"))]
        self.assertEqual(writes, [])

    def check_concurrent_change(self, change):
        generation = self.prepare()
        reached_source = threading.Event()
        release_source = threading.Event()
        errors = []
        open_source = self.app.transcoding._open_source
        first = True

        @contextmanager
        def blocked_source(book, key):
            nonlocal first
            with open_source(book, key) as source:
                yield source
            if first:
                first = False
                reached_source.set()
                if not release_source.wait(5):
                    raise RuntimeError("Concurrent mutation did not finish")

        def synchronize():
            try:
                self.synchronize(generation)
            except BaseException as error:
                errors.append(error)

        with patch.object(self.app.transcoding, "_open_source", side_effect=blocked_source):
            thread = threading.Thread(target=synchronize)
            thread.start()
            try:
                self.assertTrue(reached_source.wait(5))
                change()
            finally:
                release_source.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return generation

    def test_clear_during_source_check_does_not_commit_old_generation(self):
        generation = self.check_concurrent_change(lambda: self.manager.configure({"action": "clear"}))
        self.assertGreater(self.generation(), generation)
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.manager.snapshot()["enabled"])

    def test_scan_during_source_check_does_not_commit_old_manifest(self):
        def replace_and_scan():
            (self.album / "1.png").write_bytes(picture(73))
            self.app._scan_library(self.app._library_config(self.library_id))

        generation = self.check_concurrent_change(replace_and_scan)
        self.assertEqual(self.rows(), [], "A batch from the old book signature must not be applied")
        with closing(self.app.connect()) as connection:
            self.assertTrue(connection.execute("SELECT reconcile_pending FROM is_settings WHERE id=1").fetchone()[0])
        self.synchronize(generation)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        updated = next(row for row in rows if row["page_key"].endswith("/1.png"))
        self.assertEqual(updated["source_version"], self.app.transcoding._file_version((self.album / "1.png").stat()))

    def test_cleanup_rechecks_pages_restored_after_candidate_collection(self):
        generation = self.prepare()
        self.synchronize(generation)
        initial = self.rows()
        removed = initial[-1]["page_key"]
        with closing(self.app.connect()) as connection, connection:
            original = connection.execute("SELECT pages FROM books WHERE id=?", (self.book_id,)).fetchone()[0]
            connection.execute("UPDATE books SET pages=? WHERE id=?",
                               (json.dumps([initial[0]["page_key"]]), self.book_id))
        remove = self.manager._remove_catalogue_batch

        def restore_before_delete(connection, current_generation, obsolete):
            self.assertIn((self.book_id, removed), obsolete)
            with closing(self.app.connect()) as writer, writer:
                writer.execute("UPDATE books SET pages=? WHERE id=?", (original, self.book_id))
            return remove(connection, current_generation, obsolete)

        with patch.object(self.manager, "_remove_catalogue_batch", side_effect=restore_before_delete):
            self.synchronize(generation)
        self.assertEqual(self.rows(), initial)


if __name__ == "__main__":
    unittest.main()

"""Generated-cover checks for persistent library montages and cache boundaries."""
from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from server import Library, ReaderError
from library_covers import LibraryCoverManager, WIDTH, HEIGHT


ROOT = Path(__file__).resolve().parents[1]


class LibraryCoverTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("SUMI_TEST_TEMP", ROOT / "work/library-covers-qa"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="montage-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        for index in range(6):
            directory = self.media / f"book-{index}"
            directory.mkdir()
            image = Image.new("RGB", (160, 240), (20 + index * 35, 50, 180 - index * 25))
            image.save(directory / "1.jpg", "JPEG")
            image.transpose(Image.Transpose.ROTATE_180).save(directory / "2.jpg", "JPEG")
        self.hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.media.rglob("*.jpg")}
        self.app = Library(self.media, self.root / "data", "", 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = self.app.image_search.auto_start = False
        self.app.register({"username": "cover-admin", "password": "temporary-cover-password"}, "test")
        self.library_id = self.app.save_library({"name": "Test", "path": str(self.media), "media_type": "photo"})["library"]["id"]
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)
        self.manager = getattr(self.app, "library_covers", None) or LibraryCoverManager(self.app, ReaderError)
        self.addCleanup(self.manager.close)

    def record(self):
        with closing(self.app.connect()) as connection:
            row = connection.execute("SELECT * FROM library_cover_cache WHERE library_id=?", (self.library_id,)).fetchone()
            return dict(row) if row else None

    def ready(self):
        self.manager.metadata(self.library_id)
        for _ in range(10):
            if not self.manager.run_once():
                break
        self.assertFalse(self.manager.metadata(self.library_id)["pending"])
        return self.manager.image(self.library_id)

    def test_get_is_lightweight_and_background_montage_has_four_distinct_books(self):
        with patch.object(self.app, "cover", side_effect=AssertionError("GET decoded a book")):
            initial = self.manager.metadata(self.library_id)
            self.assertTrue(initial["pending"])
            self.assertEqual(self.manager.image(self.library_id)[0], self.manager.placeholder)
        data, mime, etag = self.ready()
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(etag, '"' + hashlib.sha256(data).hexdigest() + '"')
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (WIDTH, HEIGHT))
        chosen = json.loads(self.record()["selection"])
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertEqual(len(chosen), 4)
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.hashes}, self.hashes)

    def test_ready_hit_unchanged_scan_and_recreated_manager_preserve_selection_and_bytes(self):
        data, _, etag = self.ready()
        original = self.record()
        self.app._scan_library(self.app._library_config(self.library_id))
        self.manager.reconcile()
        other = LibraryCoverManager(self.app, ReaderError)
        self.addCleanup(other.close)
        with patch.object(self.app, "cover", side_effect=AssertionError("Cached montage reread book")):
            self.assertEqual(other.image(self.library_id), (data, "image/jpeg", etag))
            self.assertFalse(other.metadata(self.library_id)["pending"])
        self.assertEqual(self.record()["selection"], original["selection"])
        self.assertEqual(self.record()["generation"], original["generation"])

    def test_shuffle_above_four_always_changes_group(self):
        self.ready()
        for _ in range(10):
            before = set(json.loads(self.record()["selection"]))
            self.assertTrue(self.manager.shuffle(self.library_id)["pending"])
            after = json.loads(self.record()["selection"])
            self.assertEqual(len(after), 4)
            self.assertEqual(len(set(after)), 4)
            self.assertNotEqual(set(after), before)
            self.ready()
        self.assertEqual(len(self.manager.cache_entries()), 1)

    def test_zero_one_and_three_candidates_never_duplicate_fill(self):
        with closing(self.app.connect()) as connection, connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM books ORDER BY id")]
        for count in (3, 1, 0):
            with closing(self.app.connect()) as connection, connection:
                connection.execute("UPDATE books SET active=0")
                for book_id in ids[:count]:
                    connection.execute("UPDATE books SET active=1 WHERE id=?", (book_id,))
            metadata = self.manager.metadata(self.library_id)
            self.assertEqual(metadata["count"], count)
            self.assertEqual(len(set(json.loads(self.record()["selection"]))), count)
            self.manager.run_once()
            if count == 0:
                self.assertFalse(metadata["pending"])
                self.assertEqual(self.manager.image(self.library_id)[0], self.manager.placeholder)

    def test_cover_source_removal_and_unpublication_invalidate_old_image(self):
        self.ready()
        old_etag = self.manager.image(self.library_id)[2]
        selected = json.loads(self.record()["selection"])
        self.app.save_book_metadata(selected[0], {"cover_page": 1})
        self.assertTrue(self.manager.metadata(self.library_id)["pending"])
        self.assertNotEqual(self.manager.image(self.library_id)[2], old_etag)
        self.ready()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE tc_settings SET enabled=1")
            connection.execute("UPDATE tc_books SET published=0 WHERE book_id=?", (selected[0],))
            connection.execute("UPDATE books SET removed=1 WHERE id=?", (selected[1],))
        self.assertTrue(self.manager.metadata(self.library_id)["pending"])
        self.assertEqual(self.manager.image(self.library_id)[0], self.manager.placeholder)
        updated = json.loads(self.record()["selection"])
        self.assertNotIn(selected[0], updated)
        self.assertNotIn(selected[1], updated)

    def test_hidden_disabled_and_revoked_accounts_cannot_receive_ready_image_or_shuffle(self):
        self.ready()
        child = self.app.save_user({"username": "cover-child", "password": "temporary-child-password",
                                    "enabled": True, "library_ids": [self.library_id]})["user"]["id"]
        with self.app.acting_as(child):
            self.assertNotEqual(self.manager.image(self.library_id)[0], self.manager.placeholder)
            with self.assertRaises(ReaderError) as caught:
                self.manager.shuffle(self.library_id)
            self.assertEqual(caught.exception.status, 403)
            self.app.save_my_libraries({"visible_library_ids": []})
            for method in (self.manager.metadata, self.manager.image):
                with self.assertRaises(ReaderError) as caught:
                    method(self.library_id)
                self.assertEqual(caught.exception.status, 404)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("DELETE FROM user_hidden_libraries WHERE user_id=?", (child,))
            connection.execute("DELETE FROM user_libraries WHERE user_id=?", (child,))
        with self.app.acting_as(child), self.assertRaises(ReaderError) as caught:
            self.manager.image(self.library_id)
        self.assertEqual(caught.exception.status, 404)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE users SET enabled=0 WHERE id=?", (child,))
        with self.app.acting_as(child), self.assertRaises(ReaderError) as caught:
            self.manager.image(self.library_id)
        self.assertEqual(caught.exception.status, 401)

    def test_clear_during_generation_cannot_recreate_stale_cache_and_preserves_group(self):
        self.manager.metadata(self.library_id)
        chosen = self.record()["selection"]
        entered, resume = threading.Event(), threading.Event()
        original_cover = self.app.cover
        calls = []
        def blocked(book_id):
            if not calls:
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("clear race timed out")
            calls.append(book_id)
            return original_cover(book_id)
        with patch.object(self.app, "cover", side_effect=blocked):
            worker = threading.Thread(target=self.manager.run_once)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.manager.clear_cache()
            resume.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.record()["selection"], chosen)
        self.assertEqual(self.record()["deferred"], 1)
        self.assertEqual(self.manager.cache_entries(), [])
        self.assertEqual(list(self.manager.root.glob(".tmp-*")), [])
        self.ready()

    def test_cache_tamper_missing_files_and_owned_eviction_recover_without_touching_other_files(self):
        self.ready()
        entry = self.manager.cache_entries()[0]
        path = entry["path"]
        path.write_bytes(b"tampered")
        self.assertEqual(self.manager.image(self.library_id)[0], self.manager.placeholder)
        self.ready()
        ordinary = self.manager.root / "keep-me.jpg"
        ordinary.write_bytes(b"not-owned")
        self.assertEqual(self.manager.remove_cache_file(ordinary), {"files": 0, "bytes": 0})
        removed = self.manager.remove_cache_file(self.manager.cache_entries()[0]["path"])
        self.assertEqual(removed["files"], 1)
        self.assertTrue(removed["bytes"] > 0)
        self.assertEqual(self.record()["deferred"], 1)
        self.manager.reconcile()
        self.assertFalse(self.manager.run_once())
        self.ready()
        self.assertEqual(self.manager.clear_cache()["files"], 1)
        self.assertEqual(ordinary.read_bytes(), b"not-owned")

    def test_background_composition_refuses_raw_large_and_non_jpeg_inputs(self):
        for size, format_name in (((401, 600), "JPEG"), ((32, 48), "PNG")):
            output = BytesIO()
            Image.new("RGB", size).save(output, format_name)
            with self.subTest(size=size, format=format_name), self.assertRaises(ValueError):
                self.manager._compose([output.getvalue()])

    def test_failed_generation_exposes_generic_error_without_permanent_pending_and_shuffle_retries(self):
        self.manager.metadata(self.library_id)
        with patch.object(self.app, "cover", side_effect=ValueError("private source path must not escape")):
            with self.assertRaises(ValueError):
                self.manager.run_once()
        failed = self.manager.metadata(self.library_id)
        self.assertFalse(failed["pending"])
        self.assertIn("换一组封面", failed["error"])
        self.assertNotIn("private", failed["error"])
        self.assertGreater(failed["retry_after"], time.time())
        self.assertFalse(self.manager.run_once())
        retried = self.manager.shuffle(self.library_id)
        self.assertTrue(retried["pending"])
        self.assertEqual(retried["error"], "")
        self.ready()

    def test_source_scan_change_and_empty_library_prune_stale_owned_montage(self):
        self.ready()
        selected = json.loads(self.record()["selection"])[0]
        book = self.app.book(selected, internal=True)
        source = Path(book["source_root"]) / json.loads(book["pages"])[0]
        Image.new("RGB", (160, 240), "#ea5098").save(source, "JPEG")
        self.app._scan_library(self.app._library_config(self.library_id))
        self.assertTrue(self.manager.metadata(self.library_id)["pending"])
        self.ready()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET active=0 WHERE library_id=?", (self.library_id,))
        self.manager.reconcile()
        self.assertEqual(self.manager.cache_entries(), [])
        self.assertEqual(self.manager.metadata(self.library_id)["count"], 0)

    def test_candidate_listing_only_checks_page_source_versions_for_final_four_books(self):
        statements = []
        connect = self.app.connect
        def traced_connection():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(self.app, "connect", side_effect=traced_connection):
            self.manager.metadata(self.library_id)
        source_queries = [statement for statement in statements if "SELECT p.source_version FROM tc_pages" in statement]
        self.assertEqual(len(source_queries), 1)
        queried = set(re.findall(r"'[a-f0-9]{24}'", source_queries[0]))
        self.assertEqual(queried, {"'" + item + "'" for item in json.loads(self.record()["selection"])})
        self.assertEqual(len(queried), 4)


if __name__ == "__main__":
    unittest.main()

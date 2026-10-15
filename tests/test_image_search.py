"""Disposable-source checks for private, incremental local image matching."""
from contextlib import closing
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Library, ReaderError
from image_search import ALGORITHM_VERSION, ImageSearchManager, MAX_QUERY_PIXELS, _features


def picture(seed=1, size=(384, 576), format="PNG", **options):
    randomizer = random.Random(seed)
    image = Image.new("RGB", size, (220, 210, 205))
    draw = ImageDraw.Draw(image)
    for _ in range(20):
        x, y = randomizer.randrange(size[0]), randomizer.randrange(size[1])
        w, h = randomizer.randrange(20, size[0] // 2), randomizer.randrange(20, size[1] // 2)
        draw.rectangle((x, y, x + w, y + h), fill=tuple(randomizer.randrange(255) for _ in range(3)))
    draw.text((20, 20), str(seed), fill="black")
    stream = BytesIO()
    image.save(stream, format, **options)
    return stream.getvalue()


class ImageSearchFixture(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "image-search-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="images-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.data = self.root / "data"
        self.album = self.media / "public" / "几何作品"
        self.album.mkdir(parents=True)
        self.source = picture(1)
        self.second = picture(8)
        (self.album / "1.png").write_bytes(self.source)
        (self.album / "2.png").write_bytes(self.second)
        self.app = Library(self.media, self.data, "", 0)
        self.addCleanup(lambda: self.app.close())
        self.app.register({"username": "image-admin", "password": "temporary-image-search-password"}, "test")
        self.manager = getattr(self.app, "image_search", None) or ImageSearchManager(self.app, ReaderError)
        self.manager.auto_start = False
        self.app.transcoding.auto_start = False
        self.library_id = self.create_library(self.media / "public", "开放写真库")
        self.book_id = self.app.listing()["books"][0]["id"]

    def create_library(self, path, name):
        value = self.app.save_library({"name": name, "path": str(path), "media_type": "photo"})["library"]["id"]
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)
        return value

    def enable(self):
        self.manager.configure({"enabled": True})
        self.finish()

    def finish(self):
        for _ in range(50):
            if not self.manager.run_once():
                return
        self.fail("Image index did not become idle")

    def rows(self):
        with closing(self.app.connect()) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM is_pages ORDER BY book_id,page_index")]

    def error(self, status, function, *args, **kwargs):
        with self.assertRaises(ReaderError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status, str(caught.exception))
        return caught.exception

    def child(self, libraries):
        return self.app.save_user({"username": "image-child", "password": "temporary-child-image-password",
                                  "enabled": True, "library_ids": libraries})["user"]["id"]


class ImageSearchTests(ImageSearchFixture):
    def decode_fixture(self, data=None, *, source_path=None, **options):
        payload = source_path.read_bytes() if source_path is not None else data
        result = _features(BytesIO(payload), preview=options.get("preview", False))
        result["digest"] = hashlib.sha256(payload).hexdigest()
        for name in ("preview", "local_features", "local_sketch"):
            if name in result:
                result[name] = base64.b64decode(result[name], validate=True)
        return result

    def test_repeated_catalogue_reconciliation_does_not_starve_page_indexing(self):
        self.manager.configure({"enabled": True})
        synchronize = self.manager._sync_catalogue

        def scan_during_synchronization(generation):
            synchronize(generation)
            # A periodic scan can finish while this directory pass is running.
            self.manager.reconcile()

        with patch.object(self.manager, "_sync_catalogue", side_effect=scan_during_synchronization), \
                patch.object(self.manager, "_decode", side_effect=self.decode_fixture) as decoder:
            for _ in range(4):
                self.assertTrue(self.manager.run_once())
            self.assertGreater(self.manager.snapshot()["summary"]["indexed"], 0)
            self.assertGreater(decoder.call_count, 0)

    def test_unchanged_scan_does_not_reconcile_but_source_changes_do(self):
        def reconciliation_pending():
            with closing(self.app.connect()) as connection:
                return connection.execute("SELECT reconcile_pending FROM is_settings WHERE id=1").fetchone()[0]

        def scan():
            self.app._scan_library(self.app._library_config(self.library_id))

        with patch.object(self.manager, "_decode", side_effect=self.decode_fixture) as decoder:
            self.enable()
            initial = {row["page_key"]: row for row in self.rows()}
            self.assertFalse(reconciliation_pending())
            decoder.reset_mock()
            scan()
            self.assertFalse(reconciliation_pending(), "An unchanged scan must not restart the whole index catalogue pass")
            self.assertFalse(self.manager.run_once())
            decoder.assert_not_called()

            added = picture(51)
            (self.album / "3.png").write_bytes(added)
            scan()
            self.assertTrue(reconciliation_pending())
            self.finish()
            self.assertEqual(decoder.call_count, 1)
            added_rows = {row["page_key"]: row for row in self.rows()}
            self.assertEqual(len(added_rows), 3)
            for key, row in initial.items():
                self.assertEqual(added_rows[key]["updated_at"], row["updated_at"])

            decoder.reset_mock()
            changed = picture(52)
            (self.album / "1.png").write_bytes(changed)
            scan()
            self.assertTrue(reconciliation_pending())
            self.finish()
            self.assertEqual(decoder.call_count, 1)
            changed_rows = {row["page_key"]: row for row in self.rows()}
            for key, row in added_rows.items():
                if key.endswith("/1.png"):
                    self.assertEqual(changed_rows[key]["digest"], hashlib.sha256(changed).hexdigest())
                else:
                    self.assertEqual(changed_rows[key]["updated_at"], row["updated_at"])
            self.assertEqual(self.manager.snapshot()["summary"], {"total": 3, "indexed": 3, "pending": 0, "failed": 0})

    def test_scan_detects_atomic_page_replacement_preserving_size_and_mtime(self):
        original = picture(61)
        replacement = picture(62)
        padded_size = max(len(original), len(replacement)) + 128
        original += b"\0" * (padded_size - len(original))
        replacement += b"\0" * (padded_size - len(replacement))
        target = self.album / "1.png"
        target.write_bytes(original)
        self.app._scan_library(self.app._library_config(self.library_id))
        with patch.object(self.manager, "_decode", side_effect=self.decode_fixture) as decoder:
            self.enable()
            previous = target.stat()
            with closing(self.app.connect()) as connection:
                signature = connection.execute("SELECT signature FROM books WHERE id=?", (self.book_id,)).fetchone()[0]
            temporary = self.album / "replacement.tmp"
            temporary.write_bytes(replacement)
            os.utime(temporary, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            os.replace(temporary, target)
            current = target.stat()
            self.assertEqual((current.st_size, current.st_mtime_ns), (previous.st_size, previous.st_mtime_ns))
            if current.st_ino == previous.st_ino:
                self.skipTest("This filesystem does not expose distinct file identities for atomic replacement")
            decoder.reset_mock()
            self.app._scan_library(self.app._library_config(self.library_id))
            with closing(self.app.connect()) as connection:
                self.assertEqual(connection.execute("SELECT signature FROM books WHERE id=?", (self.book_id,)).fetchone()[0], signature)
                self.assertTrue(connection.execute("SELECT reconcile_pending FROM is_settings WHERE id=1").fetchone()[0])
            self.finish()
            self.assertEqual(decoder.call_count, 1)
            row = next(row for row in self.rows() if row["page_key"].endswith("/1.png"))
            self.assertEqual(row["digest"], hashlib.sha256(replacement).hexdigest())
            self.assertEqual(row["source_version"], self.app.transcoding._file_version(current))

    def test_changed_scan_warnings_reconcile_including_partial_recovery(self):
        walk = os.walk

        def scan_with_warnings(names):
            def walking(path, *args, **options):
                for name in names:
                    options["onerror"](PermissionError(13, "Temporary directory access failure", str(Path(path) / name)))
                yield from walk(path, *args, **options)

            with patch("server.os.walk", side_effect=walking):
                self.app._scan_library(self.app._library_config(self.library_id))

        def reconciliation_pending():
            with closing(self.app.connect()) as connection:
                return connection.execute("SELECT reconcile_pending FROM is_settings WHERE id=1").fetchone()[0]

        with patch.object(self.manager, "_decode", side_effect=self.decode_fixture) as decoder:
            self.enable()
            initial = {row["page_key"]: row["updated_at"] for row in self.rows()}
            decoder.reset_mock()
            scan_with_warnings(["unreadable-a", "unreadable-b"])
            self.assertTrue(reconciliation_pending())
            self.finish()
            scan_with_warnings(["unreadable-a"])
            self.assertTrue(reconciliation_pending(), "Recovering one directory must be detected while another warning remains")
            self.finish()
            scan_with_warnings(["unreadable-a"])
            self.assertFalse(reconciliation_pending(), "Identical persistent warnings must not continually restart reconciliation")
            scan_with_warnings([])
            self.assertTrue(reconciliation_pending())
            self.finish()
            decoder.assert_not_called()
            self.assertEqual({row["page_key"]: row["updated_at"] for row in self.rows()}, initial)

    def test_invalid_admin_actions_do_not_change_settings(self):
        before = self.manager.snapshot()
        for body in ({"action": None}, {"action": []}, {"action": {}}, {"enabled": 1}, {},
                     {"action": "unknown"}, {"enabled": True, "action": "rebuild"}):
            self.error(400, self.manager.configure, body)
        self.assertEqual(self.manager.snapshot(), before)

    def test_default_disabled_and_exact_match_has_page_preview_without_source_changes(self):
        before = hashlib.sha256((self.album / "1.png").read_bytes()).hexdigest()
        self.assertFalse(self.manager.snapshot()["enabled"])
        self.error(409, self.manager.query, self.source)
        self.enable()
        status = self.manager.snapshot()
        self.assertEqual(status["summary"], {"total": 2, "indexed": 2, "pending": 0, "failed": 0})
        match = self.manager.query(self.source)
        self.assertEqual(match["results"][0], {"book_id": self.book_id, "page": 0, "match": "exact", "distance": 0})
        self.assertEqual(match["total"], 1)
        preview, mime, etag = self.manager.thumbnail(self.book_id, 0)
        self.assertEqual(mime, "image/jpeg")
        with Image.open(BytesIO(preview)) as image:
            self.assertLessEqual(max(image.size), 384)
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(before, hashlib.sha256((self.album / "1.png").read_bytes()).hexdigest())
        self.assertEqual(list(self.manager.root.iterdir()), [])

    def test_resized_and_recompressed_query_finds_original_without_semantic_false_match(self):
        self.enable()
        with Image.open(BytesIO(self.source)) as image:
            image = image.resize((192, 288))
            stream = BytesIO()
            image.save(stream, "JPEG", quality=45)
        response = self.manager.query(stream.getvalue())
        self.assertEqual(response["results"][0]["book_id"], self.book_id)
        self.assertEqual(response["results"][0]["page"], 0)
        self.assertEqual(response["results"][0]["match"], "similar")
        self.assertEqual(self.manager.query(picture(59))["results"], [])

    def test_index_accepts_69mp_jpeg_without_changing_main_process_pillow_limit(self):
        source_path = self.album / "3.jpg"
        image = Image.new("L", (9903, 6959), 128)
        image.save(source_path, "JPEG", quality=80)
        del image
        self.app.start_scan(self.library_id)
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        original_limit = Image.MAX_IMAGE_PIXELS
        self.enable()
        self.assertEqual(Image.MAX_IMAGE_PIXELS, original_limit)
        self.assertEqual(self.manager.snapshot()["summary"]["indexed"], 3)
        matches = self.manager.query(source_path.read_bytes())["results"]
        self.assertTrue(any(item["page"] == 2 and item["match"] == "exact" for item in matches))

    def test_index_accepts_large_png_in_isolated_decoder(self):
        source_path = self.album / "3.png"
        image = Image.new("L", (9903, 6959), 128)
        image.save(source_path, "PNG")
        del image
        self.app.start_scan(self.library_id)
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.enable()
        self.assertEqual(self.manager.snapshot()["summary"]["indexed"], 3)
        self.assertTrue(any(item["page"] == 2 for item in self.manager.query(source_path.read_bytes())["results"]))

    def test_zip_pages_are_indexed_without_extracting_sources(self):
        archive = self.media / "public" / "压缩作品.cbz"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("sub/1.png", self.source)
            output.writestr("sub/2.webp", picture(2, format="WEBP"))
        self.app.start_scan(self.library_id)
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.enable()
        result = self.manager.query(self.source)
        self.assertEqual(result["total"], 2)
        self.assertEqual({item["page"] for item in result["results"]}, {0})
        self.assertFalse((self.media / "public" / "sub").exists())

    def test_access_and_hidden_libraries_filter_before_limit_and_preview(self):
        private = self.media / "private" / "secret"
        private.mkdir(parents=True)
        (private / "1.png").write_bytes(self.source)
        private_id = self.create_library(private.parent, "未授权写真库")
        private_book = next(item["id"] for item in self.app.listing()["books"] if item["library_id"] == private_id)
        self.enable()
        child = self.child([self.library_id])
        with self.app.acting_as(child):
            result = self.manager.query(self.source, limit=1)
            self.assertEqual(result["results"], [{"book_id": self.book_id, "page": 0, "match": "exact", "distance": 0}])
            self.assertEqual(result["total"], 1)
            self.assertEqual(set(self.manager.snapshot()), {"enabled", "available", "indexing"})
            self.error(404, self.manager.thumbnail, private_book, 0)
            self.error(403, self.manager.configure, {"enabled": False})
        with closing(self.app.connect()) as connection, connection:
            connection.execute("INSERT INTO user_hidden_libraries(user_id,library_id) VALUES(?,?)", (child, self.library_id))
        with self.app.acting_as(child):
            self.assertEqual(self.manager.query(self.source)["results"], [])
            self.assertFalse(self.manager.snapshot()["available"])
            self.error(404, self.manager.thumbnail, self.book_id, 0)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("DELETE FROM user_hidden_libraries WHERE user_id=?", (child,))
            connection.execute("DELETE FROM user_libraries WHERE user_id=?", (child,))
        self.assertEqual(self.manager.query(self.source, user_id=child)["results"], [])

    def test_optimization_waiting_work_is_never_a_search_candidate(self):
        self.enable()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE tc_settings SET enabled=1")
            connection.execute("UPDATE tc_books SET published=0 WHERE book_id=?", (self.book_id,))
        self.assertEqual(self.manager.query(self.source)["results"], [])
        self.error(404, self.manager.thumbnail, self.book_id, 0)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE tc_books SET published=1 WHERE book_id=?", (self.book_id,))
        self.assertEqual(self.manager.query(self.source)["results"][0]["book_id"], self.book_id)

    def test_unscanned_source_change_rejects_old_match_and_thumbnail_then_reindexes(self):
        self.enable()
        (self.album / "1.png").write_bytes(picture(40))
        self.assertEqual(self.manager.query(self.source)["results"], [])
        self.error(404, self.manager.thumbnail, self.book_id, 0)
        self.finish()
        self.assertEqual(self.manager.query(self.source)["results"], [])
        self.assertEqual(self.manager.query(picture(40))["results"][0]["page"], 0)

    def test_removed_work_has_no_results_even_before_index_reconciliation(self):
        self.enable()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET active=0,removed=1 WHERE id=?", (self.book_id,))
        self.assertEqual(self.manager.query(self.source)["results"], [])
        self.error(404, self.manager.thumbnail, self.book_id, 0)
        self.manager.reconcile()
        self.finish()
        self.assertEqual(self.rows(), [])

    def test_add_remove_reorder_and_id_migration_keep_stable_page_keys(self):
        self.enable()
        initial = {row["page_key"]: row["updated_at"] for row in self.rows()}
        (self.album / "0.png").write_bytes(picture(5))
        (self.album / "2.png").unlink()
        self.app.start_scan(self.library_id)
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.finish()
        rows = self.rows()
        original = next(row for row in rows if row["page_key"].endswith("/1.png"))
        self.assertEqual(original["page_index"], 1)
        self.assertEqual(original["updated_at"], initial[original["page_key"]])
        self.assertFalse(any(row["page_key"].endswith("/2.png") for row in rows))
        new_id = "a" * 24
        with closing(self.app.connect()) as connection, connection:
            # Historical ID seeding predates personal/metadata records. The
            # search table must not introduce a new foreign-key obstacle.
            connection.execute("DELETE FROM book_models WHERE book_id=?", (self.book_id,))
            connection.execute("DELETE FROM book_metadata WHERE book_id=?", (self.book_id,))
            connection.execute("UPDATE books SET id=? WHERE id=?", (new_id, self.book_id))
        self.assertEqual(self.manager.query(self.source)["results"][0]["book_id"], new_id)
        self.assertEqual(self.manager.query(self.source)["results"][0]["page"], 1)

    def test_clear_during_decode_does_not_restore_deleted_index(self):
        self.manager.configure({"enabled": True})
        self.manager.run_once()
        decode = self.manager._decode
        def clearing(*args, **kwargs):
            result = decode(*args, **kwargs)
            self.manager.configure({"action": "clear"})
            return result
        with patch.object(self.manager, "_decode", side_effect=clearing):
            self.assertTrue(self.manager.run_once())
        self.assertFalse(self.manager.snapshot()["enabled"])
        self.assertEqual(self.rows(), [])

    def test_private_work_directory_link_does_not_prevent_reader_restart(self):
        self.app.close()
        outside = self.root / "unrelated"
        outside.mkdir()
        (outside / "keep").write_text("untouched", encoding="utf-8")
        scratch = self.data / "image-search-work"
        try:
            scratch.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows without link privileges still exercises the same
            # manager recovery branch via an invalid ordinary file.
            scratch.write_text("invalid scratch directory", encoding="utf-8")
        self.app = Library(self.media, self.data, "", 0)
        self.manager = self.app.image_search
        self.manager.auto_start = False
        self.assertIn("临时目录", self.manager.snapshot()["error"])
        self.assertEqual((outside / "keep").read_text(encoding="utf-8"), "untouched")
        self.assertEqual(len(self.app.listing()["books"]), 1)

    def test_pause_restart_resume_clear_and_rebuild_preserve_sources(self):
        self.manager.configure({"enabled": True})
        self.assertTrue(self.manager.run_once())
        self.manager.configure({"action": "pause"})
        self.assertFalse(self.manager.run_once())
        self.assertTrue(self.manager.snapshot()["paused"])
        self.app.close()
        self.app = Library(self.media, self.data, "", 0)
        self.manager = self.app.image_search
        self.manager.auto_start = False
        self.assertTrue(self.manager.snapshot()["paused"])
        self.manager.configure({"action": "resume"})
        self.finish()
        self.assertEqual(self.manager.snapshot()["summary"]["indexed"], 2)
        self.manager.configure({"action": "clear"})
        self.assertFalse(self.manager.snapshot()["enabled"])
        self.assertEqual(self.rows(), [])
        self.error(404, self.manager.thumbnail, self.book_id, 0)
        self.manager.configure({"action": "rebuild"})
        self.finish()
        self.assertEqual(self.manager.query(self.source)["results"][0]["match"], "exact")
        self.assertEqual((self.album / "1.png").read_bytes(), self.source)

    def test_source_change_during_decode_does_not_commit_stale_fingerprint(self):
        self.manager.configure({"enabled": True})
        self.manager.run_once()
        decode = self.manager._decode
        def replacing(*args, **kwargs):
            result = decode(*args, **kwargs)
            (self.album / "1.png").write_bytes(picture(44))
            return result
        with patch.object(self.manager, "_decode", side_effect=replacing):
            self.manager.run_once()
        self.assertNotEqual(self.rows()[0]["status"], "ready")
        self.finish()
        self.assertEqual(self.manager.query(self.source)["results"], [])

    def test_failed_page_retry_does_not_rebuild_unchanged_ready_pages(self):
        (self.album / "1.png").write_bytes(b"not an image")
        self.enable()
        self.assertEqual(self.manager.snapshot()["summary"], {"total": 2, "indexed": 1, "pending": 0, "failed": 1})
        unchanged = self.rows()[1]["updated_at"]
        (self.album / "1.png").write_bytes(self.source)
        self.manager.configure({"action": "retry"})
        self.finish()
        self.assertEqual(self.manager.snapshot()["summary"]["indexed"], 2)
        self.assertEqual(self.rows()[1]["updated_at"], unchanged)

    def test_malformed_oversized_and_unsupported_uploads_are_bounded(self):
        self.enable()
        self.error(422, self.manager.query, b"not an image")
        self.error(422, self.manager.query, b"")
        stream = BytesIO()
        Image.new("RGB", (8, 8)).save(stream, "GIF")
        self.error(422, self.manager.query, stream.getvalue())
        with patch("image_search.MAX_BYTES", 1):
            self.error(422, self.manager.query, b"xx")
        image = Image.new("L", (10001, 10000))
        stream = BytesIO()
        image.save(stream, "JPEG", quality=1)
        del image
        self.error(422, self.manager.query, stream.getvalue())
        self.assertEqual(self.manager.processes, set())

    def test_decoder_is_single_process_and_queries_take_priority_over_background(self):
        self.enable()
        events = []
        entered = threading.Event()
        release = threading.Event()
        generation = 1
        with closing(self.app.connect()) as connection:
            generation = connection.execute("SELECT generation FROM is_settings").fetchone()[0]
        def fake(data=None, **kwargs):
            name = "query" if data else threading.current_thread().name
            events.append(name)
            if name == "first":
                entered.set()
                release.wait(5)
            time.sleep(.01)
            return {}
        with patch.object(self.manager, "_decode_exclusive", side_effect=fake):
            first = threading.Thread(target=lambda: self.manager._decode(source_path=Path("unused"), generation=generation), name="first")
            second = threading.Thread(target=lambda: self.manager._decode(source_path=Path("unused"), generation=generation), name="background")
            query = threading.Thread(target=lambda: self.manager._decode(b"query"), name="query")
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            query.start()
            deadline = time.monotonic() + 5
            while not self.manager.waiting_queries and time.monotonic() < deadline:
                time.sleep(.005)
            release.set()
            for thread in (first, second, query):
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(events, ["first", "query", "background"])


if __name__ == "__main__":
    unittest.main()

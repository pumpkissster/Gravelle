"""Disposable integration checks for screenshots and substantial image crops.

All illustrations are generated here from fixed random seeds. The fixtures do
not contain user photographs or downloaded content, and query images only live
in memory. A crop retains 45% or more of the source area; tiny isolated details
and repeated patterns deliberately do not identify an entire page.
"""
from contextlib import closing
import hashlib
from io import BytesIO
import math
import random
import sqlite3
import sys
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import Library
from test_image_search import ImageSearchFixture


def textured_picture(seed=701, size=(960, 1280)):
    """Distinct coloured objects and detail distributed over the whole page."""
    randomizer = random.Random(seed)
    width, height = size
    image = Image.new("RGB", size, (238, 229, 220))
    draw = ImageDraw.Draw(image)
    draw.polygon(((0, 0), (width * .56, 0), (width * .2, height), (0, height)),
                 fill=(45, 65, 91))
    draw.ellipse((width * .36, height * .08, width * 1.2, height * .83),
                 fill=(166, 83, 111))
    font = ImageFont.load_default(size=15)
    for row in range(16):
        for column in range(12):
            x = column * width // 12 + randomizer.randrange(5, 21)
            y = row * height // 16 + randomizer.randrange(5, 21)
            w, h = randomizer.randrange(32, 55), randomizer.randrange(30, 57)
            color = tuple(randomizer.randrange(20, 235) for _ in range(3))
            variant = randomizer.randrange(3)
            if variant == 0:
                draw.rectangle((x, y, x + w, y + h), fill=color, outline="white", width=2)
            elif variant == 1:
                draw.ellipse((x, y, x + w, y + h), fill=color, outline="black", width=3)
            else:
                draw.polygon(((x, y + h), (x + w // 3, y), (x + w, y + h // 3)),
                             fill=color, outline="white", width=2)
            draw.line((x + 4, y + h // 3, x + w - 4, y + h - 4),
                      fill=(250, 247, 235), width=randomizer.randrange(2, 5))
            draw.text((x, y + 5), f"{randomizer.randrange(1000):03}",
                      font=font, fill=(10, 10, 15), stroke_width=1,
                      stroke_fill=(245, 245, 240))
            # Corners and unequal line segments make features distinctive after
            # JPEG recompression, without relying on high-frequency pixel noise.
            for _ in range(3):
                dx, dy = x + randomizer.randrange(w), y + randomizer.randrange(h)
                draw.line((dx, dy, dx + randomizer.randrange(5, 17),
                           dy + randomizer.randrange(4, 14)), fill="black", width=2)
    draw.text((32, height - 44), f"Generated fixture {seed}", font=font,
              fill="white", stroke_width=2, stroke_fill="black")
    return image


def encoded(image, *, format_name="PNG", quality=65):
    output = BytesIO()
    image.save(output, format_name, quality=quality)
    return output.getvalue()


def cropped_query(image, area=.45, offset=(.22, .72), longest=900):
    """Keep the stated area, then resize and re-encode as a typical screenshot."""
    factor = math.sqrt(area)
    width, height = round(image.width * factor), round(image.height * factor)
    left = round((image.width - width) * offset[0])
    top = round((image.height - height) * offset[1])
    cropped = image.crop((left, top, left + width, top + height))
    cropped.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return encoded(cropped, format_name="JPEG", quality=57)


def screenshot_query(image):
    """Full source inside browser and reader chrome, rather than a plain resize."""
    content = image.copy()
    content.thumbnail((660, 880), Image.Resampling.LANCZOS)
    screenshot = Image.new("RGB", (1000, 1120), (28, 27, 31))
    draw = ImageDraw.Draw(screenshot)
    draw.rectangle((0, 0, 1000, 42), fill=(244, 240, 243))
    draw.rounded_rectangle((105, 7, 820, 35), radius=10, fill=(218, 215, 219))
    draw.text((125, 14), "localhost / generated screenshot", fill=(60, 60, 65))
    draw.rectangle((0, 42, 1000, 112), fill=(49, 45, 49))
    draw.text((30, 70), "Fixture reader", fill=(240, 230, 237))
    screenshot.paste(content, ((1000 - content.width) // 2, 136))
    draw.rounded_rectangle((72, 1054, 928, 1097), radius=14, fill=(72, 65, 72))
    draw.line((118, 1075, 875, 1075), fill=(234, 80, 152), width=5)
    return encoded(screenshot, format_name="JPEG", quality=68)


def repeated_pattern():
    image = Image.new("RGB", (800, 1000), (236, 231, 227))
    draw = ImageDraw.Draw(image)
    for y in range(0, 1000, 40):
        for x in range(0, 800, 40):
            draw.rectangle((x, y, x + 19, y + 19), fill=(45, 65, 91))
            draw.ellipse((x + 22, y + 22, x + 36, y + 36), fill=(166, 83, 111))
    return encoded(image, format_name="JPEG", quality=65)


class ImageSearchCropTests(ImageSearchFixture):
    def setUp(self):
        super().setUp()
        self.illustration = textured_picture()
        self.source = encoded(self.illustration)
        self.second = encoded(textured_picture(903))
        (self.album / "1.png").write_bytes(self.second)
        (self.album / "2.png").write_bytes(self.source)
        self.app.start_scan(self.library_id)
        self.wait_scan()

    def wait_scan(self):
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)

    def assert_source_page(self, query):
        result = self.manager.query(query)
        self.assertTrue(result["results"], "Screenshot/crop must identify its source page")
        self.assertEqual(result["results"][0]["book_id"], self.book_id)
        self.assertEqual(result["results"][0]["page"], 1)
        self.assertEqual(result["results"][0]["match"], "similar")
        return result

    def test_full_screenshot_with_reader_and_browser_chrome_finds_inner_page(self):
        self.enable()
        self.assert_source_page(screenshot_query(self.illustration))

    def test_light_and_medium_crops_after_resize_and_jpeg_find_inner_page(self):
        self.enable()
        for area, offset in ((.70, (.5, .5)), (.45, (.22, .72))):
            with self.subTest(retained_area=area, offset=offset):
                self.assert_source_page(cropped_query(self.illustration, area, offset))

    def test_unrelated_small_details_and_repeated_pattern_do_not_identify_page(self):
        self.enable()
        queries = {
            "unrelated illustration": encoded(textured_picture(1009), format_name="JPEG"),
            "tiny isolated detail": cropped_query(self.illustration, .08, (.1, .7), longest=500),
            "repeated pattern": repeated_pattern(),
        }
        for name, query in queries.items():
            with self.subTest(query=name):
                self.assertEqual(self.manager.query(query)["results"], [])

    def test_queries_do_not_create_files_or_change_shared_index_or_originals(self):
        self.enable()
        before_rows = self.rows()
        before_files = {path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file()}
        before_sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in self.album.iterdir()}
        query = cropped_query(self.illustration)
        self.assert_source_page(query)
        self.assert_source_page(screenshot_query(self.illustration))
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual({path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file()},
                         before_files)
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in self.album.iterdir()}, before_sources)
        self.assertFalse(any(row["digest"] == hashlib.sha256(query).hexdigest() for row in self.rows()))
        self.assertEqual(list(self.manager.root.iterdir()), [])
        self.assertEqual(self.manager.processes, set())

    def test_crop_candidates_respect_authorized_and_hidden_libraries_before_limit(self):
        private = self.media / "private" / "secret"
        private.mkdir(parents=True)
        (private / "1.png").write_bytes(self.source)
        private_id = self.create_library(private.parent, "未授权写真库")
        private_book = next(book["id"] for book in self.app.listing()["books"]
                            if book["library_id"] == private_id)
        self.enable()
        child = self.child([self.library_id])
        query = cropped_query(self.illustration)
        match_candidates = self.manager._match_candidates
        scored = []
        def checking_candidates(feature, candidates):
            book_ids = {candidate["book_id"] for candidate in candidates}
            self.assertNotIn(private_book, book_ids, "Unauthorized pages must never be scored")
            scored.append(book_ids)
            return match_candidates(feature, candidates)
        with self.app.acting_as(child), patch.object(self.manager, "_match_candidates", checking_candidates):
            result = self.manager.query(query, limit=1)
            self.assertEqual([(item["book_id"], item["page"]) for item in result["results"]],
                             [(self.book_id, 1)])
            self.assertEqual(result["total"], 1)
            self.error(404, self.manager.thumbnail, private_book, 0)
        self.assertTrue(any(self.book_id in group for group in scored))
        with closing(self.app.connect()) as connection, connection:
            connection.execute("INSERT INTO user_hidden_libraries(user_id,library_id) VALUES(?,?)",
                               (child, self.library_id))
        scored.clear()
        with self.app.acting_as(child), patch.object(self.manager, "_match_candidates", checking_candidates):
            self.assertEqual(self.manager.query(query, limit=1)["results"], [])
        self.assertFalse(any(self.book_id in group for group in scored), "Hidden pages must never be scored")
        self.assertEqual({item["book_id"] for item in self.manager.query(query)["results"]},
                         {self.book_id, private_book})

    def test_legacy_hash_index_remains_searchable_while_crop_features_are_backfilled(self):
        self.enable()
        original_rows = self.rows()
        original_preview = self.manager.thumbnail(self.book_id, 1)
        self.app.close()
        # Recreate the actual old version-one table, with no knowledge of the
        # new feature columns. Retain the old hashes, timestamps and previews.
        columns = ("book_id", "page_key", "page_index", "source_version", "book_signature",
                   "algorithm", "status", "digest", "dhash", "phash", "mean", "width", "height",
                   "preview", "error", "updated_at")
        with closing(sqlite3.connect(self.data / "library.sqlite3")) as connection, connection:
            # A real version-one database also lacks any new retrieval tables.
            new_tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'is_*' "
                "AND name NOT IN ('is_pages','is_settings')")]
            for table in new_tables:
                connection.execute('DROP TABLE "' + table.replace('"', '""') + '"')
            connection.execute("DROP TABLE is_pages")
            connection.execute("""CREATE TABLE is_pages (
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE ON UPDATE CASCADE,
                page_key TEXT NOT NULL,page_index INTEGER NOT NULL,source_version TEXT NOT NULL,
                book_signature TEXT NOT NULL,algorithm INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                digest TEXT NOT NULL DEFAULT '',dhash TEXT NOT NULL DEFAULT '',phash TEXT NOT NULL DEFAULT '',
                mean TEXT NOT NULL DEFAULT '[]',width INTEGER NOT NULL DEFAULT 0,height INTEGER NOT NULL DEFAULT 0,
                preview BLOB,error TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL,
                PRIMARY KEY(book_id,page_key))""")
            connection.executemany(f"INSERT INTO is_pages ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                   [tuple(1 if name == "algorithm" else row[name] for name in columns)
                                    for row in original_rows])
        self.app = Library(self.media, self.data, "", 0)
        self.manager = self.app.image_search
        self.manager.auto_start = False
        self.app.transcoding.auto_start = False
        self.assertEqual(self.manager.thumbnail(self.book_id, 1), original_preview)
        exact = self.manager.query(self.source)
        self.assertEqual(exact["results"][0],
                         {"book_id": self.book_id, "page": 1, "match": "exact", "distance": 0})
        resized = self.illustration.resize((480, 640), Image.Resampling.LANCZOS)
        self.assert_source_page(encoded(resized, format_name="JPEG", quality=55))
        self.finish()
        self.assert_source_page(cropped_query(self.illustration))
        self.assertEqual((self.album / "2.png").read_bytes(), self.source)
        self.assertEqual({row["page_key"] for row in self.rows()},
                         {row["page_key"] for row in original_rows})

    def test_crop_match_rechecks_source_version_before_returning_old_page(self):
        self.enable()
        query = cropped_query(self.illustration)
        self.assert_source_page(query)
        match_candidates = self.manager._match_candidates
        def replacing_source(feature, candidates):
            result = match_candidates(feature, candidates)
            (self.album / "2.png").write_bytes(encoded(textured_picture(1181)))
            return result
        with patch.object(self.manager, "_match_candidates", replacing_source):
            self.assertEqual(self.manager.query(query)["results"], [])
        self.error(404, self.manager.thumbnail, self.book_id, 1)
        self.finish()
        self.assertEqual(self.manager.query(query)["results"], [])

    def test_crop_match_rechecks_library_permission_after_geometry_matching(self):
        self.enable()
        child = self.child([self.library_id])
        query = cropped_query(self.illustration)
        match_candidates = self.manager._match_candidates
        def revoking_permission(feature, candidates):
            result = match_candidates(feature, candidates)
            with closing(self.app.connect()) as connection, connection:
                connection.execute("DELETE FROM user_libraries WHERE user_id=?", (child,))
            return result
        with self.app.acting_as(child), patch.object(self.manager, "_match_candidates", revoking_permission):
            self.assertEqual(self.manager.query(query)["results"], [])

    def test_failed_feature_backfill_preserves_basic_search_and_retry_recovers_crops(self):
        self.enable()
        preview = self.manager.thumbnail(self.book_id, 1)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("""UPDATE is_pages SET local_features=NULL,local_sketch=NULL,
                local_version=0,local_state='pending' WHERE book_id=? AND page_index=1""", (self.book_id,))
        with patch.object(self.manager, "_index_page", side_effect=self.manager._error("临时解码失败", 422)):
            self.assertTrue(self.manager.run_once())
        failed = next(row for row in self.rows() if row["page_index"] == 1)
        self.assertEqual((failed["status"], failed["local_state"]), ("ready", "failed"))
        self.assertEqual(self.manager.snapshot()["summary"]["failed"], 1)
        self.app.close()
        self.app = Library(self.media, self.data, "", 0)
        self.manager = self.app.image_search
        self.manager.auto_start = False
        self.app.transcoding.auto_start = False
        self.assertEqual(self.manager.thumbnail(self.book_id, 1), preview)
        self.assertEqual(self.manager.query(self.source)["results"][0]["match"], "exact")
        resized = self.illustration.resize((480, 640), Image.Resampling.LANCZOS)
        self.assert_source_page(encoded(resized, format_name="JPEG", quality=55))
        self.manager.configure({"action": "retry"})
        self.finish()
        self.assertEqual(self.manager.snapshot()["summary"]["failed"], 0)
        self.assert_source_page(cropped_query(self.illustration))

    def test_geometry_matching_keeps_shared_task_slot_until_query_finishes(self):
        self.enable()
        matching = threading.Event()
        release_matching = threading.Event()
        optimization = threading.Event()
        errors = []
        results = []
        match_candidates = self.manager._match_candidates
        def blocking_match(feature, candidates):
            matching.set()
            if not release_matching.wait(10):
                raise AssertionError("Test did not release geometry matching")
            return match_candidates(feature, candidates)
        def query():
            try:
                results.append(self.manager.query(cropped_query(self.illustration)))
            except Exception as error:
                errors.append(error)
        def optimize():
            try:
                with self.app.task_scheduler.slot(resource="transcoding:crop-test") as acquired:
                    if acquired:
                        optimization.set()
            except Exception as error:
                errors.append(error)
        query_thread = threading.Thread(target=query, name="crop-query-test")
        optimization_thread = threading.Thread(target=optimize, name="crop-optimization-test")
        with patch.object(self.manager, "_match_candidates", blocking_match):
            try:
                query_thread.start()
                self.assertTrue(matching.wait(10))
                optimization_thread.start()
                deadline = time.monotonic() + 5
                while (not self.app.task_scheduler.waiting_count(resource="transcoding:crop-test")
                       and time.monotonic() < deadline):
                    time.sleep(.005)
                self.assertEqual(self.app.task_scheduler.waiting_count(resource="transcoding:crop-test"), 1)
                self.assertFalse(optimization.is_set(), "Optimization must wait for geometry at the default limit of one")
            finally:
                release_matching.set()
                query_thread.join(15)
                if optimization_thread.ident is not None:
                    optimization_thread.join(15)
        self.assertFalse(query_thread.is_alive())
        self.assertFalse(optimization_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(optimization.is_set())
        self.assertEqual(results[0]["results"][0]["page"], 1)


if __name__ == "__main__":
    unittest.main()

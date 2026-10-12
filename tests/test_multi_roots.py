"""Multiple allowed media roots, stable catalogue identity, and read boundaries."""
from contextlib import closing
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Library, ReaderError


class MultipleRootTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("SUMI_TEST_TEMP", ROOT / "work" / "multi-root-tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="roots-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.first, self.second = self.base / "first", self.base / "second"
        self.first.mkdir()
        self.second.mkdir()
        self.raw = []
        for index, root in enumerate((self.first, self.second)):
            folder = root / "相同目录" / "作品"
            folder.mkdir(parents=True)
            output = BytesIO()
            Image.new("RGB", (20, 30), (80 + index * 70, 25, 100)).save(output, "PNG")
            self.raw.append(output.getvalue())
            for page in (1, 2):
                (folder / f"{page}.png").write_bytes(self.raw[-1])
        self.data = self.base / "data"
        self.app = Library(self.first, self.data, "", 0, roots=[self.first, self.second])
        self.addCleanup(lambda: self.app.close())

    def wait_scan(self):
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)

    def library(self, paths=None, **options):
        result = self.app.save_library({"name": "测试写真库", "paths": paths or [str(self.first), str(self.second)], **options})
        self.wait_scan()
        return result["library"]["id"]

    def restart(self, first, roots):
        self.app.close()
        self.app = Library(first, self.data, "", 0, roots=roots)

    def assert_error(self, status, function, *args):
        with self.assertRaises(ReaderError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, status)

    def test_browse_each_root_and_keep_single_root_response_compatible(self):
        self.assertEqual(self.app.root, self.first)
        self.assertEqual(self.app.roots, [self.first, self.second])
        result = self.app.directories(str(self.second))
        self.assertEqual(result["root_paths"], [str(self.first), str(self.second)])
        self.assertEqual(result["root_path"], str(self.second))
        self.assertEqual(result["path"], str(self.second))
        self.assertIsNone(result["parent"])
        self.assertEqual(result["directories"], [{"name": "相同目录", "path": str(self.second / "相同目录")}])
        nested = self.app.directories(str(self.second / "相同目录"))
        self.assertEqual(nested["parent"], str(self.second))
        self.restart(self.first, [self.first])
        result = self.app.directories()
        self.assertEqual(result["root_paths"], [str(self.first)])
        self.assertEqual(result["root_path"], str(self.first))
        self.assertIsNone(result["parent"])
        self.assertEqual(self.app.libraries()["root_paths"], [str(self.first)])

    def test_folder_archive_and_cover_reads_use_the_correct_root(self):
        with zipfile.ZipFile(self.second / "压缩作品.cbz", "w") as archive:
            archive.writestr("1.png", self.raw[1])
        self.library()
        books = self.app.listing()["books"]
        self.assertEqual(len(books), 3)
        self.assertEqual(len({book["id"] for book in books}), 3)
        for book in books:
            internal = self.app.book(book["id"], internal=True)
            index = 0 if Path(internal["source_root"]) == self.first else 1
            self.assertEqual(self.app.image(book["id"], 0)[0], self.raw[index])
            cover, mime, _ = self.app.cover(book["id"])
            self.assertEqual(mime, "image/jpeg")
            with Image.open(BytesIO(cover)) as image:
                self.assertLessEqual(image.width, 400)
                self.assertLessEqual(image.height, 600)
                self.assertAlmostEqual(image.getpixel((0, 0))[0], 80 + index * 70, delta=5)

    def test_adding_and_reordering_roots_keeps_released_ids_and_progress(self):
        self.restart(self.first, [self.first])
        library_id = self.library([str(self.first)])
        book = self.app.listing()["books"][0]
        # Released builds used a primary-root-relative namespace. Seed that real
        # form before recording progress, then verify it survives the upgrade.
        old_id = self.app._book_id("source\0相同目录/作品", "folder", library_id)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET id=? WHERE id=?", (old_id, book["id"]))
        self.app.set_progress(old_id, {"page": 1, "completed": False})
        self.app.set_favorite(old_id, {"favorite": True})
        self.restart(self.first, [self.first, self.second])
        self.app.save_library({"paths": [str(self.first), str(self.second)]}, library_id)
        self.wait_scan()
        self.assertEqual(len(self.app.listing()["books"]), 2)
        before = {item["id"] for item in self.app.listing()["books"]}
        self.assertIn(old_id, before)
        self.assertEqual(self.app.book(old_id)["progress"]["page"], 1)
        self.restart(self.second, [self.second, self.first])
        self.app.start_scan(library_id)
        self.wait_scan()
        self.assertEqual({item["id"] for item in self.app.listing()["books"]}, before)
        self.assertEqual(self.app.book(old_id)["progress"]["page"], 1)
        self.assertTrue(self.app.book(old_id)["favorite"])

    def test_removed_root_preserves_records_but_rejects_source_and_cached_cover_reads(self):
        library_id = self.library()
        books = self.app.listing()["books"]
        removed = next(book for book in books if self.app.book(book["id"], internal=True)["source_root"] == str(self.second))
        book_id = removed["id"]
        self.app.set_progress(book_id, {"page": 1, "completed": False})
        self.app.cover(book_id)
        original_hash = hashlib.sha256((self.second / "相同目录" / "作品" / "1.png").read_bytes()).hexdigest()
        self.restart(self.first, [self.first])
        self.app.start_scan(library_id)
        self.wait_scan()
        self.assertEqual(len(self.app.listing()["books"]), 2)
        self.assertEqual(self.app.book(book_id)["progress"]["page"], 1)
        self.assert_error(403, self.app.image, book_id, 0)
        self.assert_error(403, self.app.cover, book_id)
        self.assertTrue(self.app.libraries()["libraries"][0]["errors"])
        self.assertEqual(hashlib.sha256((self.second / "相同目录" / "作品" / "1.png").read_bytes()).hexdigest(), original_hash)
        self.restart(self.first, [self.first, self.second])
        self.app.start_scan(library_id)
        self.wait_scan()
        self.assertEqual(self.app.image(book_id, 0)[0], self.raw[1])
        self.assertEqual(self.app.book(book_id)["progress"]["page"], 1)

    def test_removed_root_rejects_an_uploaded_cover_too(self):
        self.library()
        book = next(item for item in self.app.listing()["books"]
                    if self.app.book(item["id"], internal=True)["source_root"] == str(self.second))
        self.app.save_book_metadata(book["id"], {"cover_source": "upload", "cover_upload":
            {"data": base64.b64encode(self.raw[1]).decode(), "mime": "image/png"}})
        self.assertEqual(self.app.cover(book["id"])[1], "image/jpeg")
        self.restart(self.first, [self.first])
        self.assert_error(403, self.app.cover, book["id"])

    def test_sibling_prefix_data_directory_and_parent_traversal_are_rejected(self):
        sibling = self.base / "first-private"
        sibling.mkdir()
        self.assert_error(403, self.app.directories, str(sibling))
        self.assert_error(400, self.app.directories, str(self.first / ".." / "second"))
        nested_data = self.first / "data"
        nested_data.mkdir()
        self.app.data = nested_data
        self.assert_error(403, self.app.directories, str(nested_data))
        self.assertNotIn("data", [item["name"] for item in self.app.directories()["directories"]])

    def test_windows_drive_boundaries_do_not_use_string_prefixes(self):
        with patch.object(self.app, "roots", [PureWindowsPath("C:/Photos"), PureWindowsPath("D:/Pictures")]):
            self.assertEqual(self.app._allowed_root(PureWindowsPath("D:/Pictures/作品")), PureWindowsPath("D:/Pictures"))
            self.assert_error(403, self.app._allowed_root, PureWindowsPath("D:/Pictures-private/作品"))
            self.assert_error(403, self.app._allowed_root, PureWindowsPath("E:/Pictures/作品"))

    def test_symlink_roots_children_and_media_files_cannot_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "1.png").write_bytes(self.raw[1])
        link = self.second / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symbolic links are unavailable: {error}")
        self.assert_error(403, self.app.directories, str(link))
        self.assertNotIn("linked", [item["name"] for item in self.app.directories(str(self.second))["directories"]])
        child = outside / "nested"
        child.mkdir()
        with patch.object(self.app, "roots", [self.first, link / "nested"]):
            self.assert_error(403, self.app.directories, str(link / "nested"))
        library_id = self.library()
        book = next(item for item in self.app.listing()["books"]
                    if self.app.book(item["id"], internal=True)["source_root"] == str(self.second))
        page = self.second / "相同目录" / "作品" / "1.png"
        page.unlink()
        page.symlink_to(outside / "1.png")
        with self.assertRaises((ReaderError, OSError)):
            self.app.image(book["id"], 0)
        self.app.start_scan(library_id)
        self.wait_scan()
        self.assertEqual(self.app.book(book["id"])["page_count"], 1)

    def test_windows_junction_attributes_reject_roots_and_children_without_privileges(self):
        real_lstat = Path.lstat
        blocked = self.second / "相同目录"

        def linked_lstat(path, *args, **kwargs):
            if path == blocked:
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                    st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
            return real_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", linked_lstat):
            self.assert_error(403, self.app.directories, str(blocked))
            self.assertEqual(self.app.directories(str(self.second))["directories"], [])
            with patch.object(self.app, "roots", [self.first, blocked / "作品"]):
                self.assert_error(403, self.app.directories, str(blocked / "作品"))


if __name__ == "__main__":
    unittest.main()

"""PDF reading, personal state and access boundaries on generated documents."""
from contextlib import closing
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from server import COOKIE_NAME, Library, ReaderServer, ReaderError

ROOT = Path(__file__).resolve().parents[1]


def sample_pdf(path, count=3):
    images = [Image.new("RGB", size, color) for size, color in
              [((240, 360), (215, 35, 45)), ((360, 240), (35, 75, 215)), ((200, 500), (40, 180, 65))][:count]]
    try:
        images[0].save(path, "PDF", save_all=True, append_images=images[1:], resolution=72)
    finally:
        for image in images:
            image.close()


class PdfAPITests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "pdf-api-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.pdf_path = self.media / "2026-10-18 Mixed pages.PDF"
        sample_pdf(self.pdf_path)
        self.source_hash = hashlib.sha256(self.pdf_path.read_bytes()).hexdigest()
        folder = self.media / "Image work"
        folder.mkdir()
        Image.new("RGB", (40, 60), "green").save(folder / "1.png")
        with zipfile.ZipFile(self.media / "Archive.cbz", "w") as archive:
            archive.writestr("1.png", (folder / "1.png").read_bytes())
        (self.media / "broken.pdf").write_bytes(b"%PDF-1.7\nnot a document")
        self.app = Library(self.media, self.root / "data", "", 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = self.app.image_search.auto_start = False
        self.app.register({"username": "pdf-admin", "password": "generated-admin-password"}, "fixture")
        self.library = self.app.save_library({"name": "PDF fixture", "path": str(self.media), "media_type": "photo"})["library"]
        self.wait_scan()
        books = self.app.listing()["books"]
        pdfs = [book for book in books if book["format"] == "pdf"]
        with closing(self.app.connect()) as connection:
            errors = connection.execute("SELECT errors FROM media_libraries WHERE id=?", (self.library["id"],)).fetchone()[0]
        self.assertEqual(len(pdfs), 1, errors)
        self.book = pdfs[0]
        self.child = self.app.save_user({"username": "pdf-child", "password": "generated-child-password",
                                        "library_ids": [self.library["id"]]})["user"]
        self.cookies = {"admin": f"{COOKIE_NAME}={self.app.make_session(1)}",
                        "child": f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"}
        self.server = ReaderServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def wait_scan(self):
        deadline = time.monotonic() + 25
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.app.scanning)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, body=None, actor="admin", headers=None):
        metadata = {"Cookie": self.cookies[actor]} if actor else {}
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            metadata["Content-Type"] = "application/json"
        metadata.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=70)
        try:
            connection.request("POST" if body is not None else "GET", path, body=content, headers=metadata)
            response = connection.getresponse()
            payload = response.read()
            if "application/json" in response.getheader("Content-Type", "") and payload:
                payload = json.loads(payload)
            return response.status, payload, dict(response.headers)
        finally:
            connection.close()

    def test_scan_coexists_with_images_archives_and_reports_bad_document(self):
        books = self.app.listing()["books"]
        self.assertEqual({book["format"] for book in books}, {"pdf", "folder", "cbz"})
        self.assertEqual(self.book["page_count"], 3)
        detail = self.app.book(self.book["id"])
        self.assertEqual(detail["page_sizes"], [[240, 360], [360, 240], [200, 500]])
        self.assertEqual(detail["release_date"], "2026-10-18")
        self.assertEqual(len(detail["page_revision"]), 24)
        with closing(self.app.connect()) as connection:
            errors = json.loads(connection.execute("SELECT errors FROM media_libraries WHERE id=?", (self.library["id"],)).fetchone()[0])
        self.assertTrue(any(error["path"] == "broken.pdf" for error in errors))
        inspected_sizes = []
        original_inspect = self.app.pdf.inspect
        def inspect_source(handle):
            inspected_sizes.append(os.fstat(handle.fileno()).st_size)
            return original_inspect(handle)
        with patch.object(self.app.pdf, "inspect", side_effect=inspect_source):
            self.app.start_scan(self.library["id"])
            self.wait_scan()
        # The broken file must be retried so replacing it can recover. The
        # unchanged valid PDF reuses its page manifest without another parser.
        self.assertEqual(inspected_sizes, [(self.media / "broken.pdf").stat().st_size])
        self.assertEqual(hashlib.sha256(self.pdf_path.read_bytes()).hexdigest(), self.source_hash)

    def test_page_sizes_cover_selection_and_cache_reuse(self):
        base = f"/api/books/{self.book['id']}"
        first = self.request(base + "/pages/0?width=800")
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(first[2]["Content-Type"], "image/jpeg")
        with Image.open(BytesIO(first[1])) as image:
            self.assertGreater(image.height, image.width)
            self.assertLessEqual(max(image.size), 4096)
            self.assertGreater(image.getpixel((image.width // 2, image.height // 2))[0], 180)
        self.assertEqual(self.request(base + "/pages/0?width=800", headers={"If-None-Match": first[2]["ETag"]})[0], 304)
        larger = self.request(base + "/pages/0?width=2400")
        self.assertEqual(larger[0], 200, larger[1])
        with Image.open(BytesIO(first[1])) as smaller, Image.open(BytesIO(larger[1])) as bigger:
            self.assertGreater(bigger.width, smaller.width)
        self.assertEqual(self.request(base + "/metadata", {"cover_page": 1})[0], 200)
        status, cover, _ = self.request(base + "/cover")
        self.assertEqual(status, 200, cover)
        with Image.open(BytesIO(cover)) as image:
            self.assertLessEqual(image.width, 400)
            self.assertLessEqual(image.height, 600)
            self.assertGreater(image.width, image.height)
        for query in ("width=0", "width=4097", "width=1.5", "width=800&width=1600", "path=/tmp", "v=bad"):
            self.assertEqual(self.request(base + "/pages/0?" + query)[0], 400, query)
        self.assertEqual(self.request(base + "/pages/3")[0], 404)
        self.assertEqual(self.request(base + "/document")[0], 404)

    def test_personal_progress_bookmarks_favorites_and_access_revocation(self):
        base = f"/api/books/{self.book['id']}"
        self.assertEqual(self.request(base + "/pages/0", actor=None)[0], 401)
        self.assertEqual(self.request(base + "/progress", {"page": 1, "completed": False}, "child")[0], 200)
        self.assertEqual(self.request(base + "/favorite", {"favorite": True}, "child")[0], 200)
        self.assertEqual(self.request(base + "/bookmarks", {"page": 1, "note": "Private PDF bookmark"}, "child")[0], 201)
        child = self.request(base, actor="child")[1]
        admin = self.request(base)[1]
        self.assertEqual(child["progress"]["page"], 1)
        self.assertTrue(child["favorite"])
        self.assertEqual(len(child["bookmarks"]), 1)
        self.assertIsNone(admin["progress"])
        self.assertEqual(admin["bookmarks"], [])
        page = self.request(base + "/pages/0", actor="child")
        self.assertEqual(page[0], 200)
        self.app.save_user({"library_ids": []}, self.child["id"])
        # Use a new valid session after grant changes revoke old sessions.
        self.cookies["child"] = f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"
        self.assertEqual(self.request(base + "/pages/0", actor="child", headers={"If-None-Match": page[2]["ETag"]})[0], 404)
        self.assertEqual(self.request(base + "/cover", actor="child")[0], 404)
        self.assertEqual(self.request("/api/cache", actor="child")[0], 403)

    def test_cache_setting_clear_and_removed_work_leave_pdf_untouched(self):
        base = f"/api/books/{self.book['id']}"
        page = self.request(base + "/pages/0")
        self.assertEqual(page[0], 200, page[1])
        cache = self.request("/api/cache")[1]
        pdf = next(category for category in cache["categories"] if category["kind"] == "pdf")
        self.assertTrue(pdf["can_clear"])
        self.assertGreater(pdf["bytes"], 0)
        self.assertEqual(self.request("/api/cache/clear", {"kind": "pdf", "confirm": True}, "child")[0], 403)
        self.assertEqual(self.request("/api/cache/clear", {"kind": "pdf", "confirm": True})[0], 200)
        self.assertEqual(self.app.pdf.snapshot()["files"], 0)
        self.assertEqual(self.request(base + "/pages/0")[0], 200)
        self.app.batch_books({"action": "remove", "ids": [self.book["id"]]})
        self.assertEqual(self.request(base + "/pages/0")[0], 404)
        self.assertEqual(self.app.pdf.snapshot()["files"], 0)
        self.assertEqual(hashlib.sha256(self.pdf_path.read_bytes()).hexdigest(), self.source_hash)

    def test_pdf_is_not_eagerly_rendered_by_optimization_or_image_index(self):
        internal = self.app.book(self.book["id"], internal=True)
        self.assertEqual(self.app.transcoding.inspect_book(internal), [])
        with closing(self.app.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tc_pages WHERE book_id=?", (self.book["id"],)).fetchone()[0], 0)
        self.app.image_search.configure({"enabled": True})
        with closing(self.app.connect()) as connection:
            generation = self.app.image_search._settings(connection)["generation"]
        # A full catalogue reconciliation must never attempt PDF page rendering.
        with patch.object(self.app.pdf, "image", side_effect=AssertionError("Eager PDF rendering")):
            self.app.image_search._sync_catalogue(generation)
        with closing(self.app.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM is_pages WHERE book_id=?", (self.book["id"],)).fetchone()[0], 0)

    def test_pdf_cache_limit_migration_and_zero_disables_persistence(self):
        settings = self.request("/api/cache")[1]["settings"]
        self.assertEqual(settings["pdf_limit_mb"], 1024)
        status, saved, _ = self.request("/api/cache/settings", {
            "expected_revision": settings["revision"], "cover_limit_mb": 64, "pdf_limit_mb": 0})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["settings"]["pdf_limit_mb"], 0)
        base = f"/api/books/{self.book['id']}"
        self.assertEqual(self.request(base + "/pages/0")[0], 200)
        self.assertEqual(self.app.pdf.snapshot()["files"], 0)
        # An older client changing only its cover limit preserves the PDF limit.
        status, legacy, _ = self.request("/api/cache/settings", {
            "expected_revision": saved["settings"]["revision"], "cover_limit_mb": 128})
        self.assertEqual(status, 200, legacy)
        self.assertEqual(legacy["settings"]["pdf_limit_mb"], 0)
        self.assertEqual(self.request("/api/cache/settings", {
            "expected_revision": settings["revision"], "cover_limit_mb": 64, "pdf_limit_mb": 1})[0], 409)
        self.assertEqual(self.request("/api/cache/settings", {
            "expected_revision": legacy["settings"]["revision"], "cover_limit_mb": 64, "pdf_limit_mb": True})[0], 400)

    def test_source_change_requires_scan_invalidates_cache_and_keeps_identity(self):
        base = f"/api/books/{self.book['id']}"
        old = self.request(base + "/pages/0")
        self.assertEqual(old[0], 200, old[1])
        self.app.set_progress(self.book["id"], {"page": 2, "completed": False})
        sample_pdf(self.pdf_path, 1)
        self.assertEqual(self.request(base + "/pages/0", headers={"If-None-Match": old[2]["ETag"]})[0], 409)
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        new = self.app.book(self.book["id"])
        self.assertEqual(new["page_count"], 1)
        self.assertEqual(new["progress"]["page"], 0)
        self.assertNotEqual(new["page_revision"], self.book["page_revision"])
        self.assertEqual(self.app.pdf.snapshot()["files"], 0)
        self.assertEqual(self.request(base + "/pages/0")[0], 200)


if __name__ == "__main__":
    unittest.main()

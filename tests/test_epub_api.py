"""EPUB catalogue, chapter locations and permission boundaries on synthetic books."""
from contextlib import closing
import base64
import hashlib
import http.client
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image
from server import COOKIE_NAME, Library, ReaderServer

ROOT = Path(__file__).resolve().parents[1]


def sample_epub(path, title="EPUB 测试读本", count=2, cover=True, pictures=None, assets=None, svg=False):
    image = BytesIO()
    Image.new("RGB", (200, 300), "#d8498b").save(image, "PNG")
    chapters = "".join(f'<item id="c{i}" href="chapter{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(count))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(count))
    nav = "".join(f'<li><a href="chapter{i}.xhtml#start">章节 {i+1}</a></li>' for i in range(count))
    properties = ' properties="cover-image"' if cover else ''
    pictures = pictures if pictures is not None else [["cover.png"] for _ in range(count)]
    assets = assets or {}
    extra = ''.join(f'<item id="image{i}" href="{name}" media-type="image/png"/>' for i, name in enumerate(assets))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OPS/book.opf", f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">generated-fixture</dc:identifier><dc:title>{title}</dc:title><dc:creator>测试作者</dc:creator><dc:language>zh-CN</dc:language></metadata><manifest>{chapters}<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="image" href="cover.png" media-type="image/png"{properties}/>{extra}</manifest><spine>{spine}</spine></package>')
        z.writestr("OPS/nav.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>{nav}</ol></nav></body></html>')
        z.writestr("OPS/cover.png", image.getvalue())
        for name, data in assets.items():
            z.writestr("OPS/" + name, data)
        for i in range(count):
            paragraphs = "".join(f'<p>第 {i+1} 章，第 {j+1} 段。这是供本地阅读验证的示例文字。</p>' for j in range(50))
            images = ''.join((f'<svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="{name}"/></svg>' if svg else f'<img src="{name}"/>') for name in pictures[i])
            z.writestr(f"OPS/chapter{i}.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>章节 {i+1}</title><script>alert(1)</script></head><body><h1 id="start">章节 {i+1}</h1>{paragraphs}{images}<a href="chapter0.xhtml#start">返回第一章</a><img src="https://example.invalid/track.png"/></body></html>')


class EpubAPITests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "epub-api-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.path = self.media / "sample.EPUB"
        sample_epub(self.path)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        folder = self.media / "Image work"
        folder.mkdir()
        Image.new("RGB", (30, 40), "green").save(folder / "1.png")
        (self.media / "broken.epub").write_bytes(b"not epub")
        self.app = Library(self.media, self.root / "data", "", 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = self.app.image_search.auto_start = False
        self.app.register({"username":"epub-admin", "password":"generated-admin-password"}, "fixture")
        self.library = self.app.save_library({"name":"EPUB fixture", "path":str(self.media), "media_type":"photo"})["library"]
        self.wait_scan()
        books = self.app.listing()["books"]
        epubs = [b for b in books if b["format"] == "epub"]
        self.assertEqual(len(epubs), 1, self.app.listing())
        self.book = epubs[0]
        self.child = self.app.save_user({"username":"epub-child", "password":"generated-child-password", "library_ids":[self.library["id"]]})["user"]
        self.cookies = {"admin":f"{COOKIE_NAME}={self.app.make_session(1)}", "child":f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"}
        self.server = ReaderServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def wait_scan(self):
        end = time.monotonic()+20
        while self.app.scanning and time.monotonic()<end:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, body=None, actor="admin", headers=None):
        metadata = {"Cookie":self.cookies[actor]} if actor else {}
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            metadata["Content-Type"] = "application/json"
        metadata.update(headers or {})
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)) as connection:
            connection.request("POST" if body is not None else "GET", path, content, metadata)
            response = connection.getresponse()
            data = response.read()
            if "application/json" in response.getheader("Content-Type", "") and data:
                data = json.loads(data)
            return response.status, data, dict(response.headers)

    @property
    def base(self):
        return f"/api/books/{self.book['id']}"

    def test_scan_chapters_and_skip_unchanged_package(self):
        detail = self.request(self.base)[1]
        self.assertEqual(detail["page_count"], 2)
        self.assertEqual(len(detail["epub_chapters"]), 2)
        self.assertEqual(detail["document_author"], "测试作者")
        self.assertTrue(detail["epub_toc"])
        inspected = []
        original = self.app.epub.inspect
        def record(handle):
            inspected.append(handle)
            return original(handle)
        with patch.object(self.app.epub, "inspect", side_effect=record):
            self.app.start_scan(self.library["id"])
            self.wait_scan()
        self.assertEqual(len(inspected), 1)  # Only the invalid package retries.
        self.assertEqual(self.book["id"], next(b["id"] for b in self.app.listing()["books"] if b["format"]=="epub"))

    def test_inert_html_resources_cover_and_no_raw_download(self):
        status, chapter, _ = self.request(self.base+"/epub/chapters/1?v="+self.book["page_revision"])
        self.assertEqual(status, 200, chapter)
        self.assertNotIn("<script", chapter["html"])
        self.assertNotIn("example.invalid", chapter["html"])
        self.assertIn("data-epub-index", chapter["html"])
        image = re.search(r'src="([^"]+/epub/resources/[^" ]+)"', chapter["html"]).group(1)
        status, data, headers = self.request(image)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(self.request(image, headers={"If-None-Match":headers["ETag"]})[0], 304)
        self.assertEqual(self.request(self.base+"/cover")[0], 200)
        for suffix in ("/document", "/epub", "/epub/resources/OPS/cover.png", "/epub/chapters/2"):
            self.assertEqual(self.request(self.base+suffix)[0], 404, suffix)
        for query in ("?v=bad", "?v="+"0"*24, "?path=/etc/passwd", "?v=a&v=b"):
            self.assertIn(self.request(self.base+"/epub/chapters/0"+query)[0], (400,409))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), self.digest)

    def test_personal_locations_bookmarks_and_authorization_before_304(self):
        result = self.request(self.base+"/progress", {"page":1,"position":.625,"completed":False}, "child")
        self.assertEqual(result[0], 200, result[1])
        mark = self.request(self.base+"/bookmarks", {"page":1,"position":.625,"note":"Saved chapter"}, "child")
        self.assertEqual(mark[0], 201, mark[1])
        self.assertNotIn("position", mark[1]["bookmark"])
        self.assertEqual(self.request(self.base, actor="child")[1]["progress"]["page"], 1)
        self.assertIsNone(self.request(self.base)[1]["progress"])
        self.assertEqual(self.request(self.base)[1]["bookmarks"], [])
        image_url = re.search(r'src="([^"]+/epub/resources/[^" ]+)"', self.request(self.base+"/epub/chapters/0")[1]["html"]).group(1)
        image = self.request(image_url, actor="child")
        self.assertEqual(self.request(image_url, actor=None)[0], 401)
        self.app.save_user({"library_ids":[]}, self.child["id"])
        self.cookies["child"] = f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"
        self.assertEqual(self.request(image_url, actor="child", headers={"If-None-Match":image[2]["ETag"]})[0], 404)
        self.assertEqual(self.request(self.base+"/epub/chapters/0", actor="child")[0], 404)

    def test_position_validation_and_database_retains_state(self):
        for position in (-1, 1.1, "0.5", True, None):
            self.assertEqual(self.request(self.base+"/progress", {"page":0,"position":position,"completed":False})[0],400)
            self.assertEqual(self.request(self.base+"/bookmarks", {"page":0,"position":position})[0],400)
        self.assertEqual(self.request(self.base+"/progress", {"page":0,"position":.75,"completed":False})[0],200)
        with closing(self.app.connect()) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(c.execute("SELECT position FROM progress WHERE book_id=?",(self.book["id"],)).fetchone()[0],0)

    def test_bmp_declared_cover_is_safely_normalized_for_cover_cards(self):
        image = BytesIO()
        Image.new("RGB", (200, 300), "purple").save(image, "BMP")
        with zipfile.ZipFile(self.path) as source:
            entries = [(item, source.read(item)) for item in source.infolist()]
        with zipfile.ZipFile(self.path, "w") as target:
            for item, data in entries:
                target.writestr(item, image.getvalue() if item.filename == "OPS/cover.png" else data)
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        status, data, headers = self.request(self.base+"/cover")
        self.assertEqual(status, 200, data)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        with Image.open(BytesIO(data)) as cover:
            self.assertEqual(cover.format, "JPEG")

    def test_reopen_preserves_existing_account_and_chapter_offset(self):
        self.request(self.base+"/progress", {"page":1,"position":.625,"completed":False}, "child")
        self.request(self.base+"/bookmarks", {"page":1,"position":.625,"note":"Persisted"}, "child")
        self.stop_server()
        self._cleanups = [item for item in self._cleanups if item[0] != self.stop_server]
        self.app.close()
        reopened = Library(self.media, self.root / "data", "", 0)
        try:
            with closing(reopened.connect()) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
                self.assertEqual(c.execute("SELECT position FROM progress WHERE user_id=? AND book_id=?", (self.child["id"],self.book["id"])).fetchone()[0],0)
                self.assertEqual(c.execute("SELECT position FROM bookmarks WHERE user_id=? AND book_id=?", (self.child["id"],self.book["id"])).fetchone()[0],0)
                self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            reopened.close()

    def test_no_optimization_or_image_index_and_no_cover_placeholder_failure(self):
        with closing(self.app.connect()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tc_pages WHERE book_id=?",(self.book["id"],)).fetchone()[0],0)
        self.app.image_search.configure({"enabled": True})
        with closing(self.app.connect()) as c:
            generation = c.execute("SELECT generation FROM is_settings WHERE id=1").fetchone()[0]
        self.app.image_search._sync_catalogue(generation)
        with closing(self.app.connect()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM is_pages WHERE book_id=?",(self.book["id"],)).fetchone()[0],0)
        sample_epub(self.path, cover=False)
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        self.assertEqual(self.request(self.base+"/cover")[0],200)

    def test_source_replacement_requires_scan_and_clamps_locations(self):
        self.request(self.base+"/progress", {"page":1,"position":.75,"completed":False})
        sample_epub(self.path, title="Changed", count=1)
        self.assertEqual(self.request(self.base+"/epub/chapters/0")[0],409)
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        latest = self.request(self.base)[1]
        self.assertNotEqual(latest["page_revision"],self.book["page_revision"])
        self.assertEqual(latest["progress"]["page"],0)
        self.assertEqual(self.request(self.base+"/epub/chapters/0")[0],200)

    def test_picture_pages_follow_spine_and_svg_order_and_use_page_covers(self):
        assets = {}
        for name, color in (("z-last.png", "red"), ("a-first.png", "green"), ("m.png", "blue")):
            output = BytesIO()
            Image.new("RGB", (80, 120), color).save(output, "PNG")
            assets[name] = output.getvalue()
        order = ["z-last.png", "a-first.png", "m.png"]
        sample_epub(self.path, count=3, pictures=[order[:2], [], order[2:]], assets=assets, svg=True)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        detail = self.request(self.base)[1]
        self.assertEqual((detail["page_count"], detail["reader_kind"]), (3, "images"))
        for page, name in enumerate(order):
            status, data, headers = self.request(self.base + f"/pages/{page}")
            self.assertEqual((status, data, headers["Content-Type"]), (200, assets[name], "image/png"))
            self.assertEqual(self.request(self.base + f"/pages/{page}", headers={"If-None-Match": headers["ETag"]})[0], 304)
            self.assertEqual(self.request(self.base + f"/pages/{page}", actor=None)[0], 401)
        self.assertEqual(self.request(self.base + "/pages/3")[0], 404)
        self.assertEqual(self.request(self.base + "/metadata", {"cover_source": "page", "cover_page": 2})[0], 200)
        status, data, headers = self.request(self.base + "/cover")
        self.assertEqual(status, 200)
        with Image.open(BytesIO(data)) as cover:
            r, g, b = cover.convert("RGB").getpixel((20, 20))
            self.assertGreater(b, max(r, g) + 100)
        marked = self.request(self.base + "/bookmarks", {"page": 2, "note": "第三张图片"}, "child")
        self.assertEqual(marked[0], 201)
        self.assertEqual(self.request(self.base + "/progress", {"page": 2, "completed": True}, "child")[0], 200)
        self.assertEqual(self.request(self.base, actor="child")[1]["progress"]["page"], 2)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)
        # The same image API used by ordinary photo shares also serves EPUB.
        share = self.request("/api/shares", {"book_id": self.book["id"], "mode": "time", "days": 1})
        self.assertEqual(share[0], 201, share[1])
        token = share[1]["share"]["token"]
        status, unlocked, headers = self.request(f"/api/share/{token}/unlock", {"password": share[1]["password"]}, actor=None)
        self.assertEqual(status, 200, unlocked)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, packet, _ = self.request(f"/api/share/{token}/pages/2", actor=None, headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = base64.urlsafe_b64decode(unlocked["key"] + "=")
        aad = ("gravelle-share-v1:" + token + ":2").encode()
        self.assertEqual(AESGCM(key).decrypt(packet[4:16], packet[17:], packet[:17] + aad), assets[order[2]])

    def test_old_chapter_cache_and_all_user_locations_upgrade_once(self):
        sample_epub(self.path, count=3, pictures=[["cover.png"] * 2, [], ["cover.png"] * 3], svg=True)
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        self.request(self.base + "/progress", {"page": 0, "completed": False})
        self.request(self.base + "/progress", {"page": 2, "completed": False}, "child")
        self.request(self.base + "/bookmarks", {"page": 1, "note": "空白文档书签"})
        self.request(self.base + "/bookmarks", {"page": 2, "note": "图片文档书签"})
        with closing(self.app.connect()) as c, c:
            old = json.loads(c.execute("SELECT epub_metadata FROM books WHERE id=?", (self.book["id"],)).fetchone()[0])
            old.pop("metadata_version")
            c.execute("UPDATE books SET epub_metadata=?,page_count=3 WHERE id=?", (json.dumps(old), self.book["id"]))
            c.execute("UPDATE progress SET position=.75 WHERE user_id=1 AND book_id=?", (self.book["id"],))
            c.execute("UPDATE progress SET position=.8 WHERE user_id=? AND book_id=?", (self.child["id"], self.book["id"]))
        with patch.object(self.app.epub, "inspect", wraps=self.app.epub.inspect) as inspect:
            self.app.start_scan(self.library["id"])
            self.wait_scan()
            self.assertGreaterEqual(inspect.call_count, 1)
        detail = self.request(self.base)[1]
        self.assertEqual((detail["page_count"], detail["progress"]["page"]), (5, 1))
        self.assertEqual(self.request(self.base, actor="child")[1]["progress"]["page"], 4)
        self.assertEqual(len(detail["bookmarks"]), 1)
        self.assertEqual(detail["bookmarks"][0]["page"], 2)
        self.assertIn("空白文档书签", detail["bookmarks"][0]["note"])
        self.assertIn("图片文档书签", detail["bookmarks"][0]["note"])
        self.app.start_scan(self.library["id"])
        self.wait_scan()
        self.assertEqual(self.request(self.base, actor="child")[1]["progress"]["page"], 4)
        with closing(self.app.connect()) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()

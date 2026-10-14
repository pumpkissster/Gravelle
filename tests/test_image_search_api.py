"""Real HTTP checks for private, bounded image queries and shared indexing.

All sources, accounts, HTTP listeners and databases are disposable. The small
geometric fixtures deliberately exercise inner pages and CBZ entries instead
of treating a cover-only index or solid-colour matches as a successful search.
"""
from contextlib import closing
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
import zlib

from PIL import Image, ImageDraw

from server import COOKIE_NAME, Library, ReaderServer
from transcoding import DEFAULT_RULES


ROOT = Path(__file__).resolve().parents[1]


def geometric_image(variant=0, *, size=(96, 128), format_name="PNG"):
    image = Image.new("RGB", size, (244, 235, 240))
    drawing = ImageDraw.Draw(image)
    width, height = size
    if variant == 0:
        drawing.rectangle((0, 0, width // 3, height), fill=(30, 55, 160))
        drawing.ellipse((width // 4, height // 4, width - 5, height - 5), fill=(229, 72, 142))
        drawing.line((0, height - 1, width - 1, 0), fill=(253, 232, 60), width=7)
    else:
        drawing.rectangle((width // 2, 0, width - 1, height // 2), fill=(30, 155, 80))
        drawing.polygon(((0, 0), (width // 2, height - 1), (0, height - 1)), fill=(170, 40, 65))
        drawing.line((0, height // 3, width - 1, height // 3), fill=(40, 40, 40), width=11)
    output = BytesIO()
    image.save(output, format_name, quality=82)
    return output.getvalue()


def oversized_png_header():
    """Claim > 100 MP without allocating an enormous image in the test."""
    data = geometric_image()
    payload = struct.pack("!II", 10001, 10001) + data[24:29]
    return data[:16] + payload + struct.pack("!I", zlib.crc32(b"IHDR" + payload)) + data[33:]


class ImageSearchAPITests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("SUMI_TEST_TEMP", ROOT / "work" / "image-search-api-tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="is-api-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.data = self.root / "data"
        self.visible_path = self.media / "allowed"
        self.private_path = self.media / "private"
        self.album = self.visible_path / "inner-page-album"
        self.secret_album = self.private_path / "secret-album"
        self.album.mkdir(parents=True)
        self.secret_album.mkdir(parents=True)
        self.query_image = geometric_image()
        (self.album / "1.png").write_bytes(geometric_image(1))
        (self.album / "2.png").write_bytes(self.query_image)
        (self.secret_album / "1.png").write_bytes(self.query_image)
        with zipfile.ZipFile(self.visible_path / "archive.cbz", "w") as archive:
            archive.writestr("inside/page.png", self.query_image)
        self.source_hashes = self.hash_sources()
        self.app = Library(self.media, self.data, "", 0)
        self.addCleanup(self.app.close)
        self.app.register({"username": "image-admin", "password": "temporary-image-admin-pass"}, "test")
        self.app.transcoding.auto_start = False
        self.app.image_search.auto_start = False
        self.visible = self.app.save_library({"name": "Visible library", "path": str(self.visible_path),
                                              "media_type": "photo"})["library"]
        self.wait_scan()
        self.private = self.app.save_library({"name": "Private library", "path": str(self.private_path),
                                              "media_type": "photo"})["library"]
        self.wait_scan()
        books = self.app.listing()["books"]
        self.album_book = next(book for book in books if book["title"] == "inner-page-album")
        self.secret_book = next(book for book in books if book["title"] == "secret-album")
        self.archive_book = next(book for book in books if book["format"] != "folder")
        self.child = self.app.save_user({"username": "image-child", "password": "temporary-image-child-pass",
                                         "library_ids": [self.visible["id"]]})["user"]
        self.cookies = {"admin": f"{COOKIE_NAME}={self.app.make_session(1)}",
                        "child": f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"}
        self.server = ReaderServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)

    def stop_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wait_scan(self):
        deadline = time.monotonic() + 15
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning, "Disposable catalogue scan timed out")

    def hash_sources(self):
        return {path.relative_to(self.media).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.media.rglob("*") if path.is_file()}

    def request(self, path, data=None, *, body=None, actor="admin", method=None, headers=None,
                manual_headers=False):
        request_headers = {}
        if actor:
            request_headers["Cookie"] = self.cookies[actor]
        if data is not None:
            body = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        method = method or ("POST" if body is not None else "GET")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=70)
        try:
            if manual_headers:
                connection.putrequest(method, path)
                for name, value in request_headers.items():
                    connection.putheader(name, value)
                connection.endheaders()
                if body:
                    connection.send(body)
            else:
                connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            metadata = dict(response.headers)
            if content and "application/json" in metadata.get("Content-Type", ""):
                content = json.loads(content)
            return response.status, content, metadata
        finally:
            connection.close()

    def query(self, content=None, *, actor="admin", headers=None, path="/api/image-search/query"):
        return self.request(path, body=self.query_image if content is None else content, actor=actor,
                            headers={"Content-Type": "image/png", **(headers or {})})

    def enable_index(self):
        status, payload, _ = self.request("/api/image-search/settings", {"enabled": True})
        self.assertEqual(status, 200, payload)
        for _ in range(30):
            if not self.app.image_search.run_once():
                break
        else:
            self.fail("Disposable image index did not settle")
        status, payload, _ = self.request("/api/image-search/status")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["summary"]["pending"], 0, payload)
        self.assertEqual(payload["summary"]["failed"], 0, payload)
        self.assertGreaterEqual(payload["summary"]["indexed"], 4, payload)

    def assert_private_absent(self, payload):
        serialized = json.dumps(payload)
        for value in (self.private["id"], self.private["name"], self.secret_book["id"],
                      self.secret_book["title"], str(self.private_path), str(self.data)):
            self.assertNotIn(value, serialized)

    def test_login_and_admin_boundaries_include_head_and_mutations(self):
        for method in ("GET", "HEAD"):
            self.assertEqual(self.request("/api/image-search/status", actor=None, method=method)[0], 401)
        self.assertEqual(self.query(actor=None)[0], 401)
        for path, body in (("/api/image-search/settings", {"enabled": True}),
                           ("/api/image-search/actions", {"action": "clear"})):
            self.assertEqual(self.request(path, body, actor=None)[0], 401)
            self.assertEqual(self.request(path, body, actor="child")[0], 403)
        status, payload, _ = self.request("/api/image-search/status", actor="child")
        self.assertEqual(status, 200, payload)
        self.assertEqual(set(payload), {"enabled", "available", "indexing"})

    def test_disabled_feature_rejects_query_without_modifying_sources(self):
        self.assertFalse(self.request("/api/image-search/status")[1]["enabled"])
        self.assertEqual(self.query()[0], 409)
        self.assertEqual(self.query(actor="child")[0], 409)
        self.assertEqual(self.hash_sources(), self.source_hashes)

    def test_exact_inner_page_and_archive_results_respect_permissions_before_ranking(self):
        self.enable_index()
        status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 200, payload)
        self.assert_private_absent(payload)
        exact = {(item["book"]["id"], item["page"]) for item in payload["results"] if item["match"] == "exact"}
        self.assertIn((self.album_book["id"], 1), exact, "The index must contain inner pages")
        self.assertIn((self.archive_book["id"], 0), exact, "CBZ inner pages must be searchable")
        self.assertTrue(all(item["book"]["library_id"] == self.visible["id"] for item in payload["results"]))
        self.assertTrue(all("source_root" not in item["book"] and "path" not in item["book"] for item in payload["results"]))
        if "total" in payload:
            self.assertEqual(payload["total"], len(payload["results"]))
        status, administrator, _ = self.query()
        self.assertEqual(status, 200, administrator)
        self.assertIn(self.secret_book["id"], {item["book"]["id"] for item in administrator["results"]})
        self.assertEqual(self.hash_sources(), self.source_hashes)

    def test_resized_recompressed_query_finds_the_original_without_retaining_upload(self):
        self.enable_index()
        with Image.open(BytesIO(self.query_image)) as original:
            output = BytesIO()
            original.resize((48, 64), Image.Resampling.LANCZOS).save(output, "JPEG", quality=74)
            upload = output.getvalue()
        status, payload, _ = self.query(upload, actor="child", headers={"Content-Type": "image/jpeg"})
        self.assertEqual(status, 200, payload)
        self.assertIn((self.album_book["id"], 1), {(item["book"]["id"], item["page"]) for item in payload["results"]})
        self.assertTrue(all(item["match"] == "similar" for item in payload["results"]))
        for path in self.data.rglob("*"):
            if path.is_file() and path != self.app.service_lock.path:
                self.assertNotIn(upload, path.read_bytes(), "Uploaded query bytes must not be retained")
        self.assertEqual(self.hash_sources(), self.source_hashes)

    def test_hidden_libraries_filter_results_and_thumbnails_for_every_account(self):
        self.enable_index()
        thumb = f"/api/image-search/thumbnail/{self.album_book['id']}/1"
        status, content, metadata = self.request(thumb, actor="child")
        self.assertEqual(status, 200)
        self.assertTrue(metadata["Content-Type"].startswith("image/"))
        with Image.open(BytesIO(content)) as image:
            self.assertLessEqual(max(image.size), 512)
        status, payload, _ = self.request("/api/my-libraries", {"visible_library_ids": []}, actor="child")
        self.assertEqual(status, 200, payload)
        status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["results"], [])
        if "total" in payload:
            self.assertEqual(payload["total"], 0)
        for method in ("GET", "HEAD"):
            self.assertEqual(self.request(thumb, actor="child", method=method,
                                          headers={"If-None-Match": metadata.get("ETag", "*")})[0], 404)
        # Direct authorized reading is still allowed; hiding is a search/list preference.
        self.assertEqual(self.request(f"/api/books/{self.album_book['id']}/pages/1", actor="child")[0], 200)
        self.assertEqual(self.request("/api/my-libraries", {"visible_library_ids": [self.private["id"]]})[0], 200)
        administrator = self.query()[1]
        self.assertEqual({item["book"]["library_id"] for item in administrator["results"]}, {self.private["id"]})
        self.assertEqual(self.request(thumb)[0], 404)

    def test_revoked_grants_disabled_accounts_and_logout_immediately_stop_search(self):
        self.enable_index()
        thumb = f"/api/image-search/thumbnail/{self.album_book['id']}/1"
        self.assertEqual(self.query(actor="child")[0], 200)
        user_route = f"/api/users/{self.child['id']}"
        self.assertEqual(self.request(user_route, {"library_ids": []})[0], 200)
        # Changing grants also revokes the old login session.
        self.assertEqual(self.query(actor="child")[0], 401)
        self.cookies["child"] = f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"
        self.assertEqual(self.query(actor="child")[1]["results"], [])
        self.assertEqual(self.request(thumb, actor="child")[0], 404)
        self.assertEqual(self.request(user_route, {"enabled": False})[0], 200)
        self.assertEqual(self.query(actor="child")[0], 401)
        self.assertEqual(self.request("/api/image-search/status", actor="child")[0], 401)
        self.assertEqual(self.request(thumb, actor="child")[0], 401)
        self.assertEqual(self.request("/api/logout", {}, actor="admin")[0], 200)
        self.assertEqual(self.query()[0], 401)

    def test_unpublished_optimization_decisions_are_not_searchable_or_previewable(self):
        self.enable_index()
        rules = {**DEFAULT_RULES, "enabled": True, "file_size_mb": 0,
                 "max_source_pixels": 1, "max_source_side": 0, "min_free_space_mb": 0}
        self.app.transcoding.save_rules({"rules": rules, "expected_revision": self.app.transcoding.settings()[1]})
        self.assertEqual(self.query(actor="child")[1]["results"], [])
        self.assertEqual(self.request(f"/api/image-search/thumbnail/{self.album_book['id']}/1", actor="child")[0], 404)
        self.app.transcoding.actions({"action": "original", "ids": [self.album_book["id"]]})
        status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 200, payload)
        self.assertEqual({item["book"]["id"] for item in payload["results"]}, {self.album_book["id"]})
        self.assertEqual(self.hash_sources(), self.source_hashes)

    def test_source_change_or_removal_invalidates_results_and_thumbnail_before_rescan(self):
        self.enable_index()
        thumb = f"/api/image-search/thumbnail/{self.album_book['id']}/1"
        self.assertEqual(self.request(thumb, actor="child")[0], 200)
        (self.album / "2.png").write_bytes(geometric_image(1, size=(97, 129)))
        status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 200, payload)
        self.assertNotIn((self.album_book["id"], 1), {(item["book"]["id"], item["page"]) for item in payload["results"]})
        self.assertIn(self.request(thumb, actor="child")[0], (404, 409, 422))
        self.assertEqual(self.request(f"/api/books/{self.archive_book['id']}/remove", {})[0], 200)
        status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 200, payload)
        self.assertNotIn(self.archive_book["id"], {item["book"]["id"] for item in payload["results"]})
        self.assertEqual(self.request(f"/api/image-search/thumbnail/{self.archive_book['id']}/0", actor="child")[0], 404)

    def test_cross_origin_requests_and_external_parameters_are_rejected(self):
        for path, data in (("/api/image-search/settings", {"enabled": True}),
                           ("/api/image-search/actions", {"action": "rebuild"})):
            self.assertEqual(self.request(path, data, headers={"Origin": "https://example.invalid"})[0], 403)
            self.assertEqual(self.request(path + "?path=/outside", data)[0], 400)
        self.assertEqual(self.query(headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.query(path="/api/image-search/query?url=https://example.invalid/image.png")[0], 400)
        self.assertEqual(self.request("/api/image-search/status?path=/outside")[0], 400)
        self.assertEqual(self.request(f"/api/image-search/thumbnail/{self.album_book['id']}/1?path=/outside")[0], 400)
        self.assertEqual(self.request("/api/image-search/thumbnail/../../outside/0")[0], 404)

    def test_upload_headers_are_bounded_before_reading_and_json_limits_stay_small(self):
        cases = (({}, 411), ({"Content-Length": "abc"}, 400),
                 ({"Content-Length": "-1"}, 400),
                 ({"Content-Length": str(64 * 1024 * 1024 + 1)}, 413),
                 ({"Transfer-Encoding": "chunked", "Content-Length": "0"}, 400),
                 ({"Content-Encoding": "gzip", "Content-Length": "1"}, 400))
        for extra, expected in cases:
            with self.subTest(extra=extra):
                status, payload, _ = self.request("/api/image-search/query", method="POST",
                    headers={"Content-Type": "image/png", **extra}, manual_headers=True)
                self.assertEqual(status, expected, payload)
        status, payload, _ = self.request("/api/image-search/settings", method="POST",
            headers={"Content-Type": "application/json", "Content-Length": "16385"}, manual_headers=True)
        self.assertEqual(status, 413, payload)
        self.assertNotIn(str(self.data), json.dumps(payload))
        self.assertNotIn(str(self.media), json.dumps(payload))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.putrequest("POST", "/api/image-search/query")
            connection.putheader("Cookie", self.cookies["admin"])
            connection.putheader("Content-Type", "image/png")
            connection.putheader("Content-Length", "1")
            connection.putheader("Content-Length", "2")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()

    def test_bad_format_truncation_pixels_and_paths_are_rejected_without_server_failure(self):
        self.enable_index()
        for content, mime, expected in ((b"", "image/png", (400, 422)),
                                        (b"not an image", "image/png", (422,)),
                                        (self.query_image[:40], "image/png", (422,)),
                                        (oversized_png_header(), "image/png", (413, 422)),
                                        (geometric_image(format_name="GIF"), "image/png", (415, 422)),
                                        (self.query_image, "application/json", (415,)),
                                        (b'{"path":"/etc/passwd"}', "image/png", (422,))):
            with self.subTest(mime=mime, length=len(content)):
                status, payload, _ = self.query(content, headers={"Content-Type": mime})
                self.assertIn(status, expected, payload)
                self.assertNotIn(str(self.data), json.dumps(payload))
                self.assertNotIn(str(self.media), json.dumps(payload))
        self.assertEqual(self.request("/api/health")[0], 200)
        self.assertEqual(self.hash_sources(), self.source_hashes)

    def test_clear_only_removes_derived_index_and_requires_explicit_reenable(self):
        self.enable_index()
        self.assertTrue(self.query()[1]["results"])
        status, payload, _ = self.request("/api/image-search/actions", {"action": "clear"})
        self.assertEqual(status, 200, payload)
        status, payload, _ = self.request("/api/image-search/status")
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["summary"]["indexed"], 0)
        self.assertEqual(self.query()[0], 409)
        self.assertEqual(self.hash_sources(), self.source_hashes)
        self.assertEqual(self.request(f"/api/books/{self.album_book['id']}/pages/1", actor="child")[1], self.query_image)

    def test_management_payloads_are_strict_and_do_not_accept_paths_or_settings_escalation(self):
        for body in ({"enabled": 1}, {"enabled": "true"}, {"enabled": True, "path": "/outside"},
                     {"enabled": True, "url": "https://example.invalid"}):
            self.assertEqual(self.request("/api/image-search/settings", body)[0], 400, body)
        for body in ({"action": "unknown"}, {"action": "clear", "path": "/outside"},
                     {"action": "rebuild", "user_id": self.child["id"]}):
            self.assertEqual(self.request("/api/image-search/actions", body)[0], 400, body)
        self.assertFalse(self.request("/api/image-search/status")[1]["enabled"])

    def test_query_rate_limit_prevents_repeated_decodes_and_recovers_after_expiry(self):
        self.enable_index()
        result = {"results": [], "total": 0, "indexing": False}
        with patch.object(self.app.image_search, "query", return_value=result) as compute:
            for _ in range(10):
                self.assertEqual(self.query(actor="child")[0], 200)
            status, payload, headers = self.query(actor="child")
            self.assertEqual(status, 429, payload)
            self.assertIn("Retry-After", headers)
            self.assertEqual(compute.call_count, 10)
            # Age the disposable limiter entries without a real minute-long wait.
            with self.server.image_search_rate_lock:
                for attempts in self.server.image_search_attempts.values():
                    for index in range(len(attempts)):
                        attempts[index] -= 61
            self.assertEqual(self.query(actor="child")[0], 200)
            self.assertEqual(compute.call_count, 11)

    def test_full_query_slots_reject_without_decoding_or_leaking_a_slot(self):
        self.enable_index()
        self.assertTrue(self.server.image_search_slots.acquire(blocking=False))
        self.assertTrue(self.server.image_search_slots.acquire(blocking=False))
        try:
            with patch.object(self.app.image_search, "query", side_effect=AssertionError("Unexpected decode")):
                self.assertEqual(self.query(actor="child")[0], 429)
        finally:
            self.server.image_search_slots.release()
            self.server.image_search_slots.release()
        with patch.object(self.app.image_search, "query", return_value={"results": [], "indexing": False}):
            self.assertEqual(self.query(actor="child")[0], 200)
        deadline = time.monotonic() + 3
        while self.app.active_requests and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.app.active_requests, 0)
        self.assertTrue(self.server.image_search_slots.acquire(blocking=False))
        self.assertTrue(self.server.image_search_slots.acquire(blocking=False))
        self.server.image_search_slots.release()
        self.server.image_search_slots.release()

    def test_query_rechecks_authorization_and_visibility_after_computation(self):
        self.enable_index()
        raw = {"results": [{"book_id": self.album_book["id"], "page": 1,
                             "match": "exact", "distance": 0}], "total": 1, "indexing": False}

        def hide_during_query(*args, **kwargs):
            with self.app.acting_as(self.child["id"]):
                self.app.save_my_libraries({"visible_library_ids": []})
            return raw

        with patch.object(self.app.image_search, "query", side_effect=hide_during_query):
            status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["results"], [])
        if "total" in payload:
            self.assertEqual(payload["total"], 0)
        with self.app.acting_as(self.child["id"]):
            self.app.save_my_libraries({"visible_library_ids": [self.visible["id"]]})

        def revoke_during_query(*args, **kwargs):
            with self.app.acting_as(1):
                self.app.save_user({"library_ids": []}, self.child["id"])
            return raw

        with patch.object(self.app.image_search, "query", side_effect=revoke_during_query):
            status, payload, _ = self.query(actor="child")
        self.assertEqual(status, 401, payload)
        self.assertNotIn("results", payload)

    def test_query_rechecks_logout_and_global_disable_after_computation(self):
        self.enable_index()
        empty = {"results": [], "total": 0, "indexing": False}

        def logout_during_query(*args, **kwargs):
            self.app.revoke_session(self.cookies["child"].split("=", 1)[1])
            return empty

        with patch.object(self.app.image_search, "query", side_effect=logout_during_query):
            self.assertEqual(self.query(actor="child")[0], 401)

        def disable_during_query(*args, **kwargs):
            self.app.image_search.configure({"enabled": False})
            return empty

        with patch.object(self.app.image_search, "query", side_effect=disable_during_query):
            self.assertEqual(self.query()[0], 409)


if __name__ == "__main__":
    unittest.main()

"""HTTP and storage-boundary checks for shared administrator-only derivatives."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import quote

from server import COOKIE_NAME, ReaderError, ReaderServer
from test_transcoding import TranscodingFixture


class TranscodingSecurityTests(TranscodingFixture):
    def setUp(self):
        super().setUp()
        self.child = self.app.save_user({"username": "tc-child", "password": "temporary-child-password",
                                        "library_ids": [self.library_id]})["user"]
        self.admin_cookie = f"{COOKIE_NAME}={self.app.make_session(1)}"
        self.child_cookie = f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"
        self.server = ReaderServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)

    def stop_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, data=None, *, actor="admin", method=None, headers=None):
        request_headers = {}
        if actor:
            request_headers["Cookie"] = self.admin_cookie if actor == "admin" else self.child_cookie
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method or ("POST" if data is not None else "GET"), path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            metadata = dict(response.headers)
            if content and "application/json" in metadata.get("Content-Type", ""):
                content = json.loads(content)
            return response.status, content, metadata
        finally:
            connection.close()

    def test_transcoding_routes_require_login_and_admin_including_previews(self):
        self.enable()
        preview = f"/api/transcoding/books/{self.book_id}/pages/0"
        for actor, expected in ((None, 401), ("child", 403)):
            for path in ("/api/transcoding", "/api/transcoding/summary", preview):
                with self.subTest(actor=actor, path=path):
                    self.assertEqual(self.request(path, actor=actor)[0], expected)
                    self.assertEqual(self.request(path, actor=actor, method="HEAD")[0], expected)
            for path, body in (("/api/transcoding/rules/preview", {"rules": {"enabled": False}}),
                               ("/api/transcoding/rules", {"rules": {"enabled": False}}),
                               ("/api/transcoding/actions", {"ids": [self.book_id], "action": "original"}),
                               ("/api/transcoding/cleanup", {})):
                self.assertEqual(self.request(path, body, actor=actor)[0], expected)
        self.assertEqual(self.request(preview)[0:2], (200, self.sources[0]))
        self.assertEqual(self.record()["decision"], "undecided")

    def test_notification_summary_is_admin_only_lightweight_and_rejects_query_parameters(self):
        self.enable()
        from unittest.mock import patch
        with patch.object(self.manager, "snapshot", side_effect=AssertionError("full snapshot called")), \
             patch.object(self.manager, "inspect_book", side_effect=AssertionError("source inspected")):
            status, result, headers = self.request("/api/transcoding/summary")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(result["summary"]["pending_books"], 1)
        self.assertEqual(result["pending_book_ids"], [self.book_id])
        self.assertFalse(result["scanning"])
        self.assertEqual(self.request("/api/transcoding/summary?library_id=" + self.library_id)[0], 400)
        self.assertEqual(self.request("/api/transcoding/summary?library_id=" + self.library_id, actor="child")[0], 403)
        self.assertEqual(self.request("/api/transcoding/summary", actor="child")[0], 403)
        self.assertEqual(self.request("/api/transcoding/summary", actor=None)[0], 401)

    def test_pending_is_excluded_from_catalogue_models_history_bookmarks_and_direct_images(self):
        # Existing personal records must not become an alternate discovery path
        # after a work becomes pending under newly enabled rules.
        self.app.set_progress(self.book_id, {"page": 1, "completed": False})
        self.app.set_favorite(self.book_id, {"favorite": True})
        self.app.save_bookmark(self.book_id, {"page": 1, "note": "pending-private-marker"})
        model_id = self.books["命中作品"]["models"][0]["id"]
        self.enable()
        for actor in ("admin", "child"):
            for path, field in (("/api/library", "books"),
                                ("/api/library?library_id=" + self.library_id, "books"),
                                ("/api/models", "models"), ("/api/bookmarks", "bookmarks")):
                status, result, _ = self.request(path, actor=actor)
                self.assertEqual(status, 200, (path, result))
                serialized = json.dumps(result)
                self.assertNotIn(self.book_id, serialized, (actor, path))
                self.assertNotIn("pending-private-marker", serialized)
            for suffix in ("", "/cover", "/pages/0"):
                route = "/api/books/" + self.book_id + suffix
                self.assertEqual(self.request(route, actor=actor)[0], 404)
                self.assertEqual(self.request(route, actor=actor, method="HEAD", headers={"If-None-Match": "*"})[0], 404)
            self.assertEqual(self.request("/api/avatars/models/" + model_id, actor=actor)[0], 404)
            self.assertEqual(self.request(f"/api/books/{self.book_id}/favorite", {"favorite": True}, actor=actor)[0], 404)

    def test_child_cannot_override_transcoding_with_query_or_payload(self):
        self.enable()
        self.action("transcode")
        self.finish()
        page = "/api/books/" + self.book_id + "/pages/0"
        expected = self.request(page, actor="child")
        self.assertEqual(expected[0], 200)
        self.assertEqual(expected[2]["Content-Type"], "image/webp")
        for query in ("?original=1", "?raw=true", "?quality=100", "?format=original", "?transcode=false",
                      "?management=true", "?source=" + quote(str(self.album / "1.png"))):
            actual = self.request(page + query, actor="child")
            self.assertEqual(actual[0:2], expected[0:2], query)
            self.assertNotEqual(actual[1], self.sources[0], query)
        for body in ({"enabled": False}, {"transcoding": False, "original": True}):
            self.assertIn(self.request("/api/profile", body, actor="child")[0], (200, 400, 403))
        self.assertEqual(self.request(page, actor="child")[1], expected[1])
        for path in ("/transcoded/" + self.rows("tc_outputs")[0]["file"],
                     "/api/transcoded/" + self.rows("tc_outputs")[0]["file"],
                     "/api/transcoding/books/" + self.book_id + "/pages/0?original=1"):
            self.assertIn(self.request(path, actor="child")[0], (403, 404))

    def test_shared_output_requires_current_library_grant_even_with_etag(self):
        self.enable()
        self.action("transcode")
        self.finish()
        path = f"/api/books/{self.book_id}/pages/0"
        status, image, headers = self.request(path, actor="child")
        self.assertEqual(status, 200)
        self.assertEqual(self.request(path)[1], image)
        self.assertEqual(self.request(path, actor="child", headers={"If-None-Match": headers["ETag"]})[0], 304)
        self.app.save_user({"library_ids": []}, self.child["id"])
        self.assertEqual(self.request(path, actor="child")[0], 401)
        self.child_cookie = f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"
        self.assertEqual(self.request(path, actor="child", headers={"If-None-Match": headers["ETag"]})[0], 404)
        self.assertEqual(self.request(path, actor="child", method="HEAD")[0], 404)
        self.assertEqual(self.request(f"/api/books/{self.book_id}", actor="child")[0], 404)
        self.assertEqual(self.request(path)[1], image)

    def test_admin_original_decision_releases_work_but_child_has_no_server_paths(self):
        self.enable()
        status, result, headers = self.request("/api/transcoding")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, result, _ = self.request("/api/transcoding/actions", {"ids": [self.book_id], "action": "original"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["books"][0]["status"], "original")
        self.assertEqual(self.request(f"/api/books/{self.book_id}/pages/0", actor="child")[1], self.sources[0])
        serialized = json.dumps(self.request("/api/library", actor="child")[1])
        self.assertNotIn(str(self.data), serialized)
        self.assertNotIn("output_path", serialized)
        self.scan()
        self.assertEqual(self.record()["status"], "original")

    def test_csrf_rules_and_actions_do_not_mutate_admin_state(self):
        self.enable()
        revision = self.manager.settings()[1]
        for path, data in (("/api/transcoding/rules", {"rules": {"enabled": False}, "expected_revision": revision}),
                           ("/api/transcoding/actions", {"ids": [self.book_id], "action": "original"}),
                           ("/api/transcoding/cleanup", {})):
            self.assertEqual(self.request(path, data, headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.manager.settings()[1], revision)
        self.assertEqual(self.record()["decision"], "undecided")

    def test_current_root_boundary_is_rechecked_for_cached_outputs(self):
        self.enable()
        self.action("transcode")
        self.finish()
        self.assertEqual(self.request(f"/api/books/{self.book_id}/pages/0")[0], 200)
        unrelated = self.root / "other-root"
        unrelated.mkdir()
        self.app.roots = [unrelated]
        self.app.root = unrelated
        for path in (f"/api/books/{self.book_id}/pages/0", f"/api/transcoding/books/{self.book_id}/pages/0"):
            self.assertIn(self.request(path)[0], (403, 404, 422))
        self.assertTrue(self.rows("tc_outputs"))

    def test_removed_source_or_catalogue_cannot_be_read_from_shared_cache(self):
        self.enable()
        self.action("transcode")
        self.finish()
        image_path = f"/api/books/{self.book_id}/pages/0"
        original = (self.album / "1.png").read_bytes()
        (self.album / "1.png").unlink()
        self.assertIn(self.request(image_path)[0], (404, 422))
        (self.album / "1.png").write_bytes(original)
        self.app.batch_books({"ids": [self.book_id], "action": "remove"})
        self.assertEqual(self.request(image_path)[0], 404)
        self.assertEqual(self.request(f"/api/transcoding/books/{self.book_id}/pages/0")[0], 404)
        self.assertEqual(self.manager.snapshot()["books"], [])
        self.assertFalse(self.manager.run_once())
        self.assertEqual((self.album / "1.png").read_bytes(), original)

    def test_missing_or_forged_cache_mapping_never_falls_back_to_original(self):
        self.enable()
        self.action("transcode")
        self.finish()
        row = next(row for row in self.rows("tc_pages") if row["page_index"] == 0)
        path = self.manager.root / row["active_file"]
        path.unlink()
        self.assertEqual(self.request(f"/api/books/{self.book_id}/pages/0", actor="child")[0], 422)
        with closing(self.app.connect()) as db, db:
            db.execute("UPDATE tc_pages SET active_file=? WHERE book_id=? AND page_index=0", ("../../outside.jpg", self.book_id))
        self.assertEqual(self.request(f"/api/books/{self.book_id}/pages/0", actor="child")[0], 422)
        self.assertEqual((self.album / "1.png").read_bytes(), self.sources[0])


if __name__ == "__main__":
    unittest.main()

"""Administrator failure maintenance without exposing source paths or rebuilding."""
from contextlib import closing
from io import BytesIO
import json
from unittest.mock import patch

from PIL import Image
from test_image_search import ImageSearchFixture


class ImageSearchFailureTests(ImageSearchFixture):
    def mark_failed(self, keys=("1.png", "2.png"), code="decode_error", actual=""):
        self.enable()
        with closing(self.app.connect()) as connection, connection:
            connection.executemany("UPDATE is_pages SET status='failed',local_state='failed',error='合成解码失败',failure_code=?,actual_format=? WHERE book_id=? AND page_key=?",
                                   [(code, actual, self.book_id, f"几何作品/{key}") for key in keys])

    def test_paginated_list_is_admin_only_and_contains_no_source_roots_or_binary_features(self):
        self.mark_failed()
        first = self.manager.failures(page=1, page_size=1)
        second = self.manager.failures(page=2, page_size=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(len(first["items"]), 1)
        self.assertNotEqual(first["items"][0]["page_key"], second["items"][0]["page_key"])
        self.assertEqual({item["page"] for item in first["items"] + second["items"]}, {0, 1})
        item = first["items"][0]
        self.assertTrue(item["retryable"])
        self.assertEqual(item["book_title"], "几何作品")
        self.assertEqual(item["library_name"], "开放写真库")
        self.assertNotIn(str(self.root), json.dumps(first, ensure_ascii=False))
        self.assertFalse(set(item) & {"source_root", "path", "preview", "local_features"})
        child = self.child([self.library_id])
        with self.app.acting_as(child):
            self.error(403, self.manager.failures)
            self.error(403, self.manager.retry_failures, {"items": [{"book_id": self.book_id, "page_key": item["page_key"]}]})
        for options in ({"page": 0}, {"page": True}, {"page_size": 101}, {"page_size": "20"}):
            self.error(400, self.manager.failures, **options)

    def test_only_selected_failure_retries_and_local_only_failure_keeps_basic_fingerprint(self):
        self.mark_failed()
        before = {row["page_key"]: row for row in self.rows()}
        key = self.manager.failures()["items"][0]["page_key"]
        result = self.manager.retry_failures({"items": [{"book_id": self.book_id, "page_key": key}]})
        self.assertEqual((result["retried"], result["skipped"]), (1, 0))
        after = {row["page_key"]: row for row in self.rows()}
        self.assertEqual(after[key]["status"], "pending")
        other = next(value for value in before if value != key)
        self.assertEqual(after[other]["status"], "failed")
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE is_pages SET status='ready',local_state='failed' WHERE book_id=? AND page_key=?", (self.book_id, other))
        result = self.manager.retry_failures({"items": [{"book_id": self.book_id, "page_key": other}]})
        current = {row["page_key"]: row for row in self.rows()}[other]
        self.assertEqual(current["status"], "ready")
        self.assertEqual(current["digest"], before[other]["digest"])
        self.assertEqual(current["local_state"], "pending")

    def test_actual_unsupported_format_is_recorded_by_real_worker_and_skipped_on_retry(self):
        output = BytesIO()
        Image.new("RGB", (96, 128), "pink").save(output, "BMP")
        (self.album / "1.png").write_bytes(output.getvalue())
        self.app._scan_library(self.app._library_config(self.library_id))
        self.enable()
        failures = self.manager.failures()["items"]
        self.assertEqual(len(failures), 1)
        item = failures[0]
        self.assertEqual(item["actual_format"], "BMP")
        self.assertEqual(item["failure_code"], "unsupported_format")
        self.assertFalse(item["retryable"])
        self.assertIn("实际格式为 BMP", item["reason"])
        result = self.manager.retry_failures({"items": [{"book_id": self.book_id, "page_key": item["page_key"]}]})
        self.assertEqual((result["retried"], result["skipped"]), (0, 1))
        self.manager.configure({"action": "retry"})
        self.assertEqual(self.rows()[0]["status"], "failed")

    def test_legacy_format_signature_is_bounded_read_only_and_unreadable_header_is_not_guessed(self):
        output = BytesIO()
        Image.new("RGB", (96, 128), "pink").save(output, "GIF")
        (self.album / "1.png").write_bytes(output.getvalue())
        self.app._scan_library(self.app._library_config(self.library_id))
        self.enable()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE is_pages SET error='请选择 JPG、PNG 或 WebP 图片。',local_error='',actual_format='',failure_code='' WHERE status='failed'")
        before = self.rows()
        item = self.manager.failures()["items"][0]
        self.assertEqual(item["actual_format"], "GIF")
        self.assertFalse(item["retryable"])
        self.assertEqual(before, self.rows())
        with patch.object(self.app.transcoding, "_open_source", side_effect=OSError("temporary unavailable")):
            item = self.manager.failures()["items"][0]
        self.assertEqual(item["actual_format"], "")
        self.assertFalse(item["retryable"])
        self.manager.configure({"action": "retry"})
        self.assertEqual(self.manager.failures()["total"], 1)

    def test_removed_work_and_concurrent_removal_cannot_be_requeued(self):
        self.mark_failed()
        items = self.manager.failures()["items"]
        original = self.manager._failure_format
        def remove_during_probe(row):
            with closing(self.app.connect()) as connection, connection:
                connection.execute("UPDATE books SET removed=1 WHERE id=?", (self.book_id,))
            return original(row)
        with patch.object(self.manager, "_failure_format", side_effect=remove_during_probe):
            result = self.manager.retry_failures({"items": [{"book_id": self.book_id, "page_key": items[0]["page_key"]}]})
        self.assertEqual(result["retried"], 0)
        self.assertEqual(self.manager.failures()["total"], 0)

    def test_retry_input_is_bounded_and_does_not_accept_paths_as_target_ids(self):
        self.mark_failed()
        for body in ({}, {"items": []}, {"items": [None]}, {"items": [{"book_id": "../../other", "page_key": "x"}]},
                     {"items": [{"book_id": self.book_id, "page_key": "x"}] * 101}):
            self.error(400, self.manager.retry_failures, body)

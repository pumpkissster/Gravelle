"""Disposable-source regression checks for administrator controlled derivatives.

These tests deliberately use small images and pixel thresholds; no real library,
database, or large allocation is needed to exercise the same decision pipeline.
"""
from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Library, ReaderError
from transcoding import DEFAULT_RULES, StoragePaused, image_dimensions


def image_bytes(size=(64, 96), color=(180, 50, 100), format_name="PNG", **options):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format_name, **options)
    return stream.getvalue()


class TranscodingFixture(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("SUMI_TEST_TEMP", ROOT / "work" / "transcoding-tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="tc-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.data = self.root / "data"
        self.album = self.media / "命中作品"
        self.album.mkdir(parents=True)
        self.sources = [image_bytes(), image_bytes((8, 12)), image_bytes((80, 120), (30, 80, 170))]
        for name, data in zip(("1.png", "2.png", "3.png"), self.sources):
            (self.album / name).write_bytes(data)
        small = self.media / "普通作品"
        small.mkdir()
        (small / "1.png").write_bytes(self.sources[1])
        self.app = Library(self.media, self.data, "", 0)
        self.addCleanup(lambda: self.app.close())
        self.app.register({"username": "tc-admin", "password": "temporary-transcode-password"}, "test")
        self.app.transcoding.auto_start = False
        self.library_id = self.app.save_library({"name": "转码测试", "path": str(self.media), "media_type": "photo"})["library"]["id"]
        self.wait_scan()
        self.books = {item["title"]: item for item in self.app.listing()["books"]}
        self.book_id = self.books["命中作品"]["id"]
        self.small_id = self.books["普通作品"]["id"]

    @property
    def manager(self):
        return self.app.transcoding

    def wait_scan(self):
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning, "Disposable scan did not finish")

    def scan(self):
        self.app.start_scan(self.library_id)
        self.wait_scan()

    def enable(self, **updates):
        rules = {**DEFAULT_RULES, "enabled": True, "file_size_mb": 0,
                 "max_source_pixels": 1000, "max_source_side": 0,
                 "max_output_side": 512, "min_free_space_mb": 0, **updates}
        return self.save_rules(rules)

    def save_rules(self, changes):
        return self.manager.save_rules({"rules": changes, "expected_revision": self.manager.settings()[1]})

    def action(self, action, ids=None):
        return self.manager.actions({"ids": ids or [self.book_id], "action": action})

    def record(self, book_id=None):
        return next(item for item in self.manager.snapshot()["books"] if item["id"] == (book_id or self.book_id))

    def rows(self, table, book_id=None):
        with closing(self.app.connect()) as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE book_id=?", (book_id or self.book_id,))]

    def source_hashes(self):
        return {item.relative_to(self.media).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
                for item in self.media.rglob("*") if item.is_file()}

    def finish(self):
        for _ in range(10):
            if not self.manager.run_once():
                break
        self.assertEqual(self.record()["status"], "ready", self.record())

    def error(self, status, function, *args, **kwargs):
        with self.assertRaises(ReaderError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def restart(self):
        self.app.close()
        self.app = Library(self.media, self.data, "", 0)
        self.app.transcoding.auto_start = False


class TranscodingTests(TranscodingFixture):
    def test_notification_summary_reads_metadata_only_and_reports_scan_completion(self):
        self.enable()
        detailed = self.manager.snapshot()["summary"]
        with patch.object(self.manager, "snapshot", side_effect=AssertionError("full snapshot called")), \
             patch.object(self.manager, "inspect_book", side_effect=AssertionError("source inspected")), \
             patch.object(self.manager, "_output_exists", side_effect=AssertionError("output inspected")), \
             patch.object(self.manager, "_storage_directory", side_effect=AssertionError("storage inspected")):
            result = self.manager.summary()
        self.assertEqual(result["summary"]["pending_books"], 1)
        self.assertEqual(result["summary"]["pending_decision_books"], 1)
        for key, value in result["summary"].items():
            self.assertEqual(value, detailed[key], key)
        self.assertEqual(result["pending_book_ids"], [self.book_id])
        self.assertEqual(result["pending_records"][0]["id"], self.book_id)
        self.assertEqual(result["pending_records"][0]["library_id"], self.library_id)
        self.assertGreater(result["pending_records"][0]["generation"], 0)
        self.assertEqual(result["pending_records"][0]["rule_revision"], result["revision"])
        self.assertTrue(result["enabled"])
        self.assertFalse(result["scanning"])
        self.assertIsInstance(result["last_scan"], float)
        self.assertEqual(result["libraries"], [{"id": self.library_id, "last_scan": result["last_scan"], "scanning": False}])
        # The notification API must not export per-page paths or cache details.
        self.assertNotIn("books", result)
        self.assertNotIn(str(self.data), json.dumps(result))
        self.assertNotIn("reclaimable_bytes", result["summary"])
        with self.app.state_lock:
            self.app.pending_scans.add(self.library_id)
            try:
                self.assertTrue(self.manager.summary()["scanning"])
            finally:
                self.app.pending_scans.discard(self.library_id)
        self.scan()
        refreshed = self.manager.summary()
        self.assertGreater(refreshed["last_scan"], result["last_scan"])
        self.assertEqual(refreshed["pending_records"], result["pending_records"])

    def test_notification_counts_only_pending_choices_not_running_paused_or_failed(self):
        self.enable()
        self.assertEqual(self.manager.summary()["summary"]["pending_books"], 1)
        self.action("transcode")
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])
        self.assertEqual(self.manager.summary()["summary"]["processing_books"], 1)
        self.action("pause")
        self.assertEqual(self.manager.summary()["summary"]["pending_books"], 0)
        self.assertEqual(self.manager.summary()["summary"]["paused_books"], 1)
        self.action("resume")
        with patch.object(self.manager, "_convert", side_effect=ReaderError("isolated failure", 422)):
            self.assertTrue(self.manager.run_once())
        self.assertEqual(self.manager.summary()["summary"]["failed_books"], 1)
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])
        self.action("retry")
        self.action("cancel")
        self.assertEqual(self.manager.summary()["summary"]["paused_books"], 1)
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])
        self.action("original")
        self.assertEqual(self.manager.summary()["summary"]["original_books"], 1)
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])

    def test_notification_includes_previous_transcode_waiting_for_rule_decision(self):
        self.enable()
        initial = self.manager.summary()["pending_records"][0]
        self.action("transcode")
        self.finish()
        self.assertEqual(self.manager.summary()["summary"]["completed_books"], 1)
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])
        self.save_rules({"quality": 72})
        record = self.record()
        self.assertEqual((record["status"], record["decision"], record["published"]), ("pending", "transcode", True))
        result = self.manager.summary()
        self.assertEqual(result["pending_book_ids"], [self.book_id])
        self.assertEqual(result["summary"]["pending_decision_books"], 1)
        self.assertGreater(result["pending_records"][0]["generation"], initial["generation"])
        self.assertGreater(result["pending_records"][0]["rule_revision"], initial["rule_revision"])
        self.save_rules({"enabled": False})
        self.assertFalse(self.manager.summary()["enabled"])
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])

    def test_notification_ignores_removed_inactive_and_unmatched_books(self):
        self.enable()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET removed=1 WHERE id=?", (self.book_id,))
        self.assertEqual(self.manager.summary()["summary"]["total_books"], 0)
        self.assertEqual(self.manager.summary()["pending_book_ids"], [])
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET removed=0,active=0 WHERE id=?", (self.book_id,))
        self.assertEqual(self.manager.summary()["summary"]["total_books"], 0)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET active=1 WHERE id=?", (self.book_id,))
        self.assertEqual(self.manager.summary()["summary"]["total_books"], 1)
        self.assertNotIn(self.small_id, self.manager.summary()["pending_book_ids"])

    def test_default_disabled_preserves_existing_sources_and_catalogue(self):
        self.assertFalse(self.manager.settings()[0]["enabled"])
        self.assertEqual(len(self.app.listing()["books"]), 2)
        self.assertEqual(self.app.image(self.book_id, 0)[0], self.sources[0])
        self.assertEqual(self.manager.snapshot()["books"], [])
        self.assertFalse(self.manager.run_once())
        self.assertFalse(self.manager.root.exists())

    def test_header_scan_matches_per_page_and_pending_is_hidden_until_decision(self):
        before = self.source_hashes()
        self.enable()
        record = self.record()
        self.assertEqual((record["status"], record["matched_count"], record["page_count"]), ("pending", 2, 3))
        self.assertFalse(record["published"])
        self.assertEqual([item["id"] for item in self.app.listing()["books"]], [self.small_id])
        self.error(404, self.app.book, self.book_id)
        self.assertEqual(self.app.image(self.book_id, 0, management=True)[0], self.sources[0])
        self.assertFalse(self.manager.run_once())
        self.assertEqual(self.source_hashes(), before)

    def test_original_choice_persists_rescan_restart_and_stays_in_matched_list(self):
        self.enable()
        self.action("original")
        self.scan()
        self.restart()
        self.scan()
        record = self.record()
        self.assertEqual((record["decision"], record["status"], record["matched_count"]), ("original", "original", 2))
        self.assertTrue(record["published"])
        self.assertEqual(self.app.image(self.book_id, 0)[0], self.sources[0])
        self.assertEqual(len(self.app.listing()["books"]), 2)
        self.assertFalse(self.manager.run_once())
        self.assertEqual(self.rows("tc_outputs"), [])

    def test_only_matched_pages_convert_and_publish_after_all_succeed(self):
        before = self.source_hashes()
        self.enable()
        self.action("transcode")
        self.assertTrue(self.manager.run_once())
        self.assertEqual(self.record()["success_count"], 1)
        self.assertFalse(self.record()["published"])
        self.error(404, self.app.image, self.book_id, 0)
        self.assertTrue(self.manager.run_once())
        record = self.record()
        self.assertEqual((record["status"], record["success_count"], record["output_count"]), ("ready", 2, 2))
        self.assertEqual(self.app.image(self.book_id, 1)[0], self.sources[1])
        first, mime, etag = self.app.image(self.book_id, 0)
        self.assertEqual(mime, "image/webp")
        self.assertNotEqual(first, self.sources[0])
        with Image.open(BytesIO(first)) as image:
            self.assertEqual(image.size, (64, 96))
        outputs = self.rows("tc_outputs")
        self.assertEqual(len(outputs), 2)
        self.assertEqual(record["actual_bytes"], sum(row["size"] for row in outputs))
        self.assertEqual(Path(record["output_path"]), self.manager.root / self.book_id)
        self.assertTrue(all((self.manager.root / row["file"]).is_file() for row in outputs))
        self.assertEqual(etag, '"' + hashlib.sha256(first).hexdigest() + '"')
        self.assertEqual(self.source_hashes(), before)
        self.scan()
        self.assertEqual(self.record()["status"], "ready")
        self.assertFalse(self.manager.run_once())

    def test_real_worker_resizes_jpeg_applies_orientation_and_removes_metadata(self):
        exif = Image.Exif()
        exif[274] = 6
        (self.album / "1.png").unlink()
        (self.album / "1.jpg").write_bytes(image_bytes((900, 600), format_name="JPEG", exif=exif))
        self.scan()
        before = self.source_hashes()
        self.enable(output_format="jpeg")
        self.action("transcode")
        self.finish()
        content, mime, _ = self.app.image(self.book_id, 0)
        self.assertEqual(mime, "image/jpeg")
        with Image.open(BytesIO(content)) as image:
            self.assertEqual(image.size, (341, 512))
            self.assertFalse(image.getexif())
        self.assertEqual(self.source_hashes(), before)

    def test_rules_preview_revision_and_save_do_not_start_jobs(self):
        initial = self.manager.settings()
        original_records = self.rows("tc_books")
        preview = self.manager.preview_rules({"rules": {"enabled": True, "file_size_mb": 0, "max_source_pixels": 1000}})
        self.assertEqual(preview["totals"]["newly_matched"], 2)
        self.assertEqual(self.manager.settings(), initial)
        self.assertEqual(self.rows("tc_books"), original_records)
        self.enable()
        self.error(409, self.manager.save_rules, {"rules": {"quality": 70}, "expected_revision": initial[1]})
        self.assertFalse(self.manager.run_once())
        self.assertEqual(self.rows("tc_outputs"), [])
        self.assertEqual(self.record()["status"], "pending")

    def test_threshold_and_runtime_edits_reuse_valid_outputs(self):
        self.enable()
        self.action("transcode")
        self.finish()
        outputs = self.rows("tc_outputs")
        preview = self.manager.preview_rules({"rules": {"max_source_pixels": 2000, "timeout_seconds": 60}})
        self.assertEqual(preview["totals"]["needs_retranscode"], 0)
        self.save_rules({"max_source_pixels": 2000, "timeout_seconds": 60})
        self.assertEqual(self.record()["status"], "ready")
        self.assertEqual(self.rows("tc_outputs"), outputs)
        self.assertFalse(self.manager.run_once())

    def test_quality_change_keeps_old_mapping_until_all_replacements_succeed(self):
        self.enable()
        self.action("transcode")
        self.finish()
        original_mapping = {row["page_key"]: row["active_file"] for row in self.rows("tc_pages")}
        prior_image = self.app.image(self.book_id, 0)
        preview = self.manager.preview_rules({"rules": {"quality": 60}})
        self.assertEqual(preview["totals"]["needs_retranscode"], 2)
        self.save_rules({"quality": 60})
        self.assertTrue(self.record()["published"])
        self.assertTrue(self.record()["needs_retranscode"])
        self.assertFalse(self.manager.run_once())
        self.assertEqual(self.app.image(self.book_id, 0), prior_image)
        self.action("transcode")
        self.assertTrue(self.manager.run_once())
        self.assertEqual({row["page_key"]: row["active_file"] for row in self.rows("tc_pages")}, original_mapping)
        self.assertEqual(self.app.image(self.book_id, 0), prior_image)
        self.assertTrue(self.manager.run_once())
        self.assertFalse(self.record()["needs_retranscode"])
        self.assertNotEqual(self.app.image(self.book_id, 0)[2], prior_image[2])

    def test_inserting_unmatched_page_preserves_stable_source_mapping(self):
        self.enable()
        self.action("transcode")
        self.finish()
        before = self.app.image(self.book_id, 0)
        old_files = {row["page_key"]: row["active_file"] for row in self.rows("tc_pages") if row["matched"]}
        inserted = image_bytes((7, 9), (0, 230, 100))
        (self.album / "0.png").write_bytes(inserted)
        self.scan()
        self.assertEqual(self.record()["status"], "ready")
        self.assertEqual(self.app.image(self.book_id, 0)[0], inserted)
        self.assertEqual(self.app.image(self.book_id, 1), before)
        self.assertEqual({row["page_key"]: row["active_file"] for row in self.rows("tc_pages") if row["matched"]}, old_files)
        self.assertEqual(len(self.rows("tc_outputs")), 2)

    def test_source_replacement_never_serves_stale_derivative_and_only_redoes_changed_page(self):
        self.enable()
        self.action("transcode")
        self.finish()
        second_output = self.app.image(self.book_id, 2)
        (self.album / "1.png").write_bytes(image_bytes((64, 96), (5, 250, 20)))
        self.error(422, self.app.image, self.book_id, 0)
        self.scan()
        self.assertFalse(self.record()["published"])
        self.assertEqual(self.record()["success_count"], 1)
        self.action("transcode")
        self.assertTrue(self.manager.run_once())
        self.assertEqual(self.record()["status"], "ready")
        self.assertEqual(self.app.image(self.book_id, 2), second_output)
        self.assertFalse(self.manager.run_once())

    def test_pause_cancel_restart_resume_and_retry_preserve_partial_success(self):
        self.enable()
        self.action("transcode")
        self.assertTrue(self.manager.run_once())
        converted = self.rows("tc_outputs")
        self.action("pause")
        self.assertFalse(self.manager.run_once())
        self.restart()
        self.assertEqual(self.record()["status"], "paused")
        self.assertFalse(self.manager.run_once())
        self.action("resume")
        with patch.object(self.manager, "_convert", side_effect=RuntimeError("test decoder failure")):
            self.assertTrue(self.manager.run_once())
        self.assertEqual((self.record()["status"], self.record()["failed_count"]), ("failed", 1))
        self.assertIn("test decoder failure", self.record()["error"])
        self.assertEqual(self.rows("tc_outputs"), converted)
        self.action("cancel")
        self.assertEqual(self.record()["status"], "cancelled")
        self.assertFalse(self.manager.run_once())
        self.action("retry")
        self.assertTrue(self.manager.run_once())
        self.assertEqual((self.record()["status"], self.record()["success_count"]), ("ready", 2))
        self.assertEqual(len(self.rows("tc_outputs")), 2)

    def test_restart_recovers_interrupted_running_page_without_publishing_partial_book(self):
        self.enable()
        self.action("transcode")
        self.manager.run_once()
        with closing(self.app.connect()) as db, db:
            db.execute("UPDATE tc_books SET status='running' WHERE book_id=?", (self.book_id,))
            db.execute("UPDATE tc_pages SET status='running' WHERE book_id=? AND status='pending' AND matched=1", (self.book_id,))
        self.restart()
        self.assertEqual(self.record()["status"], "queued")
        self.assertFalse(self.record()["published"])
        self.assertEqual(self.record()["success_count"], 1)
        self.assertTrue(self.manager.run_once())
        self.assertEqual(self.record()["status"], "ready")

    def test_disk_shortage_pauses_replacement_and_keeps_published_output(self):
        self.enable()
        self.action("transcode")
        self.finish()
        before = self.app.image(self.book_id, 0)
        output_hashes = {row["file"]: hashlib.sha256((self.manager.root / row["file"]).read_bytes()).hexdigest()
                         for row in self.rows("tc_outputs")}
        self.save_rules({"quality": 55, "min_free_space_mb": 1})
        self.action("transcode")
        with patch("transcoding.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
            self.assertTrue(self.manager.run_once())
        self.assertEqual(self.record()["status"], "paused")
        self.assertTrue(self.record()["published"])
        self.assertEqual(self.app.image(self.book_id, 0), before)
        for filename, digest in output_hashes.items():
            self.assertEqual(hashlib.sha256((self.manager.root / filename).read_bytes()).hexdigest(), digest)
        self.assertEqual(list(self.manager.root.rglob(".job-*")), [])

    def test_archive_uses_entry_size_and_dimensions_and_only_converts_matched_entry(self):
        archive_path = self.media / "压缩作品.cbz"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("1.png", self.sources[0])
            archive.writestr("2.png", self.sources[1])
        self.scan()
        archive_id = next(item["id"] for item in self.app.listing()["books"] if item["format"] == "cbz")
        before = archive_path.read_bytes()
        self.enable()
        self.assertEqual(self.record(archive_id)["matched_count"], 1)
        self.action("transcode", [archive_id])
        self.assertTrue(self.manager.run_once())
        self.assertEqual(self.record(archive_id)["status"], "ready")
        self.assertEqual(self.app.image(archive_id, 1)[0], self.sources[1])
        self.assertEqual(self.app.image(archive_id, 0)[1], "image/webp")
        self.assertEqual(len(self.rows("tc_outputs", archive_id)), 1)
        self.assertEqual(archive_path.read_bytes(), before)

    def test_batch_validation_is_atomic_and_disabling_rules_restores_source(self):
        self.enable()
        self.error(404, self.manager.actions, {"ids": [self.book_id, "f" * 24], "action": "original"})
        self.assertEqual(self.record()["decision"], "undecided")
        self.save_rules({"enabled": False})
        self.assertEqual(len(self.app.listing()["books"]), 2)
        self.assertEqual(self.app.image(self.book_id, 0)[0], self.sources[0])
        self.assertEqual(self.manager.snapshot()["books"], [])

    def test_corrupt_header_fails_closed_without_mutating_source(self):
        broken = b"not-a-valid-image"
        (self.album / "1.png").write_bytes(broken)
        self.scan()
        self.enable()
        self.assertIn("图片信息读取失败", self.record()["reasons"])
        self.action("transcode")
        self.assertTrue(self.manager.run_once())
        self.assertEqual(self.record()["status"], "failed")
        self.assertFalse(self.record()["published"])
        self.assertEqual((self.album / "1.png").read_bytes(), broken)
        self.assertEqual(list(self.manager.root.rglob(".job-*")), [])

    def test_headers_handle_png_jpeg_and_webp_without_full_decoding(self):
        for format_name in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format=format_name):
                data = image_bytes((63, 95), format_name=format_name)
                with patch.object(Image.Image, "load", side_effect=AssertionError("header scan decoded image")):
                    self.assertEqual(image_dimensions(BytesIO(data)), (63, 95))


if __name__ == "__main__":
    unittest.main()

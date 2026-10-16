"""Derivative history and explicit active-cache removal with disposable photos."""
from contextlib import closing
import json
import threading
import time
from unittest.mock import patch

from server import ReaderError
from test_transcoding import TranscodingFixture


class OptimizationDetailsTests(TranscodingFixture):
    def test_details_are_paginated_and_include_unmatched_original_paths_without_decoding(self):
        self.enable()
        with patch.object(self.manager, "_open_source", side_effect=AssertionError("details opened original")):
            data = self.manager.details(self.book_id, page=2, page_size=1)
        self.assertEqual(data["pagination"], {"page": 2, "page_size": 1, "total": 3, "pages": 3})
        self.assertEqual(data["pages"][0]["status"], "unmatched")
        self.assertTrue(data["pages"][0]["original_path"].endswith("2.png"))
        self.assertEqual(data["book"]["pending_count"], 2)
        self.assertEqual(data["book"]["output_count"], 0)
        self.assertIsNone(data["book"]["completed_at"])
        self.assertEqual(self.manager.details(self.book_id, page=999, page_size=1)["pagination"]["page"], 3)
        for kwargs in ({"page": True}, {"page": 0}, {"page_size": 101}, {"page_size": "40"}):
            with self.assertRaises(ReaderError):
                self.manager.details(self.book_id, **kwargs)

    def test_real_output_parameters_and_completion_are_preserved_by_scan_and_reuse(self):
        self.enable(quality=71)
        self.action("transcode")
        self.finish()
        before = self.manager.details(self.book_id)
        stamp = before["book"]["completed_at"]
        active = before["active_profiles"][0]
        self.assertEqual(active["parameters"]["quality"], 71)
        self.assertEqual(active["parameters"]["rule_revision"], before["target_rule_revision"])
        self.assertEqual(before["book"]["actual_bytes"], sum(row["active_bytes"] for row in before["pages"]))
        self.scan()
        self.assertEqual(self.record()["completed_at"], stamp)
        self.action("original")
        self.action("transcode")
        self.assertEqual(self.record()["completed_at"], stamp)
        self.save_rules({"max_source_pixels": 7000})
        self.assertEqual(self.record()["completed_at"], stamp)
        self.save_rules({"max_source_pixels": 1000})
        self.assertEqual(self.record()["completed_at"], stamp)
        self.save_rules({"quality": 72})
        waiting = self.manager.details(self.book_id)
        self.assertEqual(waiting["book"]["completed_at"], stamp)
        self.assertEqual(waiting["active_profiles"][0]["parameters"]["quality"], 71)
        self.assertEqual(waiting["target_parameters"]["quality"], 72)
        self.assertNotEqual(waiting["active_profiles"][0]["profile"], waiting["target_profile"])
        self.action("transcode")
        self.finish()
        after = self.manager.details(self.book_id)
        self.assertGreater(after["book"]["completed_at"], stamp)
        self.assertEqual(after["active_profiles"][0]["parameters"]["quality"], 72)

    def test_historical_missing_parameters_stay_unknown(self):
        self.enable()
        self.action("transcode")
        self.finish()
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE tc_outputs SET output_parameters=''")
        data = self.manager.details(self.book_id)
        self.assertIsNone(data["active_profiles"][0]["parameters"])
        self.assertIsNone(data["pages"][0]["active_parameters"])
        self.assertEqual(data["target_parameters"]["quality"], 88)
        self.assertTrue(data["pages"][0]["active_path"].startswith(str(self.data / "transcoded")))

    def test_details_and_clear_require_admin_and_reject_inactive_books(self):
        self.enable()
        with patch.object(self.app, "require_admin", side_effect=ReaderError("拒绝", 403)):
            for call in (lambda: self.manager.details(self.book_id), self.manager.cache_stats,
                         lambda: self.manager.clear_active({"confirm": True})):
                with self.assertRaises(ReaderError) as error:
                    call()
                self.assertEqual(error.exception.status, 403)
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET removed=1 WHERE id=?", (self.book_id,))
        with self.assertRaises(ReaderError):
            self.manager.details(self.book_id)

    def test_explicit_clear_switches_ready_book_to_original_and_retains_personal_records(self):
        self.app.set_progress(self.book_id, {"page": 1, "completed": False})
        self.app.set_favorite(self.book_id, {"favorite": True})
        self.app.save_bookmark(self.book_id, {"page": 1, "note": "keep reading record"})
        self.enable()
        self.action("transcode")
        self.finish()
        before_sources = self.source_hashes()
        before_stamp = self.record()["completed_at"]
        with closing(self.app.connect()) as connection:
            tables = {name: [tuple(row) for row in connection.execute("SELECT * FROM " + name)]
                      for name in ("progress", "favorites", "bookmarks")}
        before = self.manager.cache_stats()
        self.assertEqual(before["active_files"], 2)
        result = self.manager.clear_active({"confirm": True, "ids": [self.book_id]})
        self.assertEqual(result["books"], 1)
        self.assertEqual(result["removed_files"], 2)
        self.assertEqual(self.manager.cache_stats()["files"], 0)
        self.assertEqual(self.record()["status"], "original")
        self.assertTrue(self.record()["published"])
        self.assertEqual(self.record()["completed_at"], before_stamp)
        self.assertFalse(self.manager.run_once())
        self.scan()
        self.assertEqual(self.record()["decision"], "original")
        self.assertEqual(self.source_hashes(), before_sources)
        with closing(self.app.connect()) as connection:
            for name, rows in tables.items():
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM " + name)], rows)

    def test_pending_replacement_and_unpublished_outputs_are_protected(self):
        self.enable()
        self.action("transcode")
        self.assertTrue(self.manager.run_once())
        waiting = self.manager.cache_stats()
        self.assertEqual(waiting["waiting_files"], 1)
        self.assertEqual(waiting["clearable_files"], 0)
        with self.assertRaises(ReaderError):
            self.manager.clear_active({"confirm": True, "ids": [self.book_id]})
        self.finish()
        self.save_rules({"quality": 75})
        self.assertEqual(self.manager.cache_stats()["clearable_files"], 0)
        before = self.rows("tc_pages")
        result = self.manager.clear_active({"confirm": True})
        self.assertEqual(result["books"], 0)
        self.assertEqual(self.rows("tc_pages"), before)

    def test_clear_rejects_running_worker_and_missing_confirmation(self):
        self.enable()
        self.action("transcode")
        entered, release = threading.Event(), threading.Event()
        original = self.manager._convert
        def blocked(job, rules):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(job, rules)
        with patch.object(self.manager, "_convert", side_effect=blocked):
            thread = threading.Thread(target=self.manager.run_once)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ReaderError) as error:
                    self.manager.clear_active({"confirm": True})
                self.assertEqual(error.exception.status, 409)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        for body in ({}, {"confirm": 1}, {"confirm": True, "path": "/arbitrary"}):
            with self.assertRaises(ReaderError):
                self.manager.clear_active(body)

    def test_failed_unlink_is_unreferenced_and_remains_available_to_old_cleanup(self):
        self.enable()
        self.action("transcode")
        self.finish()
        output = self.rows("tc_outputs")[0]["file"]
        original = self.manager._output_path
        def locked(name, **kwargs):
            if name == output:
                raise OSError("simulated locked file")
            return original(name, **kwargs)
        with patch.object(self.manager, "_output_path", side_effect=locked):
            result = self.manager.clear_active({"confirm": True})
        self.assertEqual(result["skipped_files"], 1)
        stats = self.manager.cache_stats()
        self.assertEqual(stats["active_files"], 0)
        self.assertEqual(stats["reclaimable_files"], 1)
        self.assertTrue(original(output).is_file())
        self.manager.cleanup({})
        self.assertEqual(self.manager.cache_stats()["files"], 0)

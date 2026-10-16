"""Disposable-cover capacity never evicts uploaded metadata or reading images."""
from contextlib import closing
import os
from pathlib import Path
import time
from unittest.mock import patch

from cache_manager import CacheManager, MIB
from server import ReaderError
from test_transcoding import TranscodingFixture


class LibraryCoversFixture:
    def __init__(self, data):
        self.root = data / "library-covers"
        self.root.mkdir(exist_ok=True)
        self.removed = []

    def cache_entries(self):
        return [dict(path=path, size=path.stat().st_size, modified=path.stat().st_mtime) for path in self.root.glob("*.jpg")]

    def remove_cache_file(self, path):
        if not path.exists():
            return dict(files=0, bytes=0)
        size = path.stat().st_size
        path.unlink()
        self.removed.append(path)
        return dict(files=1, bytes=size)

    def clear_cache(self):
        entries = self.cache_entries()
        results = [self.remove_cache_file(entry["path"]) for entry in entries]
        return dict(files=sum(item["files"] for item in results), bytes=sum(item["bytes"] for item in results))


class CacheManagerTests(TranscodingFixture):
    def setUp(self):
        super().setUp()
        self.library_covers = LibraryCoversFixture(self.data)
        self.cache = CacheManager(self.app, self.library_covers)
        self.addCleanup(self.cache.close)

    def cover_file(self, token, size, *, upload=False, library=False, age=0):
        name = self.book_id + ("-" if library else "-upload-" if upload else "-page-") + token * 24 + ".jpg"
        path = (self.library_covers.root if library else self.app.covers) / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_persistent_admin_limit_compare_and_swap_and_unknown_category(self):
        self.assertEqual(self.cache.settings(), {"revision": 1, "cover_limit_mb": 512})
        updated = self.cache.save_settings({"expected_revision": 1, "cover_limit_mb": 64})
        self.assertEqual(updated["settings"], {"revision": 2, "cover_limit_mb": 64})
        other = CacheManager(self.app, self.library_covers)
        self.addCleanup(other.close)
        self.assertEqual(other.settings()["cover_limit_mb"], 64)
        with self.assertRaises(ReaderError) as error:
            other.save_settings({"expected_revision": 1, "cover_limit_mb": 32})
        self.assertEqual(error.exception.status, 409)
        for body in ({"expected_revision": 2, "cover_limit_mb": True}, {"expected_revision": 2, "cover_limit_mb": -1},
                     {"expected_revision": 2, "cover_limit_mb": 1, "path": "/tmp"}):
            with self.assertRaises(ReaderError):
                self.cache.save_settings(body)
        for body in ({"kind": "pdf", "confirm": True}, {"kind": "covers", "confirm": False},
                     {"kind": "covers", "confirm": True, "ids": [self.book_id]}):
            with self.assertRaises(ReaderError):
                self.cache.clear(body)
        self.assertNotIn("pdf", [row["kind"] for row in self.cache.snapshot()["categories"]])

    def test_capacity_combines_two_generated_cover_types_and_protects_upload_and_optimized(self):
        self.enable()
        self.action("transcode")
        self.finish()
        outputs = self.rows("tc_outputs")
        first = self.cover_file("a", 700_000, age=30)
        second = self.cover_file("b", 700_000, library=True, age=20)
        newest = self.cover_file("c", 700_000, age=10)
        uploaded = self.cover_file("d", 2 * MIB, upload=True, age=50)
        self.cache.save_settings({"expected_revision": 1, "cover_limit_mb": 1})
        result = self.cache.maintain()
        self.assertEqual(result["removed_files"], 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(newest.exists())
        self.assertTrue(uploaded.exists())
        self.assertEqual(self.rows("tc_outputs"), outputs)
        self.assertTrue(all((self.manager.root / row["file"]).is_file() for row in outputs))
        snapshot = self.cache.snapshot()
        self.assertEqual(snapshot["summary"]["disposable_bytes"], 700_000)
        self.assertEqual(snapshot["summary"]["protected_cover_bytes"], 2 * MIB)

    def test_explicit_cover_clear_preserves_uploads_unfamiliar_files_and_source_hashes(self):
        generated = self.cover_file("a", 100)
        uploaded = self.cover_file("b", 200, upload=True)
        stranger = self.app.covers / "notes.txt"
        stranger.write_text("not owned", encoding="utf-8")
        before = self.source_hashes()
        result = self.cache.clear({"kind": "covers", "confirm": True})
        self.assertEqual(result["cleared"]["files"], 1)
        self.assertFalse(generated.exists())
        self.assertTrue(uploaded.exists())
        self.assertTrue(stranger.exists())
        self.assertEqual(self.source_hashes(), before)

    def test_inventory_does_not_follow_hard_links_or_delete_changed_file(self):
        outside = self.root / "external-cover.jpg"
        outside.write_bytes(b"leave untouched")
        linked = self.app.covers / (self.book_id + "-page-" + "e" * 24 + ".jpg")
        try:
            os.link(outside, linked)
        except OSError:
            self.skipTest("Hard links unavailable")
        generated = self.cover_file("a", 20)
        entries, _, _ = self.cache._read_inventory(force=True)
        self.assertEqual([row["path"] for row in entries], [generated])
        generated.write_bytes(b"new generation")
        self.assertEqual(self.cache._remove_page(entries[0]), {"files": 0, "bytes": 0})
        self.cache.clear({"kind": "covers", "confirm": True})
        self.assertTrue(linked.exists())
        self.assertEqual(outside.read_bytes(), b"leave untouched")

    def test_inventory_is_reused_between_polls_and_producer_only_wakes_maintenance(self):
        self.cover_file("a", 200)
        with patch.object(self.cache, "_page_entries", wraps=self.cache._page_entries) as listing:
            self.cache.snapshot()
            self.cache.snapshot()
            self.cache.enforce_cover_limit()
            self.assertEqual(listing.call_count, 1)
            self.assertTrue(self.cache.wake.is_set())
        self.cache.save_settings({"expected_revision": 1, "cover_limit_mb": 0})
        self.assertEqual(self.cache.maintain()["removed_files"], 0)

    def test_library_cover_clear_delegates_and_active_optimized_clear_coordinates(self):
        montage = self.cover_file("a", 200, library=True)
        result = self.cache.clear({"kind": "library_covers", "confirm": True})
        self.assertEqual(result["cleared"]["files"], 1)
        self.assertFalse(montage.exists())
        self.enable()
        self.action("transcode")
        self.finish()
        result = self.cache.clear({"kind": "optimized", "confirm": True, "ids": [self.book_id]})
        self.assertEqual(result["cleared"]["books"], 1)
        self.assertEqual(self.record()["status"], "original")
        self.assertEqual(next(row for row in result["categories"] if row["kind"] == "optimized")["active_files"], 0)

    def test_admin_boundary_applies_to_all_external_cache_methods(self):
        with patch.object(self.app, "require_admin", side_effect=ReaderError("拒绝", 403)):
            for call in (self.cache.snapshot, lambda: self.cache.save_settings({"expected_revision": 1, "cover_limit_mb": 0}),
                         lambda: self.cache.clear({"kind": "covers", "confirm": True})):
                with self.assertRaises(ReaderError) as error:
                    call()
                self.assertEqual(error.exception.status, 403)

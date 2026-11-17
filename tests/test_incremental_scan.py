from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil
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
from incremental_scan import LibraryChangeMonitor, MAX_TARGETS, ScanTarget, event_targets
from server import Library, ReaderError, is_hidden


class IncrementalScanTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "incremental-scan-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.media = self.base / "media"
        self.left = self.media / "left"
        self.right = self.media / "right"
        for root in (self.left, self.right):
            self.image(root / "existing" / "001.png")
        self.app = Library(self.media, self.base / "data", "", 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = False
        self.app.image_search.auto_start = False
        self.id = self.app.save_library({"name": "Scan fixture", "paths": [str(self.left), str(self.right)],
                                         "media_type": "photo", "exclude_rules": ["skip/"]})["library"]["id"]
        self.wait_until(lambda: not self.app.scanning)

    @staticmethod
    def image(path, color="pink"):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 32), color).save(path)

    def wait_until(self, condition):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.025)
        self.fail("Scan fixture did not settle")

    def catalogue(self):
        with closing(self.app.connect()) as connection:
            return tuple(tuple(row) for row in connection.execute("""SELECT id,source_root,path,format,page_count,pages,signature
                FROM books WHERE library_id=? AND active=1 AND removed=0 ORDER BY source_root,path""", (self.id,)))

    def scan(self, *targets):
        self.app._scan_library(self.app._library_config(self.id), list(targets) or None)
        return self.app.scan_task_status()["recent"][0]

    def test_partial_folder_and_archive_match_full_catalogue(self):
        before = self.catalogue()
        self.image(self.left / "existing" / "002.png")
        archive = self.right / "added.cbz"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(self.left / "existing" / "002.png", "001.png")
        record = self.scan(ScanTarget(str(self.left), "existing", "folder"),
                           ScanTarget(str(self.right), archive.name, "file"))
        self.assertEqual((record["mode"], record["directories"], record["done"]), ("incremental", 1, 2))
        partial = self.catalogue()
        self.assertEqual(len(partial), len(before) + 1)
        self.assertIn(before[1], partial)
        self.assertIn((before[0][0], 2), [(row[0], row[4]) for row in partial])
        full = self.scan()
        self.assertGreater(full["directories"], record["directories"])
        self.assertEqual(self.catalogue(), partial)

    def test_move_remove_exclusion_and_unavailable_root(self):
        original = self.catalogue()
        shutil.move(self.left / "existing", self.right / "moved")
        self.image(self.right / "skip" / "ignored.png")
        self.scan(ScanTarget(str(self.left), "existing", "tree"), ScanTarget(str(self.right), "moved", "tree"),
                  ScanTarget(str(self.right), "skip", "tree"))
        moved = self.catalogue()
        self.assertNotIn(original[0], moved)
        self.assertEqual([row[2] for row in moved], ["existing", "moved"])
        directory_path = self.app._directory_path

        def inaccessible(value):
            if value == str(self.right):
                raise ReaderError("Fixture mount unavailable", 422)
            return directory_path(value)

        with patch.object(self.app, "_directory_path", side_effect=inaccessible):
            unavailable = self.scan(ScanTarget(str(self.right), "moved", "tree"))
        self.assertEqual(unavailable["outcome"], "warnings")
        self.assertEqual(moved, self.catalogue())
        shutil.rmtree(self.right / "moved")
        self.scan(ScanTarget(str(self.right), "moved", "tree"))
        self.assertEqual(self.catalogue(), (original[1],))

    def test_fixing_one_root_preserves_warning_at_same_path_on_other_root(self):
        for root in (self.left, self.right):
            (root / "bad.cbz").write_bytes(b"broken")
        self.scan()
        fixed = self.left / "bad.cbz"
        with zipfile.ZipFile(fixed, "w") as handle:
            handle.write(self.left / "existing" / "001.png", "001.png")
        self.scan(ScanTarget(str(self.left), "bad.cbz", "file"))
        config = self.app._library_config(self.id)
        self.assertEqual(json.loads(config["errors"])[0]["scan_scope"][0], str(self.right))
        self.assertEqual(len(self.app.libraries(include_covers=False)["libraries"][0]["errors"]), 1)
        self.scan()
        self.assertEqual(len(self.app.libraries(include_covers=False)["libraries"][0]["errors"]), 1)

    def test_case_only_rename_has_the_same_catalogue_as_full_scan(self):
        source = self.left / "existing"
        temporary = source.with_name("temporary")
        renamed = source.with_name("EXISTING")
        source.rename(temporary)
        temporary.rename(renamed)
        self.scan(ScanTarget(str(self.left), source.name, "tree"), ScanTarget(str(self.left), renamed.name, "tree"))
        partial = self.catalogue()
        self.assertEqual([row[2] for row in partial], ["EXISTING", "existing"])
        self.scan()
        self.assertEqual(partial, self.catalogue())

    @unittest.skipUnless(importlib.util.find_spec("watchdog"), "watchdog is not installed")
    def test_native_events_are_coalesced_and_live_configuration_can_disable_them(self):
        self.assertFalse(self.app.libraries()["libraries"][0]["incremental_scan"])
        with self.assertRaises(ReaderError):
            self.app.save_library({"incremental_scan": "true"}, self.id)
        self.app.change_monitor.start()
        enabled = self.app.save_library({"incremental_scan": True}, self.id)
        self.assertTrue(enabled["library"]["incremental_scan"])
        self.wait_until(lambda: not self.app.scanning and len(self.app.change_monitor._watches) == 2
                        and any(row.get("mode") == "incremental" for row in self.app.scan_task_status()["recent"]))
        start = self.app.scan_task_status()["recent"][0]["endedAt"]
        staging = self.base / "staging"
        self.image(staging / "001.png")
        self.image(staging / "chapter" / "001.png")
        shutil.copytree(staging, self.left / "added")
        self.wait_until(lambda: len(self.catalogue()) == 4 and not self.app.scanning)
        records = [row for row in self.app.scan_task_status()["recent"] if row["endedAt"] > start]
        self.assertEqual([(row["mode"], row["directories"], row["done"]) for row in records],
                         [("incremental", 2, 2)])
        self.image(self.left / "added" / "002.png")
        self.wait_until(lambda: not self.app.scanning and any(row[2] == "added" and row[4] == 2 for row in self.catalogue()))
        shutil.move(self.left / "added", self.right / "added")
        self.wait_until(lambda: not self.app.scanning and len(self.catalogue()) == 4
                        and all(row[1] == str(self.right) for row in self.catalogue() if row[2].startswith("added")))
        shutil.rmtree(self.right / "added")
        self.wait_until(lambda: not self.app.scanning and len(self.catalogue()) == 2)
        disabled = self.app.save_library({"incremental_scan": False}, self.id)
        self.assertFalse(disabled["library"]["incremental_scan"])
        self.wait_until(lambda: not self.app.change_monitor._watches and not self.app.scanning)
        self.image(self.left / "not-watched" / "001.png")
        revision = self.app._library_config(self.id)["revision"]
        self.app._queue_incremental_scan(self.id, revision, {ScanTarget(str(self.left), "not-watched", "tree")})
        self.assertEqual(len(self.catalogue()), 2)
        self.assertTrue(self.app.start_scan(self.id))
        self.wait_until(lambda: not self.app.scanning)
        self.assertEqual(len(self.catalogue()), 3)
        self.assertEqual(self.app.scan_task_status()["recent"][0]["mode"], "full")

    def test_queue_bounds_and_filters_reader_data(self):
        monitor = LibraryChangeMonitor(data=self.base / "data", routes=lambda: {}, directory=lambda value: value,
                                       classify=self.app._scan_event_kind, hidden=is_hidden, submit=lambda *args: None)
        root = str(self.base)
        def event(kind, path, directory=False):
            return SimpleNamespace(event_type=kind, src_path=str(path), dest_path="", is_directory=directory)
        for ignored in (event("modified", self.base / "data" / "cover.jpg"),
                        event("modified", self.left, True),
                        event("opened", self.left / "cover.jpg"),
                        event("created", self.left / ".incomplete" / "cover.jpg")):
            self.assertEqual(list(event_targets(root, self.base / "data", ignored, self.app._scan_event_kind, is_hidden)), [])
        for n in range(MAX_TARGETS + 1):
            monitor._merge(self.id, 1, [ScanTarget(root, f"directory-{n}", "folder")], float(n))
        self.assertIsNone(monitor._batches[self.id][3])


if __name__ == "__main__":
    unittest.main()

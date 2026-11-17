from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database_backup import DATABASE_SCHEMA_VERSION, DatabaseBackupError, database_upgrade, restore_database_backup
from desktop.core import LauncherConfig
from desktop.runtime import serve
from server import Library


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "database-backup-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.data / "library.sqlite3"
        self.backups = self.data / "backups"

    def start(self):
        return Library(self.media, self.data, "", 0)

    def rows(self, path, sql):
        with closing(sqlite3.connect(path)) as db:
            return db.execute(sql).fetchall()

    def create_pre_upgrade_fixture(self):
        album = self.media / "Fixture"
        album.mkdir()
        for number in range(3):
            Image.new("RGB", (20, 30), "pink").save(album / f"{number}.png")
        app = self.start()
        try:
            app.register({"username": "migration-admin", "password": "test-migration-password"}, "fixture")
            app.transcoding.auto_start = False
            app.image_search.auto_start = False
            lib = app.save_library({"name": "Fixture", "media_type": "photo", "path": str(self.media)})["library"]
            app.scan_thread.join(timeout=10)
            self.assertFalse(app.scanning)
            book = app.listing()["books"][0]["id"]
            child = app.save_user({"username": "migration-child", "password": "test-child-password",
                                   "library_ids": [lib["id"]]})["user"]["id"]
            app.save_bookmark(book, {"page": 1, "note": "Before migration"})
            with app.acting_as(child):
                app.set_progress(book, {"page": 2, "completed": True})
            share = app.sharing.create({"book_id": book, "days": 1, "password": "test-share-password"})["share"]
        finally:
            app.close()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TRIGGER catalog_revision_media_libraries_update")
            db.execute("ALTER TABLE media_libraries DROP COLUMN incremental_scan")
            db.execute("DROP TABLE task_run_history")
            db.execute("PRAGMA user_version=0")
        return child, share["id"]

    def assert_fixture_data(self, app, child, share):
        self.assertEqual(app.login({"username": "migration-admin", "password": "test-migration-password"},
                                   "fixture"), 1)
        self.assertEqual(app.login({"username": "migration-child", "password": "test-child-password"},
                                   "fixture"), child)
        self.assertEqual(app.bookmarks()["bookmarks"][0]["note"], "Before migration")
        with app.acting_as(child):
            progress = app.listing()["books"][0]["progress"]
            self.assertEqual(progress["page"], 2)
            self.assertTrue(progress["completed"])
        self.assertEqual(app.sharing.reveal_password(share), {"password": "test-share-password"})

    def test_backup_precedes_all_initializers_and_is_not_repeated_on_restart(self):
        child, share = self.create_pre_upgrade_fixture()
        first = self.start()
        try:
            backup = first.migration_backup
            self.assertIsNotNone(backup)
            self.assertEqual(self.rows(backup, "PRAGMA quick_check"), [("ok",)])
            self.assertEqual(self.rows(backup, "PRAGMA journal_mode"), [("delete",)])
            self.assertEqual(self.rows(backup, "PRAGMA user_version"), [(0,)])
            self.assertNotIn("incremental_scan", [row[1] for row in self.rows(backup, "PRAGMA table_info(media_libraries)")])
            self.assertEqual(self.rows(backup, "PRAGMA table_info(task_run_history)"), [])
            self.assertEqual(self.rows(self.database, "PRAGMA user_version"), [(DATABASE_SCHEMA_VERSION,)])
            self.assert_fixture_data(first, child, share)
        finally:
            first.close()
        second = self.start()
        try:
            self.assertIsNone(second.migration_backup)
            self.assertEqual(list(self.backups.glob("*.sqlite3")), [backup])
            self.assertFalse((self.backups / ".schema-upgrade.json").exists())
        finally:
            second.close()
        (self.backups / ".schema-upgrade.json").write_text("unreadable residue", encoding="utf-8")
        restarted = self.start()
        try:
            self.assertIsNone(restarted.migration_backup)
        finally:
            restarted.close()

    def test_late_failure_logs_backup_reuses_it_and_offline_restore_recovers_records(self):
        child, share = self.create_pre_upgrade_fixture()
        initialize = Library._initialize_catalog_revision

        def fail_after_changes(app):
            initialize(app)
            with closing(app.connect()) as db, db:
                db.execute("UPDATE bookmarks SET note='Partial writes'")
            raise sqlite3.OperationalError("Test late failure")

        output = StringIO()
        config = LauncherConfig(library_paths=[str(self.media)])
        with (
            patch.object(Library, "_initialize_catalog_revision", fail_after_changes),
            self.assertLogs("sumi", level="ERROR") as logs,
            redirect_stdout(output),
        ):
            self.assertEqual(serve(config, self.data, instance="fixture-instance"), 1)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0], {"stage": "database_upgrade", "instance": "fixture-instance"})
        self.assertFalse(records[1]["ready"])
        backup, = self.backups.glob("before-schema-upgrade-*.sqlite3")
        self.assertIn(str(backup), records[1]["error"])
        self.assertIn(str(backup), "\n".join(logs.output))
        self.assertEqual(self.rows(self.database, "SELECT note FROM bookmarks"), [("Partial writes",)])
        self.assertEqual(self.rows(self.database, "PRAGMA user_version"), [(0,)])
        self.assertEqual(self.rows(backup, "SELECT note FROM bookmarks"), [("Before migration",)])
        with patch.object(Library, "_initialize_catalog_revision", fail_after_changes):
            with self.assertRaisesRegex(DatabaseBackupError, "Test late failure"):
                self.start()
        self.assertEqual(list(self.backups.glob("before-schema-upgrade-*.sqlite3")), [backup])

        previous = restore_database_backup(self.data, backup.name)
        self.assertEqual(self.rows(previous, "SELECT note FROM bookmarks"), [("Partial writes",)])
        self.assertEqual(self.rows(self.database, "SELECT note FROM bookmarks"), [("Before migration",)])
        self.assertEqual(self.rows(self.database, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self.rows(self.database, "PRAGMA foreign_key_check"), [])
        self.assertFalse((self.backups / ".schema-upgrade.json").exists())
        self.assertNotIn("incremental_scan", [row[1] for row in self.rows(self.database, "PRAGMA table_info(media_libraries)")])
        restored = self.start()
        try:
            self.assert_fixture_data(restored, child, share)
        finally:
            restored.close()

    def test_snapshot_includes_committed_wal_and_is_standalone(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE fixture(value)")
            writer.execute("INSERT INTO fixture VALUES('committed WAL value')")
            writer.commit()
            self.assertGreater((self.data / "library.sqlite3-wal").stat().st_size, 0)
            with database_upgrade(self.database) as backup:
                self.assertEqual(self.rows(backup, "SELECT value FROM fixture"), [("committed WAL value",)])
                self.assertFalse(backup.with_name(backup.name + "-wal").exists())
        self.assertEqual(self.rows(self.database, "PRAGMA user_version"), [(DATABASE_SCHEMA_VERSION,)])

    def test_backup_write_failure_aborts_before_schema_changes(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE fixture(value)")
        original = self.database.read_bytes()
        with patch("database_backup._sqlite_copy", side_effect=sqlite3.OperationalError("No fixture space")):
            with self.assertRaisesRegex(DatabaseBackupError, "No fixture space"):
                self.start()
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_restore_rejects_a_running_service_and_names_outside_backups(self):
        app = self.start()
        try:
            self.assertFalse(self.backups.exists(), "New database should not need a snapshot")
            with self.assertRaisesRegex(DatabaseBackupError, "请先停止"):
                restore_database_backup(self.data, "before-schema-upgrade-v0-to-v1-test.sqlite3")
        finally:
            app.close()
        with self.assertRaisesRegex(DatabaseBackupError, "备份文件名"):
            restore_database_backup(self.data, "../before-schema-upgrade-v0-to-v1-test.sqlite3")

    def test_newer_database_is_refused_before_any_initializer_changes_it(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION + 1}")
        original = self.database.read_bytes()
        with self.assertRaisesRegex(DatabaseBackupError, "结构高于此版本"):
            self.start()
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse(self.backups.exists())


if __name__ == "__main__":
    unittest.main()

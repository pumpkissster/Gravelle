"""Early local transcoding schemas preserve data and legacy catalogue IDs."""
from contextlib import closing
import hashlib
import re
import sqlite3
import unittest

from server import Library
from test_transcoding import TranscodingFixture


class TranscodingMigrationTests(TranscodingFixture):
    def early_local_schema(self):
        self.app.close()
        # Recreate the unreleased first schema, whose new child tables used
        # SQLite's default ON UPDATE NO ACTION. Only disposable fixture data.
        with closing(sqlite3.connect(self.data / "library.sqlite3")) as db, db:
            for table in ("tc_books", "tc_pages", "tc_outputs"):
                create = db.execute("SELECT sql FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone()[0]
                indexes = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL", (table,))]
                earlier = table + "_early"
                create = re.sub(r"(?i)^CREATE TABLE [\"`\[]?" + table + r"[\"`\]]?", "CREATE TABLE " + earlier, create, count=1)
                create = create.replace(" ON UPDATE CASCADE", "")
                db.execute(create)
                db.execute(f"INSERT INTO {earlier} SELECT * FROM {table}")
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {earlier} RENAME TO {table}")
                for statement in indexes:
                    db.execute(statement)
            self.assertTrue(all(row[5] == "NO ACTION" for table in ("tc_books", "tc_pages", "tc_outputs")
                                for row in db.execute(f"PRAGMA foreign_key_list({table})")))

    def reopen(self):
        self.app = Library(self.media, self.data, "", 0)
        self.app.transcoding.auto_start = False

    def test_early_schema_upgrade_preserves_outputs_sources_and_personal_records(self):
        self.enable()
        self.action("transcode")
        self.finish()
        self.app.set_progress(self.book_id, {"page": 1, "completed": False})
        self.app.set_favorite(self.book_id, {"favorite": True})
        self.app.save_bookmark(self.book_id, {"page": 1, "note": "migration fixture"})
        before_sources = self.source_hashes()
        before_rows = {table: self.rows(table) for table in ("tc_books", "tc_pages", "tc_outputs")}
        before_output = self.app.image(self.book_id, 0)
        before_files = {row["file"]: hashlib.sha256((self.manager.root / row["file"]).read_bytes()).hexdigest()
                        for row in before_rows["tc_outputs"]}
        self.early_local_schema()
        self.reopen()
        for table, records in before_rows.items():
            self.assertEqual(self.rows(table), records)
            with closing(self.app.connect()) as db:
                self.assertEqual({row["on_update"] for row in db.execute(f"PRAGMA foreign_key_list({table})")}, {"CASCADE"})
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.app.image(self.book_id, 0), before_output)
        self.assertEqual(self.app.book(self.book_id)["progress"]["page"], 1)
        self.assertTrue(self.app.book(self.book_id)["favorite"])
        self.assertEqual(self.app.bookmarks()["bookmarks"][0]["note"], "migration fixture")
        for filename, digest in before_files.items():
            self.assertEqual(hashlib.sha256((self.manager.root / filename).read_bytes()).hexdigest(), digest)
        self.assertEqual(self.source_hashes(), before_sources)

    def test_early_schema_upgrade_does_not_block_historical_book_id_assignment(self):
        # Legacy ID seeding occurs before personal/metadata rows are created.
        # The new derived tables must not add an earlier foreign-key obstacle.
        with closing(self.app.connect()) as db, db:
            db.execute("DELETE FROM book_models WHERE book_id=?", (self.book_id,))
            db.execute("DELETE FROM book_metadata WHERE book_id=?", (self.book_id,))
        self.early_local_schema()
        self.reopen()
        historical_id = "b" * 24
        with closing(self.app.connect()) as db, db:
            db.execute("UPDATE books SET id=? WHERE id=?", (historical_id, self.book_id))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.rows("tc_books", historical_id)[0]["book_id"], historical_id)
        self.assertEqual(len(self.rows("tc_pages", historical_id)), 3)
        self.assertEqual(self.rows("tc_books"), [])
        self.assertEqual(self.app.image(historical_id, 0)[0], self.sources[0])


if __name__ == "__main__":
    unittest.main()

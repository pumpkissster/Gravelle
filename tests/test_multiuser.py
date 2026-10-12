"""Focused temporary-data checks for account migration and personal state."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from accounts import hash_password, migrate_accounts, prepare_account_migration
from server import Library, ReaderError


class MultiuserTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(os.environ.get('SUMI_TEST_TEMP', ROOT / 'work' / 'multiuser-tests'))
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='multiuser-', dir=temporary_root)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        for name in ('甲库', '乙库'):
            chapter = self.media / name / '作品'
            chapter.mkdir(parents=True)
            (chapter / '1.png').write_bytes(b'first-image')
            (chapter / '2.png').write_bytes(b'second-image')
        self.app = Library(self.media, self.root / 'data', '', 0)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.app.close)
        self.app.register({'username': 'admin', 'password': 'admin-test-password'}, 'local-test')
        self.libraries = [self.app.save_library({'name': name, 'path': str(self.media / name)})['library']
                          for name in ('甲库', '乙库')]
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.books = {book['library_id']: book for book in self.app.listing()['books']}

    def create(self, name='child', library_ids=None):
        return self.app.save_user({'username': name, 'password': 'child-test-password',
                                  'library_ids': library_ids or [self.libraries[0]['id']]})['user']

    def assert_reader_error(self, status, function, *args):
        with self.assertRaises(ReaderError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, status)

    def test_account_acl_and_no_shared_management(self):
        child = self.create()
        self.assertEqual(child['role'], 'user')
        self.assertFalse(any(child['permissions'].values()))
        with self.app.acting_as(child['id']):
            self.assertEqual(len(self.app.listing()['books']), 1)
            self.assert_reader_error(404, self.app.book, self.books[self.libraries[1]['id']]['id'])
            self.assert_reader_error(403, self.app.save_book_metadata, self.books[self.libraries[0]['id']]['id'], {'title': 'no'})
            self.assert_reader_error(403, self.app.save_model, {'name': 'no'})
            self.assert_reader_error(403, self.app.start_scan, self.libraries[0]['id'])
            self.assert_reader_error(403, self.app.users)
            self.assert_reader_error(403, self.app.directories)
            self.assert_reader_error(403, self.app.save_profile, {'role': 'admin'}, 'local-test')
        self.assert_reader_error(403, self.app.save_user, {'permissions': {'edit_metadata': True}}, child['id'])
        self.assert_reader_error(403, self.app.delete_user, 1)
        self.assert_reader_error(403, self.app.save_user, {'enabled': False}, 1)

    def test_independent_personal_state_and_preferences(self):
        first, second = self.create('first'), self.create('second')
        book_id = self.books[self.libraries[0]['id']]['id']
        with self.app.acting_as(first['id']):
            self.app.set_progress(book_id, {'page': 1, 'completed': True})
            self.app.set_favorite(book_id, {'favorite': True})
            bookmark = self.app.save_bookmark(book_id, {'page': 1, 'note': 'private'})[0]['bookmark']
            self.app.save_reading_preferences(book_id, {'reading_mode': 'double', 'reading_direction': 'rtl'})
            self.assertEqual(self.app.book(book_id)['reading_mode'], 'double')
        with self.app.acting_as(second['id']):
            book = self.app.book(book_id)
            self.assertIsNone(book['progress'])
            self.assertFalse(book['favorite'])
            self.assertEqual(book['bookmarks'], [])
            self.assertEqual(book['reading_preferences'], {'reading_mode': None, 'reading_direction': None})
            self.assert_reader_error(404, self.app.delete_bookmark, bookmark['id'])
            self.app.save_bookmark(book_id, {'page': 1, 'note': 'other'})
        self.assertEqual(self.app.book(book_id)['reading_mode'], 'single')
        self.assertIsNone(self.app.book(book_id)['progress'])

    def test_personal_hidden_libraries_and_authorization_changes(self):
        ids = [item['id'] for item in self.libraries]
        child = self.create(library_ids=ids)
        with self.app.acting_as(child['id']):
            self.app.save_my_libraries({'visible_library_ids': []})
            self.assertEqual(self.app.libraries()['libraries'], [])
            self.assertEqual(self.app.listing()['books'], [])
            self.assertTrue(all(not item['visible'] for item in self.app.my_libraries()['libraries']))
            # Hiding is personal presentation, not loss of access.
            self.assertEqual(self.app.image(self.books[ids[0]]['id'], 0)[0], b'first-image')
            bookmark = self.app.save_bookmark(self.books[ids[0]]['id'], {'page': 0, 'note': 'hidden direct'})[0]['bookmark']
            self.assertEqual(self.app.book(self.books[ids[0]]['id'])['bookmarks'], [bookmark])
            self.assertEqual(self.app.bookmarks()['bookmarks'], [])
            self.assert_reader_error(404, self.app.save_my_libraries, {'visible_library_ids': ['f' * 24]})
        self.app.save_user({'username': 'renamed'}, child['id'])
        with self.app.acting_as(child['id']):
            self.assertEqual(self.app.listing()['books'], [])
        self.app.save_user({'library_ids': ids[:1]}, child['id'])
        self.app.save_user({'library_ids': ids}, child['id'])
        with self.app.acting_as(child['id']):
            preferences = {item['id']: item['visible'] for item in self.app.my_libraries()['libraries']}
            self.assertFalse(preferences[ids[0]])
            self.assertTrue(preferences[ids[1]])

    def test_administrator_visibility_is_personal_and_management_keeps_all_choices(self):
        ids = [item['id'] for item in self.libraries]
        child = self.create(library_ids=ids)
        book_id = self.books[ids[0]]['id']
        model = self.app.save_model({'name': '作品模特'})[0]['model']
        self.app.save_book_metadata(book_id, {'model_ids': [model['id']]})
        self.app.set_favorite(book_id, {'favorite': True})
        self.app.save_bookmark(book_id, {'page': 0})
        self.app.save_my_libraries({'visible_library_ids': []})
        self.assertEqual(self.app.libraries()['libraries'], [])
        self.assertEqual(self.app.listing()['books'], [])
        self.assertEqual(self.app.bookmarks()['bookmarks'], [])
        self.assertNotIn(model['id'], [item['id'] for item in self.app.models()['models']])
        self.assertEqual({item['id'] for item in self.app.users()['libraries']}, set(ids))
        self.assertEqual(self.app.image(book_id, 0)[0], b'first-image')
        # An edit to a hidden library still returns its management response.
        self.assertEqual(self.app.save_library({'name': '隐藏库改名'}, ids[0])['library']['id'], ids[0])
        with self.app.acting_as(child['id']):
            self.assertEqual(len(self.app.libraries()['libraries']), 2)
        self.app.save_my_libraries({'visible_library_ids': ids})
        self.assertEqual(len(self.app.listing()['books']), 2)

    def test_sessions_disabled_reset_delete_and_password_scope(self):
        child, other = self.create('child'), self.create('other')
        first = self.app.make_session(child['id'])
        other_session = self.app.make_session(other['id'])
        self.app.save_user({'library_ids': [item['id'] for item in self.libraries]}, child['id'])
        self.assertFalse(self.app.session(first)['authenticated'])
        self.assertTrue(self.app.session(other_session)['authenticated'])
        token = self.app.make_session(child['id'])
        with self.app.acting_as(child['id']):
            self.app.save_profile({'current_password': 'child-test-password', 'password': 'new-child-password'}, 'local-test')
        self.assertFalse(self.app.session(token)['authenticated'])
        self.assertTrue(self.app.session(other_session)['authenticated'])
        token = self.app.make_session(child['id'])
        self.app.save_user({'enabled': False}, child['id'])
        self.assertFalse(self.app.session(token)['authenticated'])
        self.app.save_user({'enabled': True, 'password': 'admin-reset-password'}, child['id'])
        self.assertEqual(self.app.login({'username': 'child', 'password': 'admin-reset-password'}, 'local-test'), child['id'])
        self.app.delete_user(child['id'])
        replacement = self.create('replacement')
        self.assertGreater(replacement['id'], child['id'])
        self.assertTrue(self.app.session(other_session)['authenticated'])
        self.assertEqual(len(self.app.listing()['books']), 2)

    def test_bulk_favorite_rechecks_access_after_concurrent_revocation(self):
        child = self.create()
        book_id = self.books[self.libraries[0]['id']]['id']
        checked, revoked = threading.Event(), threading.Event()
        failures = []
        original_book = self.app.book
        def revoke():
            try:
                if not checked.wait(5):
                    raise AssertionError('bulk operation did not reach preliminary ACL check')
                self.app.save_user({'library_ids': []}, child['id'])
            except BaseException as error:
                failures.append(error)
            finally:
                revoked.set()
        def pause_after_check(*args, **kwargs):
            result = original_book(*args, **kwargs)
            checked.set()
            if not revoked.wait(5):
                raise AssertionError('administrator did not finish revocation')
            return result
        administrator = threading.Thread(target=revoke)
        administrator.start()
        try:
            with self.app.acting_as(child['id']), patch.object(self.app, 'book', side_effect=pause_after_check):
                self.assert_reader_error(404, self.app.batch_books,
                                         {'ids': [book_id], 'action': 'favorite', 'favorite': True})
        finally:
            administrator.join(timeout=6)
        self.assertFalse(administrator.is_alive())
        if failures:
            raise failures[0]
        with closing(self.app.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM favorites WHERE user_id=?', (child['id'],)).fetchone()[0], 0)


class MigrationTests(unittest.TestCase):
    def test_existing_visibility_preferences_upgrade_for_administrator_without_losing_child_rows(self):
        temporary_root = ROOT / 'work' / 'multiuser-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temp:
            database = Path(temp) / 'library.sqlite3'
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript('''
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE users(id INTEGER PRIMARY KEY);
                    CREATE TABLE media_libraries(id TEXT PRIMARY KEY);
                    CREATE TABLE user_libraries(user_id INTEGER,library_id TEXT,PRIMARY KEY(user_id,library_id));
                    CREATE TABLE user_hidden_libraries(user_id INTEGER,library_id TEXT,
                        PRIMARY KEY(user_id,library_id),
                        FOREIGN KEY(user_id,library_id) REFERENCES user_libraries(user_id,library_id) ON DELETE CASCADE);
                    INSERT INTO users VALUES(1),(2);
                    INSERT INTO media_libraries VALUES('library');
                    INSERT INTO user_libraries VALUES(2,'library');
                    INSERT INTO user_hidden_libraries VALUES(2,'library');
                ''')
            self.assertFalse(migrate_accounts(database))
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute("INSERT INTO user_hidden_libraries VALUES(1,'library')")
                self.assertEqual(db.execute('SELECT * FROM user_hidden_libraries ORDER BY user_id').fetchall(),
                                 [(1,'library'),(2,'library')])
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_migration_backup_preserves_owner_and_records_and_revokes_sessions(self):
        temporary_root = ROOT / 'work' / 'multiuser-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temp:
            database = Path(temp) / 'library.sqlite3'
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript('''
                    CREATE TABLE books(id TEXT PRIMARY KEY);
                    CREATE TABLE media_libraries(id TEXT PRIMARY KEY);
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE models(id TEXT PRIMARY KEY);
                    CREATE TABLE owner_account(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT,
                        normalized_username TEXT,display_name TEXT,avatar_color TEXT,password_hash TEXT,
                        created_at REAL,updated_at REAL,avatar_file TEXT,avatar_revision INTEGER);
                    CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER,
                        created_at REAL,expires_at REAL);
                    CREATE INDEX auth_sessions_expiry ON auth_sessions(expires_at);
                    CREATE TABLE progress(book_id TEXT PRIMARY KEY,page INTEGER,completed INTEGER,updated_at REAL);
                    CREATE TABLE favorites(book_id TEXT PRIMARY KEY,favorite INTEGER);
                    CREATE TABLE bookmarks(id TEXT PRIMARY KEY,book_id TEXT,page INTEGER,note TEXT,
                        created_at REAL,UNIQUE(book_id,page));
                    INSERT INTO books VALUES('book');
                    INSERT INTO progress VALUES('book',3,1,12);
                    INSERT INTO favorites VALUES('book',1);
                    INSERT INTO bookmarks VALUES('bookmark','book',3,'old note',10);
                    INSERT INTO auth_sessions VALUES('old-token',1,1,9999999999);
                ''')
                original = (1,'old','old','旧名字','#abcdef',hash_password('old-test-password'),1,2,'user-1-old.jpg',9)
                db.execute('INSERT INTO owner_account VALUES(?,?,?,?,?,?,?,?,?,?)', original)
            backup = prepare_account_migration(database)
            self.assertTrue(backup.is_file())
            self.assertTrue(migrate_accounts(database))
            with closing(sqlite3.connect(database)) as db:
                fields = 'id,username,normalized_username,display_name,avatar_color,password_hash,created_at,updated_at,avatar_file,avatar_revision'
                self.assertEqual(db.execute('SELECT '+fields+' FROM users').fetchone(), original)
                self.assertEqual(db.execute('SELECT role,enabled FROM users').fetchone(), ('admin',1))
                self.assertEqual(db.execute('SELECT user_id,book_id,page,completed,updated_at FROM progress').fetchone(), (1,'book',3,1,12))
                self.assertEqual(db.execute('SELECT user_id,favorite FROM favorites').fetchone(), (1,1))
                self.assertEqual(db.execute('SELECT id,user_id,note FROM bookmarks').fetchone(), ('bookmark',1,'old note'))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0],0)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            with closing(sqlite3.connect(backup)) as old:
                self.assertEqual(old.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0],1)
            self.assertIsNone(prepare_account_migration(database))
            self.assertFalse(migrate_accounts(database))


if __name__ == '__main__':
    unittest.main()

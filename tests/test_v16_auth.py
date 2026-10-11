"""Owner registration and revocable authentication with disposable databases."""
from contextlib import closing
from io import BytesIO
import http.cookiejar
import json
import sqlite3
import unittest
import urllib.request

from PIL import Image

from test_integration import RunningApp


class V15DatabaseApp(RunningApp):
    """Released 1.5 schema, before any account or session tables existed.

    Reuse the actual historical 1.1–1.4 fixtures and apply only the 1.5
    multi-root/author/cover columns checked against the released 1.5 archive.
    """
    def seed_legacy_database(self):
        super().seed_legacy_database()
        self.seed_v11_libraries()
        self.seed_v12_photo_metadata()
        self.seed_v13_photo_metadata()
        self.seed_v14_photo_metadata()
        self.second_root = self.library / '第二目录'
        self.second_root.mkdir()
        image = BytesIO()
        Image.new('RGB', (12, 18), (120, 80, 40)).save(image, 'JPEG')
        self.old_cover = image.getvalue()
        self.old_cover_name = self.legacy_id + '-upload-' + 'a' * 24 + '.jpg'
        covers = self.data / 'covers'
        covers.mkdir()
        (covers / self.old_cover_name).write_bytes(self.old_cover)
        self.old_bookmark_id = 'b' * 24
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as db, db:
            db.executescript('''
                ALTER TABLE media_libraries ADD COLUMN paths TEXT NOT NULL DEFAULT '[]';
                ALTER TABLE media_libraries ADD COLUMN default_author_id TEXT REFERENCES models(id);
                ALTER TABLE books ADD COLUMN source_root TEXT NOT NULL DEFAULT '';
                ALTER TABLE book_metadata ADD COLUMN cover_source TEXT NOT NULL DEFAULT 'page';
                ALTER TABLE book_metadata ADD COLUMN cover_file TEXT NOT NULL DEFAULT '';
                ALTER TABLE book_metadata ADD COLUMN cover_revision INTEGER NOT NULL DEFAULT 0;
            ''')
            db.execute('UPDATE media_libraries SET paths=?,default_author=?,default_author_id=?',
                       (json.dumps([str(self.library), str(self.second_root)]),
                        '原有模特', self.legacy_model_id))
            db.execute('UPDATE books SET source_root=?', (str(self.library),))
            db.execute("UPDATE book_metadata SET cover_source='upload',cover_file=?,cover_revision=3 WHERE book_id=?",
                       (self.old_cover_name, self.legacy_id))
            db.execute('INSERT INTO bookmarks VALUES(?,?,?,?,?)',
                       (self.old_bookmark_id, self.legacy_id, 1, '升级前的书签', 456))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'owner_account' not in tables and 'auth_sessions' not in tables


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp(auto_register=False)
        self.addCleanup(self.app.close)
        self.credentials = {'username': '个人书房', 'password': 'test-only-owner-passphrase',
                            'display_name': '测试藏馆'}

    def register(self, **changes):
        status, data, headers = self.app.request('/api/register', {**self.credentials, **changes})
        self.assertEqual(status, 201, data)
        return data, headers

    def new_client(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def test_first_setup_protects_media_and_registration_creates_owner(self):
        session = self.app.request('/api/session')[1]
        self.assertFalse(session['authenticated'])
        self.assertTrue(session['registration_required'])
        self.assertIsNone(session['user'])
        for route, body in [('/api/library', None), ('/api/models', None),
                            ('/api/directories', None), ('/api/profile', {'display_name': '不能修改'})]:
            self.assertEqual(self.app.request(route, body)[0], 401)
        data, headers = self.register()
        self.assertTrue(data['authenticated'])
        self.assertFalse(data['registration_required'])
        self.assertEqual(data['user']['username'], self.credentials['username'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.app.request('/api/library')[0], 200)
        self.assertEqual(self.app.request('/api/register', {**self.credentials, 'username': '另一个人'})[0], 409)
        self.assertNotIn(self.credentials['password'], json.dumps(data))

    def test_registration_validation_is_atomic(self):
        cases = ({'username': ''}, {'username': ['user']}, {'username': 'a'},
                 {'username': 'a b'}, {'password': 'short'}, {'password': []},
                 {'password': 'a' * 257}, {'display_name': '\x00不能保存'})
        # Keep each validation batch below the real eight-failure rate limit;
        # this test checks atomic validation, while rate limiting has its own test.
        for offset in range(0, len(cases), 4):
            app = self.app if offset == 0 else RunningApp(auto_register=False)
            if app is not self.app:
                self.addCleanup(app.close)
            for changes in cases[offset:offset + 4]:
                with self.subTest(changes=changes):
                    status = app.request('/api/register', {**self.credentials, **changes})[0]
                    self.assertIn(status, (400, 422))
                    self.assertTrue(app.request('/api/session')[1]['registration_required'])
            self.assertEqual(app.request('/api/register', self.credentials)[0], 201)

    def test_login_logout_revokes_replayed_cookie_and_session_survives_restart(self):
        self.register()
        original_cookie = '; '.join(f'{c.name}={c.value}' for c in self.app.cookies)
        self.app.stop(); self.app.start()
        self.assertTrue(self.app.request('/api/session')[1]['authenticated'])
        self.assertEqual(self.app.request('/api/logout', {})[0], 200)
        self.assertFalse(self.app.request('/api/session')[1]['authenticated'])
        self.assertFalse(self.app.request('/api/session', headers={'Cookie': original_cookie})[1]['authenticated'])
        self.assertEqual(self.app.request('/api/library', headers={'Cookie': original_cookie})[0], 401)
        self.assertEqual(self.app.request('/api/login', {'username': self.credentials['username'], 'password': 'wrong'})[0], 401)
        self.assertEqual(self.app.request('/api/login', self.credentials)[0], 200)
        self.assertTrue(self.app.request('/api/session')[1]['authenticated'])

    def test_profile_edit_persists_and_never_exposes_password_material(self):
        self.register()
        status, result, _ = self.app.request('/api/profile', {
            'username': 'new-reader', 'display_name': '新的名字', 'avatar_color': '#526d82'})
        self.assertEqual(status, 200, result)
        self.app.stop(); self.app.start()
        user = self.app.request('/api/session')[1]['user']
        self.assertEqual((user['username'], user['display_name'], user['avatar_color']),
                         ('new-reader', '新的名字', '#526d82'))
        self.assertFalse(any(word in key for key in user for word in ('password', 'salt', 'hash')))
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as connection:
            dump = '\n'.join(connection.iterdump())
            self.assertNotIn(self.credentials['password'], dump)
        self.app.request('/api/logout', {})
        self.assertEqual(self.app.request('/api/login', {'username': self.credentials['username'],
                                                        'password': self.credentials['password']})[0], 401)
        self.assertEqual(self.app.request('/api/login', {'username': 'new-reader',
                                                        'password': self.credentials['password']})[0], 200)

    def test_invalid_profile_edit_preserves_all_fields(self):
        self.register()
        original = self.app.request('/api/session')[1]['user']
        for invalid in ({'username': 'bad name'}, {'display_name': ''}, {'avatar_color': 'red'},
                        {'password': 'short', 'current_password': self.credentials['password']},
                        {'password': 'new-test-only-password'},
                        {'password': 'new-test-only-password', 'current_password': 'wrong'}):
            with self.subTest(invalid=invalid):
                body = {'display_name': '不应部分保存', **invalid}
                self.assertIn(self.app.request('/api/profile', body)[0], (400, 401, 403, 422))
                self.assertEqual(self.app.request('/api/session')[1]['user'], original)

    def test_password_change_invalidates_other_sessions_and_old_password(self):
        self.register()
        first = self.app.client
        self.app.client = self.new_client()
        self.assertEqual(self.app.request('/api/login', self.credentials)[0], 200)
        second = self.app.client
        self.app.client = first
        status, result, _ = self.app.request('/api/profile', {
            'current_password': self.credentials['password'], 'password': 'new-test-only-password'})
        self.assertEqual(status, 200, result)
        self.assertTrue(self.app.request('/api/session')[1]['authenticated'])
        self.app.client = second
        self.assertFalse(self.app.request('/api/session')[1]['authenticated'])
        self.assertEqual(self.app.request('/api/login', self.credentials)[0], 401)
        self.assertEqual(self.app.request('/api/login', {**self.credentials, 'password': 'new-test-only-password'})[0], 200)

    def test_cross_origin_account_mutations_are_rejected(self):
        headers = {'Origin': 'https://example.invalid'}
        self.assertEqual(self.app.request('/api/register', self.credentials, headers)[0], 403)
        self.register()
        for route, body in [('/api/profile', {'display_name': '不应保存'}),
                            ('/api/logout', {}), ('/api/login', self.credentials)]:
            self.assertEqual(self.app.request(route, body, headers)[0], 403)
        self.assertTrue(self.app.request('/api/session')[1]['authenticated'])

    def test_repeated_login_failures_are_limited(self):
        self.register(); self.app.request('/api/logout', {})
        statuses = [self.app.request('/api/login', {'username': 'missing-user', 'password': 'wrong-password'})[0]
                    for _ in range(9)]
        self.assertTrue(all(status in (401, 429) for status in statuses))
        self.assertEqual(statuses[-1], 429)

    def test_account_setup_preserves_existing_library_and_reading_state(self):
        self.register()
        library = self.app.create_library()
        book = self.app.books(library['id'])['books'][0]
        route = '/api/books/' + book['id']
        self.app.request(route + '/progress', {'page': 1, 'completed': False})
        self.app.request(route + '/favorite', {'favorite': True})
        self.app.request('/api/logout', {})
        self.assertEqual(self.app.request('/api/login', self.credentials)[0], 200)
        self.app.stop(); self.app.start()
        after = self.app.request(route)[1]
        self.assertEqual((after['id'], after['added_at'], after['progress']['page'], after['favorite']),
                         (book['id'], book['added_at'], 1, True))


class LegacySetupGateTests(unittest.TestCase):
    def test_legacy_password_gates_registration_then_username_login_takes_over(self):
        app = RunningApp(password='legacy-test-password', auto_register=False)
        self.addCleanup(app.close)
        body = {'username': 'owner', 'password': 'new-test-only-password'}
        self.assertIn(app.request('/api/register', body)[0], (400, 401, 403))
        status, result, _ = app.request('/api/register', {**body, 'setup_password': 'legacy-test-password'})
        self.assertEqual(status, 201, result)
        app.request('/api/logout', {})
        self.assertIn(app.request('/api/login', {'password': 'legacy-test-password'})[0], (400, 401))
        self.assertEqual(app.request('/api/login', body)[0], 200)


class V15AccountMigrationTests(unittest.TestCase):
    def test_v15_catalogue_metadata_covers_and_history_survive_first_registration(self):
        app = V15DatabaseApp(legacy='v1.5', auto_register=False)
        self.addCleanup(app.close)
        session = app.request('/api/session')[1]
        self.assertTrue(session['registration_required'])
        self.assertFalse(session['authenticated'])
        self.assertEqual(app.request('/api/library')[0], 401)
        credentials = {'username': '升级账户', 'password': 'migration-test-only-password'}
        self.assertEqual(app.request('/api/register', credentials)[0], 201)
        library = app.request('/api/libraries')[1]['libraries'][0]
        self.assertEqual(library['id'], app.legacy_library_id)
        self.assertEqual(library['paths'], [str(app.library), str(app.second_root)])
        self.assertEqual(library['default_author_id'], app.legacy_model_id)
        self.assertTrue(library['hide_date_prefix'])
        book = next(item for item in app.books(library['id'])['books'] if item['id'] == app.legacy_id)
        expected = ('原有自定义标题', '2020-01-02', 'double', 'ltr', 1, 1, True)

        def preserved(item):
            return (item['title'], item['release_date'], item['reading_mode_override'],
                    item['reading_direction_override'], item['added_at'], item['progress']['page'], item['favorite'])

        self.assertEqual(preserved(book), expected)
        self.assertEqual([model['id'] for model in book['models']], [app.legacy_model_id])
        self.assertEqual(book['cover_source'], 'upload')
        route = '/api/books/' + app.legacy_id
        self.assertEqual(app.request(route + '/cover')[1], app.old_cover)
        bookmarks = app.request('/api/bookmarks')[1]['bookmarks']
        self.assertEqual([(item['id'], item['page'], item['note'], item['created_at']) for item in bookmarks],
                         [(app.old_bookmark_id, 1, '升级前的书签', 456)])
        app.stop(); app.start()
        self.assertTrue(app.request('/api/session')[1]['authenticated'])
        app.books(library['id'])
        self.assertEqual(preserved(app.request(route)[1]), expected)
        self.assertEqual(app.request(route + '/cover')[1], app.old_cover)
        self.assertTrue((app.data / 'covers' / app.old_cover_name).is_file())


if __name__ == '__main__':
    unittest.main()

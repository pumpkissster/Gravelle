"""Black-box integration checks; run: python -m unittest discover -s tests -v."""
import http.cookiejar
import hashlib
from io import BytesIO
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
import zipfile
import zlib

from PIL import Image


APP = Path(__file__).resolve().parents[1]


def png(rgb):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes(rgb) * 2) * 2)) + chunk(b'IEND', b''))


def assert_cover(test, response, expected_image=None):
    """Covers are persistent, small JPEGs; pages remain their original bytes."""
    status, content, headers = response
    test.assertEqual(status, 200)
    test.assertEqual(headers.get_content_type(), 'image/jpeg')
    with Image.open(BytesIO(content)) as cover:
        test.assertEqual(cover.format, 'JPEG')
        test.assertEqual(cover.mode, 'RGB')
        test.assertLessEqual(cover.width, 400)
        test.assertLessEqual(cover.height, 600)
        if expected_image:
            with Image.open(BytesIO(expected_image)) as source:
                expected = source.convert('RGB').getpixel((0, 0))
            actual = cover.getpixel((0, 0))
            test.assertTrue(all(abs(a - b) <= 5 for a, b in zip(actual, expected)), (actual, expected))
    return content


class RunningApp:
    def __init__(self, password='', legacy=False, auto_register=True):
        self.temp = tempfile.TemporaryDirectory(prefix='sumi-test-', dir=os.getenv('SUMI_TEST_TEMP'))
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.data = self.root / 'data'
        chapter = self.library / '测试系列' / '第01话'
        chapter.mkdir(parents=True)
        self.images = [png((200, 20, 20)), png((20, 200, 20)), png((20, 20, 200))]
        for name, content in zip(['1.png', '2.PNG', '10.png'], self.images):
            (chapter / name).write_bytes(content)
        (chapter / '.hidden.png').write_bytes(self.images[0])
        (self.library / '.hidden').mkdir()
        (self.library / '.hidden' / '1.png').write_bytes(self.images[0])
        with zipfile.ZipFile(self.library / '样例.cbz', 'w') as archive:
            for name, content in zip(['chapter/10.png', 'chapter/2.png', 'chapter/1.png'], reversed(self.images)):
                archive.writestr(name, content)
            archive.writestr('../escape.png', self.images[0])
            archive.writestr('__MACOSX/._cover.png', self.images[0])
        (self.library / '损坏.zip').write_bytes(b'not a zip archive')
        if legacy:
            self.seed_legacy_database()
            if legacy in ('v1.1', 'v1.2', 'v1.3', 'v1.4'):
                self.seed_v11_libraries()
            if legacy in ('v1.2', 'v1.3', 'v1.4'):
                self.seed_v12_photo_metadata()
            if legacy in ('v1.3', 'v1.4'):
                self.seed_v13_photo_metadata()
            if legacy == 'v1.4':
                self.seed_v14_photo_metadata()
        self.password = password
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.cookies = http.cookiejar.CookieJar()
        # Local tests must not inherit a workstation's external HTTP proxy.
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPCookieProcessor(self.cookies))
        self.process = None
        self.log = None
        try:
            self.start()
            if auto_register and not password:
                status, result, _ = self.request('/api/register', {
                    'username': 'test-reader', 'password': 'test-only-strong-passphrase',
                    'display_name': '测试用户'})
                if status != 201:
                    raise RuntimeError(f'Test account setup failed: {status} {result}')
        except BaseException:
            self.close()
            raise

    def seed_legacy_database(self):
        """Build the actual v1 schema, not a new-schema approximation."""
        self.data.mkdir()
        self.legacy_id = hashlib.sha256('folder\0测试系列/第01话'.encode()).hexdigest()[:24]
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as db, db:
            db.executescript('''
                CREATE TABLE books (id TEXT PRIMARY KEY,title TEXT NOT NULL,series TEXT NOT NULL,
                    path TEXT NOT NULL,format TEXT NOT NULL,page_count INTEGER NOT NULL,
                    size INTEGER NOT NULL,modified REAL NOT NULL,added_at REAL NOT NULL,
                    pages TEXT NOT NULL,signature TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE progress (book_id TEXT PRIMARY KEY REFERENCES books(id),page INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL);
                CREATE TABLE favorites (book_id TEXT PRIMARY KEY REFERENCES books(id),favorite INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
            pages = ['测试系列/第01话/' + name for name in ('1.png', '2.PNG', '10.png')]
            db.execute('INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (self.legacy_id, '第01话', '测试系列', '测试系列/第01话', 'folder', 3,
                        sum(map(len, self.images)), 1, 1, json.dumps(pages), 'legacy-signature', 1))
            db.execute('INSERT INTO progress VALUES(?,?,?,?)', (self.legacy_id, 1, 0, 123))
            db.execute('INSERT INTO favorites VALUES(?,?)', (self.legacy_id, 1))

    def start(self):
        env = {**os.environ, 'HOST': '127.0.0.1', 'PORT': str(self.port),
               'LIBRARY_PATH': str(self.library), 'DATA_PATH': str(self.data),
               'READER_PASSWORD': self.password, 'SCAN_INTERVAL': '0'}
        # Request logging can exceed a Windows pipe buffer during validation
        # loops. A file keeps the child writable while retaining startup errors.
        self.log = (self.root / 'server.stderr.log').open('w+b')
        self.process = subprocess.Popen([sys.executable, str(APP / 'server.py')], cwd=APP, env=env,
                                        stdout=subprocess.DEVNULL, stderr=self.log)
        for _ in range(150):
            if self.process.poll() is not None:
                self.log.seek(0)
                raise RuntimeError(self.log.read().decode(errors='replace'))
            try:
                if self.request('/api/health')[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError('Server did not become ready')

    def seed_v11_libraries(self):
        """Upgrade only to the released 1.1 schema, without any 1.2 columns."""
        # Released 1.1 used secrets.token_hex(12) for library IDs.
        self.legacy_library_id = hashlib.sha256(b'legacy-v1.1-library').hexdigest()[:24]
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as db, db:
            db.executescript('''
                ALTER TABLE books ADD COLUMN library_id TEXT;
                ALTER TABLE books ADD COLUMN scan_revision INTEGER NOT NULL DEFAULT 0;
                CREATE TABLE media_libraries (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                    reading_mode TEXT NOT NULL DEFAULT 'single',
                    reading_direction TEXT NOT NULL DEFAULT 'ltr',
                    exclude_rules TEXT NOT NULL DEFAULT '[]',
                    default_author TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                    last_scan REAL, errors TEXT NOT NULL DEFAULT '[]'
                );
            ''')
            db.execute('INSERT INTO media_libraries(id,name,path,reading_mode,reading_direction,default_author,created_at) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (self.legacy_library_id, '旧版媒体库', str(self.library), 'double', 'rtl', '原作者', 1))
            db.execute('UPDATE books SET library_id=?', (self.legacy_library_id,))

    def seed_v12_photo_metadata(self):
        """Use the released 1.2 schema, before automatic metadata source flags."""
        old_folder = self.library / '测试系列' / '第01话'
        renamed = old_folder.with_name('2026-10-09 旧写真')
        old_folder.rename(renamed)
        cleared = renamed.with_name('2024-02-29 手工清空')
        cleared.mkdir()
        (cleared / '1.png').write_bytes(self.images[0])
        association_only = renamed.with_name('2025-12-31 旧模特关联')
        association_only.mkdir()
        (association_only / '1.png').write_bytes(self.images[0])
        self.legacy_cleared_id = hashlib.sha256(b'legacy-cleared-photo').hexdigest()[:24]
        self.legacy_association_id = hashlib.sha256(b'legacy-association-only-photo').hexdigest()[:24]
        self.legacy_model_id = hashlib.sha256(b'legacy-photo-model').hexdigest()[:24]
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as db, db:
            db.executescript('''
                ALTER TABLE media_libraries ADD COLUMN media_type TEXT NOT NULL DEFAULT 'manga_jp';
                CREATE TABLE book_metadata (
                    book_id TEXT PRIMARY KEY REFERENCES books(id),title TEXT,
                    release_date TEXT NOT NULL DEFAULT '',cover_page INTEGER NOT NULL DEFAULT 0,
                    reading_direction_override TEXT);
                CREATE TABLE models (id TEXT PRIMARY KEY,name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE);
                CREATE TABLE book_models (book_id TEXT NOT NULL REFERENCES books(id),
                    model_id TEXT NOT NULL REFERENCES models(id),position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(book_id,model_id));
                CREATE TABLE bookmarks (id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id),
                    page INTEGER NOT NULL,note TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,
                    UNIQUE(book_id,page));
            ''')
            db.execute("UPDATE media_libraries SET media_type='photo'")
            pages = ['测试系列/2026-10-09 旧写真/' + name for name in ('1.png', '2.PNG', '10.png')]
            db.execute('UPDATE books SET title=?,path=?,pages=? WHERE id=?',
                       (renamed.name, '测试系列/' + renamed.name, json.dumps(pages), self.legacy_id))
            db.execute('INSERT INTO books(id,title,series,path,format,page_count,size,modified,added_at,'
                       'pages,signature,active,library_id,scan_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (self.legacy_cleared_id, cleared.name, '测试系列', '测试系列/' + cleared.name,
                        'folder', 1, len(self.images[0]), 1, 2,
                        json.dumps(['测试系列/' + cleared.name + '/1.png']), 'legacy-cleared', 1,
                        self.legacy_library_id, 0))
            db.execute('INSERT INTO book_metadata(book_id,title,release_date) VALUES(?,?,?)',
                       (self.legacy_id, '原有自定义标题', '2020-01-02'))
            db.execute('INSERT INTO book_metadata(book_id,release_date) VALUES(?,?)',
                       (self.legacy_cleared_id, ''))
            db.execute('INSERT INTO models VALUES(?,?,?)',
                       (self.legacy_model_id, '原有模特', '原有模特'))
            db.execute('INSERT INTO book_models VALUES(?,?,?)',
                       (self.legacy_id, self.legacy_model_id, 0))
            # 1.2 supported book/model associations independently of metadata.
            # Preserve the link without treating a never-set date as manual.
            db.execute('INSERT INTO books(id,title,series,path,format,page_count,size,modified,added_at,'
                       'pages,signature,active,library_id,scan_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (self.legacy_association_id, association_only.name, '测试系列',
                        '测试系列/' + association_only.name, 'folder', 1, len(self.images[0]), 1, 3,
                        json.dumps(['测试系列/' + association_only.name + '/1.png']),
                        'legacy-association', 1, self.legacy_library_id, 0))
            db.execute('INSERT INTO book_models VALUES(?,?,?)',
                       (self.legacy_association_id, self.legacy_model_id, 0))

    def seed_v13_photo_metadata(self):
        """Use 1.3 source flags/aliases without the new reading or title options."""
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as db, db:
            db.executescript('''
                ALTER TABLE book_metadata ADD COLUMN release_date_manual INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE book_metadata ADD COLUMN models_manual INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE book_metadata ADD COLUMN folder_model_id TEXT REFERENCES models(id);
                CREATE TABLE model_folder_names (normalized_name TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL REFERENCES models(id));
            ''')
            db.execute('UPDATE book_metadata SET release_date_manual=1,models_manual=1')
            db.execute('UPDATE book_metadata SET reading_direction_override=? WHERE book_id=?',
                       ('ltr', self.legacy_id))
            db.execute('INSERT INTO model_folder_names VALUES(?,?)',
                       ('测试系列', self.legacy_model_id))

    def seed_v14_photo_metadata(self):
        """Released 1.4 adds per-book modes/title policy, before multi-root covers."""
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as db, db:
            db.executescript('''
                ALTER TABLE media_libraries ADD COLUMN hide_date_prefix INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE book_metadata ADD COLUMN reading_mode_override TEXT;
            ''')
            db.execute('UPDATE media_libraries SET hide_date_prefix=1')
            db.execute('UPDATE book_metadata SET reading_mode_override=?,cover_page=2 WHERE book_id=?',
                       ('double', self.legacy_id))

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.process and self.process.stderr:
            self.process.stderr.close()
        if self.log:
            self.log.close()

    def close(self):
        self.stop()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None, method=None):
        body = json.dumps(data).encode() if data is not None else None
        req_headers = {'Content-Type': 'application/json'} if body is not None else {}
        req_headers.update(headers or {})
        req = urllib.request.Request(self.base + path, data=body, headers=req_headers, method=method)
        try:
            response = self.client.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            if 'application/json' in response.headers.get('Content-Type', ''):
                content = json.loads(content)
            return response.status, content, response.headers

    def create_library(self, **overrides):
        body = {'name': '测试书库', 'path': str(self.library), **overrides}
        status, data, _ = self.request('/api/libraries', body)
        if status != 201:
            raise AssertionError(f'Create library failed: {status} {data}')
        return data['library']

    def books(self, library_id=None):
        path = '/api/library' + (f'?library_id={library_id}' if library_id else '')
        for _ in range(100):
            status, data, _ = self.request(path)
            if status == 200 and not data['scanning']:
                return data
            time.sleep(0.05)
        raise RuntimeError('Scan did not finish')


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = RunningApp()
        cls.addClassCleanup(cls.app.close)
        cls.app.create_library()
        cls.catalog = cls.app.books()
        cls.folder = next(book for book in cls.catalog['books'] if book['title'] == '第01话')
        cls.archive = next(book for book in cls.catalog['books'] if book['format'].lower() in ('cbz', 'zip'))

    def test_folder_scan_hidden_and_broken_archive(self):
        self.assertEqual(len(self.catalog['books']), 2)
        self.assertEqual(self.folder['series'], '测试系列')
        self.assertEqual(self.folder['page_count'], 3)
        self.assertTrue(self.catalog['errors'])

    def test_natural_page_order_and_cover(self):
        for index, expected in enumerate(self.app.images):
            status, content, headers = self.app.request(f"/api/books/{self.folder['id']}/pages/{index}")
            self.assertEqual(status, 200)
            self.assertEqual(content, expected)
            self.assertEqual(headers.get_content_type(), 'image/png')
        assert_cover(self, self.app.request(f"/api/books/{self.folder['id']}/cover"), self.app.images[0])

    def test_archive_order_and_no_extraction(self):
        self.assertEqual(self.archive['page_count'], 3)
        for index, expected in enumerate(self.app.images):
            self.assertEqual(self.app.request(f"/api/books/{self.archive['id']}/pages/{index}")[1], expected)
        self.assertFalse((self.app.root / 'escape.png').exists())

    def test_invalid_media_and_path_escape(self):
        for path in [f"/api/books/{self.folder['id']}/pages/-1", f"/api/books/{self.folder['id']}/pages/999",
                     '/api/books/unknown/cover', '/%2e%2e/server.py', '/server.py', '/static/../server.py']:
            self.assertIn(self.app.request(path)[0], (400, 404))

    def test_post_requires_json_and_same_origin(self):
        route = f"/api/books/{self.folder['id']}/favorite"
        self.assertIn(self.app.request(route, {'favorite': True}, {'Origin': 'https://example.invalid'})[0], (400, 403))
        self.assertIn(self.app.request(route, {'favorite': True}, {'Content-Type': 'text/plain'})[0], (400, 415))

    def test_progress_favorite_scan_and_restart(self):
        route = f"/api/books/{self.folder['id']}"
        self.assertEqual(self.app.request(route + '/progress', {'page': 1, 'completed': False})[0], 200)
        self.assertEqual(self.app.request(route + '/favorite', {'favorite': True})[0], 200)
        self.assertIn(self.app.request('/api/scan', {})[0], (200, 202))
        book = next(book for book in self.app.books()['books'] if book['id'] == self.folder['id'])
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])
        self.app.stop()
        self.app.start()
        book = next(book for book in self.app.books()['books'] if book['id'] == self.folder['id'])
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])

    def test_static_content(self):
        status, content, headers = self.app.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'<html', content.lower())
        self.assertEqual(headers.get_content_type(), 'text/html')
        for path, mime in [('/app.js', 'text/javascript'), ('/reader-model.js', 'text/javascript'),
                           ('/performance-settings.js', 'text/javascript'), ('/reader-end.js', 'text/javascript'),
                           ('/styles.css', 'text/css'), ('/reader-end.css', 'text/css'), ('/gravelle-logo.png', 'image/png'),
                           ('/gravelle-wordmark.svg', 'image/svg+xml'), ('/gravelle-icon.png', 'image/png')]:
            status, content, headers = self.app.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(content)
            self.assertEqual(headers.get_content_type(), mime)


class PasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = RunningApp('test-only-passphrase')
        cls.addClassCleanup(cls.app.close)

    def test_login_protection_and_logout(self):
        self.assertFalse(self.app.request('/api/session')[1]['authenticated'])
        self.assertEqual(self.app.request('/api/library')[0], 401)
        self.assertIn(self.app.request('/api/login', {'password': 'wrong'})[0], (400, 401, 403))
        status, _, headers = self.app.request('/api/login', {'password': 'test-only-passphrase'})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertTrue(self.app.request('/api/session')[1]['authenticated'])
        self.app.create_library()
        catalog = self.app.books()
        book_id = catalog['books'][0]['id']
        self.assertEqual(self.app.request(f'/api/books/{book_id}/cover')[0], 200)
        self.assertEqual(self.app.request('/api/logout', {})[0], 200)
        self.assertEqual(self.app.request('/api/library')[0], 401)
        self.assertEqual(self.app.request(f'/api/books/{book_id}/cover')[0], 401)


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)

    def test_fresh_install_waits_for_explicit_library_creation(self):
        status, data, _ = self.app.request('/api/libraries')
        self.assertEqual(status, 200)
        self.assertEqual(data['libraries'], [])
        self.assertEqual(Path(data['root_path']), self.app.library.resolve())
        self.assertEqual(self.app.books()['books'], [])
        self.assertFalse(self.app.books()['scanning'])

    def test_create_defaults_and_book_metadata(self):
        library = self.app.create_library()
        self.assertEqual(library['media_type'], 'manga_jp')
        self.assertEqual(library['reading_mode'], 'single')
        self.assertEqual(library['reading_direction'], 'ltr')
        self.assertEqual(library['exclude_rules'], [])
        self.assertEqual(library['default_author'], '')
        books = self.app.books(library['id'])['books']
        self.assertEqual(len(books), 2)
        for book in books:
            self.assertEqual(book['media_type'], 'manga_jp')
            self.assertEqual(book['library_id'], library['id'])
            self.assertEqual(book['library_name'], '测试书库')
            self.assertEqual(book['reading_mode'], 'single')
            self.assertEqual(book['reading_direction'], 'ltr')
            self.assertEqual(book['author'], '')
            detail = self.app.request('/api/books/' + book['id'])[1]
            self.assertEqual(detail['library_id'], library['id'])
        summary = self.app.request('/api/libraries')[1]['libraries'][0]
        self.assertEqual(summary['book_count'], 2)

    def test_edit_persists_and_inherits_defaults_without_resetting_progress(self):
        library = self.app.create_library(name='单行本', reading_mode='double',
                                          reading_direction='rtl', default_author='原作者')
        book = self.app.books(library['id'])['books'][0]
        self.app.request('/api/books/' + book['id'] + '/progress', {'page': 1, 'completed': False})
        body = {'name': '条漫收藏', 'path': str(self.app.library), 'reading_mode': 'webtoon',
                'reading_direction': 'ltr', 'exclude_rules': ['# 规则注释', '*.gif'], 'default_author': '新作者'}
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], body)[0], 200)
        updated = next(item for item in self.app.books(library['id'])['books'] if item['id'] == book['id'])
        self.assertEqual((updated['library_name'], updated['reading_mode'], updated['reading_direction'], updated['author']),
                         ('条漫收藏', 'webtoon', 'ttb', '新作者'))
        self.assertEqual(updated['progress']['page'], 1)
        self.app.stop()
        self.app.start()
        libraries = self.app.request('/api/libraries')[1]['libraries']
        self.assertEqual(len(libraries), 1)
        self.assertEqual(libraries[0]['id'], library['id'])
        self.assertEqual(libraries[0]['name'], '条漫收藏')
        self.assertEqual(libraries[0]['reading_mode'], 'webtoon')
        self.assertEqual(libraries[0]['reading_direction'], 'ltr')
        self.assertEqual(libraries[0]['default_author'], '新作者')
        self.assertEqual(libraries[0]['exclude_rules'], ['*.gif'])
        self.assertEqual(next(item for item in self.app.books()['books'] if item['id'] == book['id'])['progress']['page'], 1)

    def test_same_path_libraries_keep_ids_progress_and_scans_isolated(self):
        first = self.app.create_library(name='第一库', default_author='甲')
        a = next(item for item in self.app.books(first['id'])['books'] if item['title'] == '第01话')
        second = self.app.create_library(name='第二库', default_author='乙')
        b = next(item for item in self.app.books(second['id'])['books'] if item['title'] == '第01话')
        self.assertNotEqual(a['id'], b['id'])
        self.app.request('/api/books/' + a['id'] + '/progress', {'page': 2, 'completed': True})
        self.app.request('/api/books/' + a['id'] + '/favorite', {'favorite': True})
        b = self.app.request('/api/books/' + b['id'])[1]
        self.assertIsNone(b['progress'])
        self.assertFalse(b['favorite'])
        self.assertEqual(b['author'], '乙')
        self.assertEqual(self.app.request('/api/libraries/' + first['id'] + '/scan', {})[0], 202)
        self.assertEqual(len(self.app.books(first['id'])['books']), 2)
        self.assertEqual(len(self.app.books(second['id'])['books']), 2)
        self.assertEqual(len(self.app.books()['books']), 4)
        a = self.app.request('/api/books/' + a['id'])[1]
        self.assertTrue(a['favorite'])
        self.assertTrue(a['progress']['completed'])

    def test_exclusions_prune_directories_images_and_cached_archive_entries(self):
        extra = self.app.library / 'Extra' / 'nested'
        extra.mkdir(parents=True)
        (extra / '1.png').write_bytes(self.app.images[0])
        chapter = self.app.library / '测试系列' / '第01话'
        (chapter / '3-SKIP.png').write_bytes(self.app.images[0])
        with zipfile.ZipFile(self.app.library / '规则.cbz', 'w') as archive:
            archive.writestr('1.png', self.app.images[0])
            archive.writestr('2-SKIP.png', self.app.images[1])
            archive.writestr('extras/3.png', self.app.images[2])
        library = self.app.create_library()
        self.assertEqual(next(book for book in self.app.books(library['id'])['books'] if book['title'] == '规则')['page_count'], 3)
        body = {'name': '过滤库', 'path': str(self.app.library),
                'exclude_rules': ['  ', '# 注释', 'extra/', '*skip*', 'extras/']}
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], body)[0], 200)
        books = self.app.books(library['id'])['books']
        self.assertNotIn('nested', [book['title'] for book in books])
        self.assertEqual(next(book for book in books if book['title'] == '第01话')['page_count'], 3)
        archive_book = next(book for book in books if book['title'] == '规则')
        self.assertEqual(archive_book['page_count'], 1)
        self.assertEqual(self.app.request('/api/books/' + archive_book['id'] + '/pages/0')[1], self.app.images[0])
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'exclude_rules': []})[0], 200)
        restored = self.app.books(library['id'])['books']
        self.assertIn('nested', [book['title'] for book in restored])
        self.assertEqual(next(book for book in restored if book['id'] == archive_book['id'])['page_count'], 3)

    def test_directory_browser_stays_within_allowed_mount(self):
        status, data, _ = self.app.request('/api/directories')
        self.assertEqual(status, 200)
        self.assertEqual(Path(data['path']), self.app.library.resolve())
        self.assertIsNone(data['parent'])
        self.assertNotIn('.hidden', [item['name'] for item in data['directories']])
        child = next(item for item in data['directories'] if item['name'] == '测试系列')
        status, child_data, _ = self.app.request('/api/directories?path=' + quote(child['path']))
        self.assertEqual(status, 200)
        self.assertEqual(Path(child_data['parent']), self.app.library.resolve())
        for path in (str(self.app.root), '.', str(self.app.library / '..' / 'library')):
            with self.subTest(path=path):
                self.assertIn(self.app.request('/api/directories?path=' + quote(path))[0], (400, 403, 422))

    def test_library_validation_rejects_bad_fields_and_paths(self):
        cases = [
            {'name': ''}, {'name': '   '}, {'name': 42}, {'path': '.'},
            {'path': str(self.app.root)}, {'path': str(self.app.library / 'missing')},
            {'path': str(self.app.library / '..' / 'library')}, {'path': str(self.app.data)},
            {'reading_mode': 'paged'}, {'reading_direction': 'up'},
            {'exclude_rules': [42]}, {'default_author': 42},
        ]
        for change in cases:
            with self.subTest(change=change):
                status, _, _ = self.app.request('/api/libraries', {'name': '验证库', 'path': str(self.app.library), **change})
                self.assertIn(status, (400, 403, 422))
        self.assertEqual(self.app.request('/api/libraries')[1]['libraries'], [])

    def test_new_apis_require_authentication_and_same_origin(self):
        self.assertEqual(self.app.request('/api/logout', {})[0], 200)
        self.assertEqual(self.app.request('/api/libraries')[0], 401)
        self.assertEqual(self.app.request('/api/directories')[0], 401)
        self.assertEqual(self.app.request('/api/libraries', {'name': '书库', 'path': str(self.app.library)})[0], 401)
        self.assertEqual(self.app.request('/api/login', {'username': 'test-reader', 'password': 'test-only-strong-passphrase'})[0], 200)
        body = {'name': '书库', 'path': str(self.app.library)}
        self.assertEqual(self.app.request('/api/libraries', body, {'Origin': 'https://example.invalid'})[0], 403)
        library = self.app.create_library()
        for path in ('/api/libraries/' + library['id'], '/api/libraries/' + library['id'] + '/scan'):
            self.assertEqual(self.app.request(path, {}, {'Origin': 'https://example.invalid'})[0], 403)
        self.app.request('/api/logout', {})
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], body)[0], 401)
        self.assertEqual(self.app.request('/api/libraries/' + library['id'] + '/scan', {})[0], 401)


class LegacyMigrationTests(unittest.TestCase):
    def test_v1_migration_preserves_ids_progress_favorites_and_rescan(self):
        app = RunningApp(legacy=True)
        self.addCleanup(app.close)
        libraries = app.request('/api/libraries')[1]['libraries']
        self.assertEqual(len(libraries), 1)
        self.assertEqual(libraries[0]['name'], '我的漫画')
        self.assertEqual(libraries[0]['media_type'], 'manga_jp')
        self.assertEqual(Path(libraries[0]['path']), app.library.resolve())
        book = next(item for item in app.books()['books'] if item['title'] == '第01话')
        self.assertEqual(book['id'], app.legacy_id)
        self.assertEqual(book['library_id'], libraries[0]['id'])
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])
        self.assertEqual(app.request('/api/books/' + book['id'] + '/pages/1')[1], app.images[1])
        app.request('/api/libraries/' + libraries[0]['id'] + '/scan', {})
        app.books()
        app.stop()
        app.start()
        self.assertEqual(len(app.request('/api/libraries')[1]['libraries']), 1)
        book = app.request('/api/books/' + app.legacy_id)[1]
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])

    def test_v11_migration_preserves_library_defaults_and_user_state(self):
        app = RunningApp(legacy='v1.1')
        self.addCleanup(app.close)
        libraries = app.request('/api/libraries')[1]['libraries']
        self.assertEqual(len(libraries), 1)
        library = libraries[0]
        self.assertEqual((library['id'], library['name'], library['media_type']),
                         (app.legacy_library_id, '旧版媒体库', 'manga_jp'))
        self.assertEqual((library['reading_mode'], library['reading_direction'], library['default_author']),
                         ('double', 'rtl', '原作者'))
        book = app.request('/api/books/' + app.legacy_id)[1]
        self.assertEqual(book['id'], app.legacy_id)
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])
        self.assertEqual(book['models'], [])
        self.assertEqual(book['cover_page'], 0)
        self.assertEqual(book['bookmarks'], [])
        self.assertEqual(app.request('/api/libraries/' + library['id'], {'media_type': 'photo'})[0], 200)
        app.books(library['id'])
        app.stop()
        app.start()
        book = app.request('/api/books/' + app.legacy_id)[1]
        self.assertEqual(book['media_type'], 'photo')
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])

    def test_v12_migration_preserves_manual_values_and_intentional_clears(self):
        app = RunningApp(legacy='v1.2')
        self.addCleanup(app.close)
        app.books(app.legacy_library_id)
        old = app.request('/api/books/' + app.legacy_id)[1]
        cleared = app.request('/api/books/' + app.legacy_cleared_id)[1]
        self.assertEqual(old['title'], '原有自定义标题')
        self.assertEqual(old['release_date'], '2020-01-02')
        self.assertEqual([model['id'] for model in old['models']], [app.legacy_model_id])
        self.assertEqual(old['progress']['page'], 1)
        self.assertTrue(old['favorite'])
        self.assertEqual(old['added_at'], 1)
        self.assertEqual(cleared['release_date'], '')
        self.assertEqual(cleared['models'], [])
        associated = app.request('/api/books/' + app.legacy_association_id)[1]
        self.assertEqual(associated['release_date'], '2025-12-31')
        self.assertEqual([model['id'] for model in associated['models']], [app.legacy_model_id])
        for _ in range(2):
            self.assertEqual(app.request('/api/libraries/' + app.legacy_library_id + '/scan', {})[0], 202)
            app.books(app.legacy_library_id)
            app.stop()
            app.start()
            self.assertEqual(app.request('/api/books/' + app.legacy_id)[1]['release_date'], '2020-01-02')
            cleared = app.request('/api/books/' + app.legacy_cleared_id)[1]
            self.assertEqual((cleared['release_date'], cleared['models']), ('', []))
            associated = app.request('/api/books/' + app.legacy_association_id)[1]
            self.assertEqual(associated['release_date'], '2025-12-31')
            self.assertEqual([model['id'] for model in associated['models']], [app.legacy_model_id])

    def test_v13_migration_inherits_mode_and_keeps_existing_metadata_and_state(self):
        app = RunningApp(legacy='v1.3')
        self.addCleanup(app.close)
        library = app.request('/api/libraries')[1]['libraries'][0]
        self.assertFalse(library['hide_date_prefix'])
        book = next(item for item in app.books(library['id'])['books'] if item['id'] == app.legacy_id)
        self.assertEqual(book['reading_mode'], 'double')
        self.assertIsNone(book['reading_mode_override'])
        self.assertEqual((book['reading_direction'], book['reading_direction_override']), ('ltr', 'ltr'))
        self.assertEqual(book['title'], '原有自定义标题')
        self.assertEqual(book['title_override'], '原有自定义标题')
        self.assertEqual(book['source_title'], '2026-10-09 旧写真')
        self.assertEqual(book['release_date'], '2020-01-02')
        self.assertEqual([model['id'] for model in book['models']], [app.legacy_model_id])
        self.assertEqual((book['added_at'], book['progress']['page'], book['favorite']), (1, 1, True))
        self.assertEqual(app.request('/api/libraries/' + library['id'], {'hide_date_prefix': True})[0], 200)
        app.books(library['id'])
        app.stop()
        app.start()
        persisted = app.request('/api/books/' + book['id'])[1]
        self.assertEqual(persisted['title'], '原有自定义标题')
        self.assertIsNone(persisted['reading_mode_override'])
        self.assertEqual(persisted['reading_direction_override'], 'ltr')
        self.assertTrue(app.request('/api/libraries')[1]['libraries'][0]['hide_date_prefix'])


class MediaTypeTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)

    def test_four_library_types_and_edit_are_exported_for_books(self):
        for kind in ('manga_jp', 'manga_kr', 'comic_us', 'photo'):
            with self.subTest(kind=kind):
                library = self.app.create_library(name=kind, media_type=kind)
                self.assertEqual(library['media_type'], kind)
                books = self.app.books(library['id'])['books']
                self.assertEqual(len(books), 2)
                self.assertTrue(all(book['media_type'] == kind for book in books))
        self.assertEqual(len(self.app.request('/api/libraries')[1]['libraries']), 4)
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'media_type': 'manga_kr'})[0], 200)
        self.assertTrue(all(book['media_type'] == 'manga_kr'
                            for book in self.app.books(library['id'])['books']))

    def test_invalid_media_types_do_not_create_or_mutate_libraries(self):
        for kind in ('manga', '写真', '', None, 42, [], {}):
            with self.subTest(kind=kind):
                self.assertIn(self.app.request('/api/libraries', {
                    'name': '无效类型', 'path': str(self.app.library), 'media_type': kind})[0], (400, 422))
        self.assertEqual(self.app.request('/api/libraries')[1]['libraries'], [])
        library = self.app.create_library(media_type='photo')
        self.app.books(library['id'])
        self.assertIn(self.app.request('/api/libraries/' + library['id'], {'media_type': 'unknown'})[0], (400, 422))
        self.assertEqual(self.app.request('/api/libraries')[1]['libraries'][0]['media_type'], 'photo')


class PhotoLibraryTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)
        self.library = self.app.create_library(name='写真收藏', media_type='photo', reading_direction='ltr')
        books = self.app.books(self.library['id'])['books']
        self.book = next(book for book in books if book['format'] == 'folder')
        self.archive = next(book for book in books if book['format'] != 'folder')
        self.route = '/api/books/' + self.book['id']

    def create_model(self, name):
        status, data, _ = self.app.request('/api/models', {'name': name})
        self.assertEqual(status, 201)
        return data['model']

    def test_metadata_selected_cover_and_models_survive_rescan_and_restart(self):
        first, second = self.create_model('模特甲'), self.create_model('模特乙')
        body = {'title': '夏日映像', 'release_date': '2024-02-29', 'cover_page': 2,
                'reading_direction': 'rtl', 'model_ids': [first['id'], second['id']]}
        status, data, _ = self.app.request(self.route + '/metadata', body)
        self.assertEqual(status, 200)
        self.assertEqual(data['book']['title'], '夏日映像')
        self.assertEqual(data['book']['release_date'], '2024-02-29')
        self.assertEqual(data['book']['reading_direction_override'], 'rtl')
        self.assertEqual(data['book']['reading_direction'], 'rtl')
        self.assertEqual({model['id'] for model in data['book']['models']}, {first['id'], second['id']})
        self.assertEqual(data['book']['cover_page'], 2)
        assert_cover(self, self.app.request(self.route + '/cover'), self.app.images[2])
        self.assertEqual(self.app.request(self.route + '/pages/0')[1], self.app.images[0])
        self.app.request(self.route + '/progress', {'page': 1, 'completed': False})
        self.app.request(self.route + '/favorite', {'favorite': True})
        self.assertEqual(self.app.request('/api/libraries/' + self.library['id'] + '/scan', {})[0], 202)
        rescanned = next(book for book in self.app.books(self.library['id'])['books'] if book['id'] == self.book['id'])
        self.assertEqual(rescanned['title'], '夏日映像')
        self.assertEqual(rescanned['release_date'], '2024-02-29')
        self.assertEqual(rescanned['cover_page'], 2)
        self.app.stop()
        self.app.start()
        persisted = self.app.request(self.route)[1]
        for field in ('title', 'release_date', 'cover_page', 'reading_direction', 'reading_direction_override'):
            self.assertEqual(persisted[field], data['book'][field])
        self.assertEqual({model['id'] for model in persisted['models']}, {first['id'], second['id']})
        self.assertEqual(persisted['progress']['page'], 1)
        self.assertTrue(persisted['favorite'])
        assert_cover(self, self.app.request(self.route + '/cover'), self.app.images[2])

    def test_metadata_clear_restores_scan_title_and_library_direction(self):
        model = self.create_model('模特')
        self.assertEqual(self.app.request(self.route + '/metadata', {
            'title': '自定义标题', 'release_date': '2025-10-09', 'cover_page': 1,
            'reading_direction': 'rtl', 'model_ids': [model['id']]})[0], 200)
        self.assertEqual(self.app.request('/api/libraries/' + self.library['id'], {'reading_direction': 'rtl'})[0], 200)
        self.app.books(self.library['id'])
        status, data, _ = self.app.request(self.route + '/metadata', {
            'title': None, 'release_date': '', 'cover_page': 0, 'reading_direction': None, 'model_ids': []})
        self.assertEqual(status, 200)
        book = data['book']
        self.assertEqual(book['title'], self.book['title'])
        self.assertIn(book['release_date'], (None, ''))
        self.assertIsNone(book['reading_direction_override'])
        self.assertEqual(book['reading_direction'], 'rtl')
        self.assertEqual(book['models'], [])
        self.assertEqual(book['cover_page'], 0)
        assert_cover(self, self.app.request(self.route + '/cover'), self.app.images[0])

    def test_metadata_validation_is_atomic(self):
        cases = [
            {'title': 42}, {'title': 'x' * 201},
            {'release_date': '2025-02-29'}, {'release_date': '2025-2-09'}, {'release_date': 42},
            {'cover_page': -1}, {'cover_page': 3}, {'cover_page': True}, {'cover_page': '1'},
            {'reading_direction': 'up'}, {'reading_direction': []},
            {'model_ids': 'unknown'}, {'model_ids': ['unknown']}, {'model_ids': [42]},
        ]
        for change in cases:
            with self.subTest(change=change):
                status, _, _ = self.app.request(self.route + '/metadata', {'title': '不应保存', **change})
                self.assertIn(status, (400, 404, 422))
                self.assertEqual(self.app.request(self.route)[1]['title'], self.book['title'])
        self.assertEqual(self.app.request('/api/books/unknown/metadata', {'title': '标题'})[0], 404)

    def test_models_many_to_many_rename_and_counts_across_libraries(self):
        first, second = self.create_model('模特甲'), self.create_model('模特乙')
        self.assertEqual(self.app.request(self.route + '/metadata', {'model_ids': [first['id'], second['id'], first['id']]})[0], 200)
        self.assertEqual(len(self.app.request(self.route)[1]['models']), 2)
        self.assertEqual(self.app.request('/api/books/' + self.archive['id'] + '/metadata', {'model_ids': [first['id']]})[0], 200)
        other_library = self.app.create_library(name='另一个模特库', media_type='photo')
        other_book = next(book for book in self.app.books(other_library['id'])['books'] if book['format'] == 'folder')
        self.assertEqual(self.app.request('/api/books/' + other_book['id'] + '/metadata', {'model_ids': [first['id']]})[0], 200)
        models = {model['id']: model for model in self.app.request('/api/models')[1]['models']}
        self.assertEqual(models[first['id']]['book_count'], 3)
        self.assertEqual(models[second['id']]['book_count'], 1)
        self.assertIn(models[first['id']]['cover_book_id'], {self.book['id'], self.archive['id'], other_book['id']})
        status, data, _ = self.app.request('/api/models/' + first['id'], {'name': '模特甲新名'})
        self.assertEqual(status, 200)
        self.assertEqual(data['model']['name'], '模特甲新名')
        self.assertIn('模特甲新名', [model['name'] for model in self.app.request(self.route)[1]['models']])
        status, data, _ = self.app.request('/api/models', {'name': '  模特甲新名  '})
        self.assertEqual(status, 200)
        self.assertEqual(data['model']['id'], first['id'])
        self.assertEqual(self.app.request('/api/models/' + first['id'], {'name': second['name']})[0], 409)
        self.app.request('/api/libraries/' + self.library['id'], {'exclude_rules': ['*']})
        self.app.books(self.library['id'])
        models = {model['id']: model for model in self.app.request('/api/models')[1]['models']}
        self.assertEqual(models[first['id']]['book_count'], 1)
        self.assertEqual(models[second['id']]['book_count'], 0)

    def test_model_name_validation_and_unicode_duplicate_detection(self):
        existing = self.app.request('/api/models')[1]['models']
        for name in ('', '  ', None, 42, [], 'x' * 101):
            with self.subTest(name=name):
                self.assertIn(self.app.request('/api/models', {'name': name})[0], (400, 422))
        self.assertEqual(self.app.request('/api/models')[1]['models'], existing)
        model = self.create_model('Ａlice')
        status, data, _ = self.app.request('/api/models', {'name': 'alice'})
        self.assertEqual(status, 200)
        self.assertEqual(data['model']['id'], model['id'])
        self.assertEqual(self.app.request('/api/models/unknown', {'name': '不存在'})[0], 404)

    def test_bookmark_upsert_detail_delete_and_restart(self):
        self.assertEqual(self.app.request('/api/bookmarks')[1]['bookmarks'], [])
        status, data, _ = self.app.request(self.route + '/bookmarks', {'page': 1, 'note': '喜欢这一页'})
        self.assertEqual(status, 201)
        first = data['bookmark']
        self.assertEqual(first['book_id'], self.book['id'])
        self.assertEqual(first['page'], 1)
        status, data, _ = self.app.request(self.route + '/bookmarks', {'page': 1, 'note': '更新备注'})
        self.assertEqual(status, 200)
        self.assertEqual(data['bookmark']['id'], first['id'])
        self.assertEqual(data['bookmark']['created_at'], first['created_at'])
        self.assertEqual(data['bookmark']['note'], '更新备注')
        self.assertEqual(self.app.request(self.route + '/bookmarks', {'page': 2, 'note': ''})[0], 201)
        bookmarks = self.app.request('/api/bookmarks')[1]['bookmarks']
        self.assertEqual(len(bookmarks), 2)
        self.assertTrue(all(item['book']['id'] == self.book['id'] for item in bookmarks))
        self.assertEqual(len(self.app.request(self.route)[1]['bookmarks']), 2)
        self.assertEqual(self.app.request('/api/bookmarks/' + first['id'] + '/delete', {})[0], 200)
        self.app.stop()
        self.app.start()
        remaining = self.app.request('/api/bookmarks')[1]['bookmarks']
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['page'], 2)
        self.assertEqual(self.app.request(self.route)[1]['bookmarks'][0]['id'], remaining[0]['id'])

    def test_bookmark_page_and_note_validation(self):
        for body in ({'page': -1}, {'page': 3}, {'page': True}, {'page': '1'},
                     {'page': 1, 'note': 42}, {'page': 1, 'note': 'x' * 1001}, {}):
            with self.subTest(body=body):
                self.assertIn(self.app.request(self.route + '/bookmarks', body)[0], (400, 422))
        self.assertEqual(self.app.request('/api/bookmarks')[1]['bookmarks'], [])
        self.assertEqual(self.app.request('/api/books/unknown/bookmarks', {'page': 0})[0], 404)
        self.assertEqual(self.app.request('/api/bookmarks/unknown/delete', {})[0], 404)

    def test_models_metadata_bookmarks_require_authentication_and_same_origin(self):
        model = self.create_model('受保护模特')
        bookmark = self.app.request(self.route + '/bookmarks', {'page': 0, 'note': '备注'})[1]['bookmark']
        self.assertEqual(self.app.request('/api/logout', {})[0], 200)
        routes = [('/api/models', None), ('/api/bookmarks', None),
                  (self.route + '/metadata', {'title': '不能写入'}),
                  ('/api/models', {'name': '不能创建'}), ('/api/models/' + model['id'], {'name': '不能改名'}),
                  (self.route + '/bookmarks', {'page': 1}), ('/api/bookmarks/' + bookmark['id'] + '/delete', {})]
        for path, body in routes:
            with self.subTest(path=path):
                self.assertEqual(self.app.request(path, body)[0], 401)
        self.assertEqual(self.app.request('/api/login', {'username': 'test-reader', 'password': 'test-only-strong-passphrase'})[0], 200)
        for path, body in routes:
            if body is not None:
                with self.subTest(path=path):
                    self.assertEqual(self.app.request(path, body, {'Origin': 'https://example.invalid'})[0], 403)
        self.assertEqual(self.app.request(self.route)[1]['title'], self.book['title'])
        self.assertEqual(len(self.app.request('/api/bookmarks')[1]['bookmarks']), 1)


class PhotoDirectoryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)
        self.collection = self.app.library / '写真合集'
        self.collection.mkdir()

    def album(self, relative, archive=False):
        path = self.collection / relative
        if archive:
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path, 'w') as contents:
                contents.writestr('1.png', self.app.images[0])
                contents.writestr('2.png', self.app.images[1])
        else:
            path.mkdir(parents=True, exist_ok=True)
            for index, image in enumerate(self.app.images[:2], 1):
                (path / f'{index}.png').write_bytes(image)
        return path

    def create_library(self, path=None, **overrides):
        return self.app.create_library(path=str(path or self.collection), media_type='photo',
                                       **overrides)

    def rescan(self, library):
        self.assertEqual(self.app.request('/api/libraries/' + library['id'] + '/scan', {})[0], 202)
        return self.app.books(library['id'])['books']

    def detail(self, book):
        return self.app.request('/api/books/' + book['id'])[1]

    def test_collection_and_single_model_roots_reuse_models_and_parse_archive_dates(self):
        model_root = self.collection / '【模特甲'
        self.album('【模特甲/2026-10-09 模特甲 作品')
        self.album('【模特甲/2024-02-29 合刊.cbz', archive=True)
        self.album('林奈/2025-12-31 夜景')
        collection = self.create_library()
        books = self.app.books(collection['id'])['books']
        self.assertEqual(len(books), 3)
        kato = [book for book in books if '模特甲' in book['title']][0]
        kato_model = kato['models'][0]
        self.assertEqual(kato_model['name'], '模特甲')
        self.assertEqual(kato['author'], '模特甲')
        self.assertEqual(kato['release_date'], '2026-10-09')
        archive = next(book for book in books if book['format'] != 'folder')
        self.assertEqual(archive['release_date'], '2024-02-29')
        self.assertEqual(archive['models'], [kato_model])
        individual = self.create_library(path=model_root, name='自取的库名')
        own_books = self.app.books(individual['id'])['books']
        self.assertEqual(len(own_books), 2)
        # Adding another library may change the cover used as the default avatar.
        for book in own_books:
            self.assertEqual([(model['id'], model['name']) for model in book['models']],
                             [(kato_model['id'], kato_model['name'])])
        self.assertTrue(all(book['author'] == '模特甲' for book in own_books))
        models = self.app.request('/api/models')[1]['models']
        self.assertEqual({model['name'] for model in models}, {'模特甲', '林奈'})
        self.assertEqual(next(model for model in models if model['id'] == kato_model['id'])['book_count'], 4)

    def test_date_prefix_requires_real_iso_date_and_non_numeric_boundary(self):
        cases = {'2024-02-29闰日': '2024-02-29', '2026-10-09': '2026-10-09',
                 '2026-10-09_作品': '2026-10-09', '2025-02-29 非闰年': '',
                 '2026-13-01 月份错误': '', '0000-01-01 年份错误': '',
                 '2026-2-09 不补零': '', '2026-10-091 连续数字': '',
                 '作品 2026-10-09': '', '2026-10-09９ 全角连续数字': ''}
        for name in cases:
            self.album('模特甲/' + name)
        library = self.create_library()
        actual = {book['title']: book['release_date'] for book in self.app.books(library['id'])['books']}
        self.assertEqual(actual, cases)

    def test_root_images_and_deep_paths_do_not_guess_model_and_bad_names_are_skipped(self):
        self.album('')
        self.album('模特甲/合集/2026-10-09 深层作品')
        self.album('【】/2026-10-09 空模特')
        self.album('名字\u0085控制/2026-10-09 控制字符')
        self.album('x' * 101 + '/2026-10-09 超长模特')
        self.album('【[（「『模特乙』」）]】/2026-10-09 装饰名称')
        library = self.create_library()
        books = self.app.books(library['id'])['books']
        for book in books:
            if book['title'] == '2026-10-09 装饰名称':
                self.assertEqual([model['name'] for model in book['models']], ['模特乙'])
                self.assertEqual(book['author'], '模特乙')
            else:
                self.assertEqual(book['models'], [])
                self.assertEqual(book['author'], '')
        self.assertEqual({model['name'] for model in self.app.request('/api/models')[1]['models']}, {'模特乙'})

    def test_non_photo_libraries_do_not_infer_dates_or_models(self):
        self.album('模特甲/2026-10-09 作品')
        for media_type in ('manga_jp', 'manga_kr', 'comic_us'):
            with self.subTest(media_type=media_type):
                library = self.app.create_library(path=str(self.collection), media_type=media_type,
                                                  default_author='漫画作者')
                book = self.app.books(library['id'])['books'][0]
                self.assertEqual(book['models'], [])
                self.assertEqual(book['release_date'], '')
                self.assertEqual(book['author'], '漫画作者')
        models = self.app.request('/api/models')[1]['models']
        self.assertEqual([(model['name'], model['book_count']) for model in models], [('漫画作者', 0)])

    def test_manual_values_and_clears_survive_scans_restart_and_preserve_independent_auto_fields(self):
        for name in ('手动资料', '手动清空', '只清日期', '只清模特'):
            self.album('模特甲/2026-10-09 ' + name)
        library = self.create_library()
        books = {book['title'].split(' ', 1)[1]: book for book in self.app.books(library['id'])['books']}
        model = self.app.request('/api/models', {'name': '手工模特'})[1]['model']
        changes = {'手动资料': {'release_date': '2020-01-02', 'model_ids': [model['id']]},
                   '手动清空': {'release_date': '', 'model_ids': []},
                   '只清日期': {'release_date': ''}, '只清模特': {'model_ids': []}}
        for name, body in changes.items():
            self.assertEqual(self.app.request('/api/books/' + books[name]['id'] + '/metadata', body)[0], 200)
        self.rescan(library)
        self.app.stop()
        self.app.start()
        self.rescan(library)
        manual, cleared, date_only, models_only = (self.detail(books[name]) for name in changes)
        self.assertEqual(manual['release_date'], '2020-01-02')
        self.assertEqual([item['id'] for item in manual['models']], [model['id']])
        self.assertEqual(manual['author'], '模特甲')
        self.assertEqual((cleared['release_date'], cleared['models']), ('', []))
        self.assertEqual(date_only['release_date'], '')
        self.assertEqual([item['name'] for item in date_only['models']], ['模特甲'])
        self.assertEqual(models_only['release_date'], '2026-10-09')
        self.assertEqual(models_only['models'], [])

    def test_title_and_cover_only_edit_does_not_freeze_automatic_date_or_models(self):
        self.album('模特甲/2026-10-09 作品')
        library = self.app.create_library(path=str(self.collection), media_type='manga_jp')
        book = self.app.books(library['id'])['books'][0]
        self.assertEqual(self.app.request('/api/books/' + book['id'] + '/metadata',
                                          {'title': '2020-01-01 手动标题', 'cover_page': 1})[0], 200)
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'media_type': 'photo'})[0], 200)
        updated = self.app.books(library['id'])['books'][0]
        self.assertEqual(updated['title'], '2020-01-01 手动标题')
        self.assertEqual(updated['cover_page'], 1)
        self.assertEqual(updated['release_date'], '2026-10-09')
        self.assertEqual([model['name'] for model in updated['models']], ['模特甲'])

    def test_model_rename_and_new_albums_preserve_folder_alias_across_restart(self):
        self.album('【模特甲】/2026-10-09 作品')
        library = self.create_library()
        book = self.app.books(library['id'])['books'][0]
        original_model = book['models'][0]
        self.assertEqual(self.app.request('/api/models/' + original_model['id'], {'name': '新姓名'})[0], 200)
        self.app.stop()
        self.app.start()
        self.album('【模特甲】/2026-10-10 新作.cbz', archive=True)
        books = self.rescan(library)
        self.assertEqual(len(books), 2)
        for updated in books:
            self.assertEqual([(model['id'], model['name']) for model in updated['models']],
                             [(original_model['id'], '新姓名')])
            self.assertIn('avatar_url', updated['models'][0])
            self.assertIn('avatar_revision', updated['models'][0])
            self.assertIsInstance(updated['models'][0]['avatar_url'], str)
            self.assertIsInstance(updated['models'][0]['avatar_revision'], int)
            self.assertEqual(updated['author'], '新姓名')
        self.assertEqual({model['name'] for model in self.app.request('/api/models')[1]['models']}, {'新姓名'})

    def test_added_at_is_first_import_time_and_survives_metadata_exclusions_and_restart(self):
        first_path = self.album('模特甲/2026-10-09 初次导入')
        os.utime(first_path, (100, 100))
        library = self.create_library()
        first = self.app.books(library['id'])['books'][0]
        self.assertGreater(first['added_at'], 100)
        self.assertEqual(self.app.request('/api/books/' + first['id'] + '/metadata', {'title': '编辑名称'})[0], 200)
        self.assertEqual(self.rescan(library)[0]['added_at'], first['added_at'])
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'exclude_rules': ['*']})[0], 200)
        self.assertEqual(self.app.books(library['id'])['books'], [])
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'exclude_rules': []})[0], 200)
        restored = self.app.books(library['id'])['books'][0]
        self.assertEqual((restored['id'], restored['added_at']), (first['id'], first['added_at']))
        self.app.stop()
        self.app.start()
        self.assertEqual(self.detail(first)['added_at'], first['added_at'])
        time.sleep(0.02)
        self.album('模特甲/2026-10-10 后来导入')
        books = self.rescan(library)
        later = next(book for book in books if book['id'] != first['id'])
        self.assertGreater(later['added_at'], first['added_at'])
        self.assertEqual(next(book for book in books if book['id'] == first['id'])['added_at'], first['added_at'])


class ReadingPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)
        self.library = self.app.create_library(media_type='photo', reading_mode='single',
                                               reading_direction='ltr')
        self.books = self.app.books(self.library['id'])['books']
        self.book = next(book for book in self.books if book['format'] == 'folder')
        self.route = '/api/books/' + self.book['id']
        self.library_route = '/api/libraries/' + self.library['id']

    def test_book_mode_and_direction_override_inherit_and_persist_independently(self):
        self.assertIsNone(self.book['reading_mode_override'])
        self.assertIsNone(self.book['reading_direction_override'])
        self.assertEqual(self.app.request(self.route + '/progress', {'page': 1, 'completed': False})[0], 200)
        for mode in ('double', 'webtoon', 'single'):
            with self.subTest(mode=mode):
                status, data, _ = self.app.request(self.route + '/metadata',
                                                  {'reading_mode': mode, 'reading_direction': 'rtl'})
                self.assertEqual(status, 200)
                self.assertEqual((data['book']['reading_mode'], data['book']['reading_mode_override']),
                                 (mode, mode))
        self.assertEqual(self.app.request(self.library_route,
                                          {'reading_mode': 'double', 'reading_direction': 'rtl'})[0], 200)
        self.app.books(self.library['id'])
        self.app.stop()
        self.app.start()
        book = self.app.request(self.route)[1]
        self.assertEqual((book['reading_mode'], book['reading_mode_override']), ('single', 'single'))
        self.assertEqual(book['reading_direction_override'], 'rtl')
        self.assertEqual(book['progress']['page'], 1)
        status, data, _ = self.app.request(self.route + '/metadata',
                                          {'reading_mode': None, 'reading_direction': None})
        self.assertEqual(status, 200)
        self.assertEqual((data['book']['reading_mode'], data['book']['reading_direction']), ('double', 'rtl'))
        self.assertIsNone(data['book']['reading_mode_override'])
        self.assertIsNone(data['book']['reading_direction_override'])
        self.assertEqual(self.app.request(self.route + '/metadata', {'reading_mode': ''})[0], 200)
        self.assertIsNone(self.app.request(self.route)[1]['reading_mode_override'])

    def test_invalid_book_mode_is_atomic(self):
        for invalid in ('paged', 'follow', 1, True, [], {}):
            with self.subTest(invalid=invalid):
                status, _, _ = self.app.request(self.route + '/metadata',
                                                {'title': '不能保存', 'reading_mode': invalid,
                                                 'reading_direction': 'rtl'})
                self.assertIn(status, (400, 422))
                book = self.app.request(self.route)[1]
                self.assertEqual(book['title'], self.book['title'])
                self.assertIsNone(book['reading_mode_override'])
                self.assertIsNone(book['reading_direction_override'])

    def test_default_changes_keep_overrides_unless_apply_requested(self):
        self.assertEqual(self.app.request(self.route + '/metadata',
                                          {'reading_mode': 'double', 'reading_direction': 'rtl'})[0], 200)
        for apply in (False, None):
            body = {'reading_mode': 'webtoon', 'reading_direction': 'ltr'}
            if apply is not None:
                body['apply_reading_defaults'] = apply
            self.assertEqual(self.app.request(self.library_route, body)[0], 200)
            books = self.app.books(self.library['id'])['books']
            explicit = next(book for book in books if book['id'] == self.book['id'])
            inherited = next(book for book in books if book['id'] != self.book['id'])
            self.assertEqual((explicit['reading_mode'], explicit['reading_direction']), ('double', 'rtl'))
            self.assertEqual((inherited['reading_mode'], inherited['reading_direction']), ('webtoon', 'ttb'))
        self.app.stop()
        self.app.start()
        self.assertEqual(self.app.request(self.route)[1]['reading_mode_override'], 'double')
        library = self.app.request('/api/libraries')[1]['libraries'][0]
        self.assertNotIn('apply_reading_defaults', library)

    def test_apply_defaults_resets_active_and_inactive_books_only_in_target_library(self):
        for book in self.books:
            self.assertEqual(self.app.request('/api/books/' + book['id'] + '/metadata',
                                              {'reading_mode': 'double', 'reading_direction': 'rtl'})[0], 200)
        self.app.request(self.route + '/metadata', {'title': '保留标题', 'release_date': '2020-01-02',
                                                  'cover_page': 1})
        self.app.request(self.route + '/progress', {'page': 1, 'completed': False})
        self.app.request(self.route + '/favorite', {'favorite': True})
        bookmark = self.app.request(self.route + '/bookmarks', {'page': 1, 'note': '保留书签'})[1]['bookmark']
        other_library = self.app.create_library(name='不受影响的库', media_type='photo')
        other = self.app.books(other_library['id'])['books'][0]
        other_route = '/api/books/' + other['id']
        self.app.request(other_route + '/metadata', {'reading_mode': 'webtoon', 'reading_direction': 'rtl'})
        self.assertEqual(self.app.request(self.library_route, {'exclude_rules': ['样例.cbz']})[0], 200)
        self.assertEqual(len(self.app.books(self.library['id'])['books']), 1)
        # Fixture changes may update shared model avatars; snapshot only the action under test.
        before = self.app.request(self.route)[1]
        # Defaults are deliberately unchanged: the explicit action must still reset overrides.
        self.assertEqual(self.app.request(self.library_route, {'apply_reading_defaults': True})[0], 200)
        self.app.books(self.library['id'])
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as db:
            rows = db.execute('SELECT b.active,m.reading_mode_override,m.reading_direction_override '
                              'FROM books b JOIN book_metadata m ON b.id=m.book_id '
                              'WHERE b.library_id=?', (self.library['id'],)).fetchall()
        self.assertEqual(sorted(row[0] for row in rows), [0, 1])
        self.assertTrue(all(row[1:] == (None, None) for row in rows))
        after = self.app.request(self.route)[1]
        for field in ('title', 'release_date', 'cover_page', 'models', 'added_at', 'progress', 'favorite'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['bookmarks'][0]['id'], bookmark['id'])
        other = self.app.request(other_route)[1]
        self.assertEqual((other['reading_mode_override'], other['reading_direction_override']), ('webtoon', 'rtl'))
        self.assertEqual(self.app.request(self.library_route, {'exclude_rules': []})[0], 200)
        restored = self.app.books(self.library['id'])['books']
        self.assertTrue(all(book['reading_mode_override'] is None and
                            book['reading_direction_override'] is None for book in restored))
        self.app.stop()
        self.app.start()
        self.assertIsNone(self.app.request(self.route)[1]['reading_mode_override'])
        self.assertEqual(self.app.request(other_route)[1]['reading_mode_override'], 'webtoon')

    def test_library_flags_require_booleans_and_invalid_edits_are_atomic(self):
        self.assertFalse(self.library['hide_date_prefix'])
        self.app.request(self.route + '/metadata', {'reading_mode': 'double', 'reading_direction': 'rtl'})
        for field in ('hide_date_prefix', 'apply_reading_defaults'):
            for invalid in (None, 0, 1, 'true', [], {}):
                with self.subTest(field=field, invalid=invalid):
                    status, _, _ = self.app.request(self.library_route,
                                                    {'name': '不能保存', 'reading_mode': 'webtoon', field: invalid})
                    self.assertIn(status, (400, 422))
                    library = self.app.request('/api/libraries')[1]['libraries'][0]
                    self.assertEqual((library['name'], library['reading_mode'], library['hide_date_prefix']),
                                     ('测试书库', 'single', False))
                    book = self.app.request(self.route)[1]
                    self.assertEqual((book['reading_mode_override'], book['reading_direction_override']),
                                     ('double', 'rtl'))
                with self.subTest(create_field=field, invalid=invalid):
                    status, _, _ = self.app.request('/api/libraries',
                                                    {'name': '不能创建', 'path': str(self.app.library), field: invalid})
                    self.assertIn(status, (400, 422))
        self.assertEqual(len(self.app.request('/api/libraries')[1]['libraries']), 1)


class PhotoTitleDisplayTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)
        self.collection = self.app.library / '照片显示规则'
        self.collection.mkdir()

    def album(self, name, archive=False):
        path = self.collection / '模特甲' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if archive:
            with zipfile.ZipFile(path, 'w') as contents:
                contents.writestr('1.png', self.app.images[0])
        else:
            path.mkdir()
            (path / '1.png').write_bytes(self.app.images[0])

    def test_date_prefix_hiding_is_reversible_and_preserves_source_release_and_state(self):
        self.album('2026-10-09 夏日作品')
        self.album('2024-02-29 合刊.cbz', archive=True)
        library = self.app.create_library(path=str(self.collection), media_type='photo', hide_date_prefix=True)
        self.assertTrue(library['hide_date_prefix'])
        books = self.app.books(library['id'])['books']
        self.assertEqual({book['title'] for book in books}, {'夏日作品', '合刊'})
        book = next(book for book in books if book['format'] == 'folder')
        self.assertEqual((book['source_title'], book['title_override'], book['release_date']),
                         ('2026-10-09 夏日作品', None, '2026-10-09'))
        self.assertEqual(book['path'], '模特甲/2026-10-09 夏日作品')
        route = '/api/books/' + book['id']
        self.app.request(route + '/progress', {'page': 0, 'completed': False})
        self.app.request(route + '/favorite', {'favorite': True})
        # An unrelated edit must not turn the transformed display name into a manual title.
        self.assertEqual(self.app.request(route + '/metadata', {'reading_mode': 'double'})[0], 200)
        for hidden in (False, True):
            self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'hide_date_prefix': hidden})[0], 200)
            updated = next(item for item in self.app.books(library['id'])['books'] if item['id'] == book['id'])
            self.assertEqual(updated['title'], '夏日作品' if hidden else book['source_title'])
            self.assertEqual(updated['source_title'], book['source_title'])
            self.assertEqual(updated['path'], book['path'])
            self.assertEqual(updated['release_date'], '2026-10-09')
            self.assertEqual(updated['added_at'], book['added_at'])
            self.assertEqual(updated['reading_mode_override'], 'double')
            self.assertTrue(updated['favorite'])
            self.assertEqual(updated['progress']['page'], 0)
        self.app.stop()
        self.app.start()
        self.assertEqual(self.app.request(route)[1]['title'], '夏日作品')
        self.assertTrue(self.app.request('/api/libraries')[1]['libraries'][0]['hide_date_prefix'])

    def test_only_valid_original_date_prefix_is_hidden_and_empty_result_falls_back(self):
        cases = {'2026-10-09 夏日': '夏日', '2024-02-29\u3000闰日': '闰日',
                 '2026-10-09无空格': '无空格', '2026-10-09_符号': '_符号',
                 '2026-10-09': '2026-10-09', '2025-02-29 无效': '2025-02-29 无效',
                 '2026-10-091 数字': '2026-10-091 数字',
                 '2026-10-09９ 全角数字': '2026-10-09９ 全角数字',
                 '作品 2026-10-09': '作品 2026-10-09'}
        for name in cases:
            self.album(name)
        library = self.app.create_library(path=str(self.collection), media_type='photo', hide_date_prefix=True)
        books = self.app.books(library['id'])['books']
        self.assertEqual({book['source_title']: book['title'] for book in books}, cases)

    def test_manual_titles_are_preserved_and_non_photo_titles_are_unchanged(self):
        self.album('2026-10-09 原作')
        photo = self.app.create_library(path=str(self.collection), media_type='photo', hide_date_prefix=True)
        book = self.app.books(photo['id'])['books'][0]
        route = '/api/books/' + book['id']
        self.assertEqual(self.app.request(route + '/metadata', {'title': '2020-01-02 自定标题'})[0], 200)
        for hidden in (False, True):
            self.app.request('/api/libraries/' + photo['id'], {'hide_date_prefix': hidden})
            self.app.books(photo['id'])
            updated = self.app.request(route)[1]
            self.assertEqual((updated['title'], updated['title_override']),
                             ('2020-01-02 自定标题', '2020-01-02 自定标题'))
            self.assertEqual(updated['release_date'], '2026-10-09')
        self.assertEqual(self.app.request(route + '/metadata', {'title': None})[0], 200)
        self.assertEqual(self.app.request(route)[1]['title'], '原作')
        for media_type in ('manga_jp', 'manga_kr', 'comic_us'):
            with self.subTest(media_type=media_type):
                library = self.app.create_library(path=str(self.collection), media_type=media_type,
                                                  hide_date_prefix=True)
                manga = self.app.books(library['id'])['books'][0]
                self.assertEqual(manga['title'], '2026-10-09 原作')
                self.assertEqual(manga['release_date'], '')


if __name__ == '__main__':
    unittest.main()

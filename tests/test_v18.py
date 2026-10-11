"""HTTP checks for 1.8 avatars and reader-only catalogue removal."""
import base64
from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
import urllib.request
import zlib

from PIL import Image

from test_integration import APP, RunningApp, png
from server import Library, ReaderServer


def upload(image, format_name="PNG", **options):
    output = BytesIO()
    image.save(output, format_name, **options)
    mime = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}[format_name]
    return f"data:image/{mime};base64," + base64.b64encode(output.getvalue()).decode()


def source_snapshot(app):
    return {path.relative_to(app.library).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in app.library.rglob("*") if path.is_file()}


class V18Tests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)

    def library(self, **options):
        library = self.app.create_library(**options)
        self.app.books()
        return library

    def test_user_avatar_default_upload_cache_reset_restart(self):
        initial = self.app.request("/api/session")[1]
        self.assertEqual(initial["version"], "1.8.3")
        self.assertTrue(initial["user"]["avatar_url"].startswith("/default-avatar.png"))
        transparent = Image.new("RGBA", (600, 400), (255, 0, 0, 0))
        status, updated, _ = self.app.request("/api/profile", {"avatar_upload": upload(transparent)})
        self.assertEqual(status, 200)
        user = updated["user"]
        self.assertEqual(user["avatar_revision"], 1)
        status, content, headers = self.app.request(user["avatar_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(headers["Cache-Control"], "private, no-cache")
        with Image.open(BytesIO(content)) as image:
            self.assertEqual((image.size, image.mode), ((256, 256), "RGB"))
            self.assertEqual(image.getpixel((128, 128)), (255, 255, 255))
            self.assertFalse(image.getexif())
        self.assertEqual(self.app.request(user["avatar_url"], headers={"If-None-Match": headers["ETag"]})[0], 304)
        self.assertEqual(self.app.request(user["avatar_url"], method="HEAD")[0:2], (200, b""))
        self.app.stop()
        self.app.start()
        self.assertEqual(self.app.request(user["avatar_url"])[1], content)
        self.assertEqual(self.app.request("/api/session")[1]["user"]["avatar_url"], user["avatar_url"])
        status, reset, _ = self.app.request("/api/profile", {"avatar_reset": True})
        self.assertEqual(status, 200)
        self.assertEqual(reset["user"]["avatar_revision"], 2)
        self.assertTrue(reset["user"]["avatar_url"].startswith("/default-avatar.png"))
        self.assertEqual(self.app.request(user["avatar_url"])[0], 404)
        self.assertEqual(list((self.app.data / "avatars").glob("*.jpg")), [])

    def test_user_avatar_exif_orientation_and_webp_replacement(self):
        image = Image.new("RGB", (80, 40), (255, 0, 0))
        image.paste((0, 0, 255), (40, 0, 80, 40))
        exif = Image.Exif()
        exif[274] = 6
        result = self.app.request("/api/profile", {"avatar_upload": upload(image, "JPEG", exif=exif)})
        self.assertEqual(result[0], 200)
        first_url = result[1]["user"]["avatar_url"]
        with Image.open(BytesIO(self.app.request(first_url)[1])) as actual:
            self.assertGreater(actual.getpixel((128, 32))[0], 240)
            self.assertGreater(actual.getpixel((128, 224))[2], 240)
            self.assertFalse(actual.getexif())
        result = self.app.request("/api/profile", {"avatar_upload": upload(image, "WEBP")})
        self.assertEqual(result[0], 200)
        self.assertNotEqual(result[1]["user"]["avatar_url"], first_url)
        self.assertEqual(len(list((self.app.data / "avatars").glob("*.jpg"))), 1)

    def test_model_avatar_name_binding_cover_fallback_and_restart(self):
        self.library(media_type="photo")
        original = self.app.books()["books"][0]
        model = self.app.request("/api/models", {"name": "测试模特"})[1]["model"]
        self.app.request(f"/api/books/{original['id']}/metadata", {"model_ids": [model["id"]]})
        model = next(item for item in self.app.request("/api/models")[1]["models"] if item["id"] == model["id"])
        self.assertEqual(model["avatar_url"], f"/api/books/{original['id']}/cover")
        result = self.app.request(f"/api/models/{model['id']}", {
            "name": "改名模特", "avatar_upload": upload(Image.new("RGB", (50, 100), (20, 80, 180)))})
        self.assertEqual(result[0], 200)
        changed = result[1]["model"]
        self.assertEqual((changed["name"], changed["avatar_revision"]), ("改名模特", 1))
        self.assertEqual(self.app.request(changed["avatar_url"])[0], 200)
        bound = self.app.request(f"/api/books/{original['id']}")[1]["models"][0]
        self.assertEqual((bound["id"], bound["name"], bound["avatar_url"]),
                         (changed["id"], "改名模特", changed["avatar_url"]))
        self.app.stop()
        self.app.start()
        self.app.books()
        self.assertEqual(self.app.request(changed["avatar_url"])[0], 200)
        result = self.app.request(f"/api/models/{model['id']}", {"avatar_reset": True})
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["model"]["name"], "改名模特")
        self.assertEqual(result[1]["model"]["avatar_url"], f"/api/books/{original['id']}/cover")

    def test_avatar_validation_is_atomic_and_limits_formats_bytes_pixels(self):
        model = self.app.request("/api/models", {"name": "原名"})[1]["model"]
        valid = upload(Image.new("RGB", (2, 2), (10, 20, 30)))
        raw = base64.b64decode(valid.split(",", 1)[1])
        ihdr = struct.pack("!2I5B", 10000, 4001, 8, 2, 0, 0, 0)
        huge = raw[:8] + struct.pack("!I", len(ihdr)) + b"IHDR" + ihdr + struct.pack("!I", zlib.crc32(b"IHDR" + ihdr)) + raw[33:]
        bad_values = ["data:image/png;base64,???", {"data": "bm90LWFuLWltYWdl"},
                      upload(Image.new("RGB", (2, 2)), "GIF"),
                      {"data": base64.b64encode(raw).decode(), "mime": "image/webp"},
                      {"data": base64.b64encode(huge).decode()},
                      {"data": "A" * (8 * 1024 * 1024 + 4)}]
        for bad in bad_values:
            with self.subTest(value_type=type(bad).__name__):
                self.assertIn(self.app.request(f"/api/models/{model['id']}", {
                    "name": "不得保存", "avatar_upload": bad})[0], (400, 413, 422))
                self.assertIn(self.app.request("/api/profile", {
                    "display_name": "不得保存", "avatar_upload": bad})[0], (400, 413, 422))
        self.assertEqual(self.app.request("/api/session")[1]["user"]["display_name"], "测试用户")
        current = self.app.request("/api/models")[1]["models"]
        self.assertEqual((current[0]["name"], current[0]["avatar_revision"]), ("原名", 0))
        self.assertEqual(list((self.app.data / "avatars").glob("*")), [])
        self.assertEqual(self.app.request("/api/profile", {"avatar_reset": "yes"})[0], 400)
        self.assertEqual(self.app.request("/api/profile", {"avatar_reset": True, "avatar_upload": valid})[0], 400)
        self.assertEqual(self.app.request("/api/models", {"name": "不应创建", "avatar_upload": bad_values[0]})[0], 400)
        self.assertEqual(len(self.app.request("/api/models")[1]["models"]), 1)

    def test_new_routes_require_authentication_and_matching_origin(self):
        library = self.library()
        books = self.app.books()["books"]
        model = self.app.request("/api/models", {"name": "权限测试"})[1]["model"]
        self.app.request(f"/api/models/{model['id']}", {"avatar_upload": upload(Image.new("RGB", (2, 2)))})
        routes = [('/api/profile', {'avatar_reset': True}),
                  (f"/api/models/{model['id']}", {'avatar_reset': True}),
                  ('/api/libraries/batch', {'action': 'remove', 'ids': [library['id']]}),
                  ('/api/books/batch', {'action': 'remove', 'ids': [books[0]['id']]}),
                  (f"/api/libraries/{library['id']}/remove", {}),
                  (f"/api/books/{books[0]['id']}/remove", {})]
        for path, body in routes:
            self.assertEqual(self.app.request(path, body, {'Origin': 'https://example.invalid'})[0], 403)
            self.assertEqual(self.app.request(path, body, {'Cookie': ''})[0], 401)
        self.assertEqual(self.app.request(f"/api/avatars/models/{model['id']}", headers={'Cookie': ''})[0], 401)
        self.assertEqual(self.app.request('/api/avatars/user/1', headers={'Cookie': ''})[0], 401)
        self.assertEqual(len(self.app.books()['books']), 2)

    def test_batch_validation_and_favorite_are_atomic(self):
        library = self.library()
        ids = [item['id'] for item in self.app.books()['books']]
        unknown = 'f' * 24
        for action in ('favorite', 'remove'):
            self.assertEqual(self.app.request('/api/books/batch', {'action': action, 'ids': [ids[0], unknown]})[0], 404)
        for action in ('scan', 'remove'):
            self.assertEqual(self.app.request('/api/libraries/batch', {'action': action, 'ids': [library['id'], unknown]})[0], 404)
        self.assertEqual(len(self.app.books()['books']), 2)
        self.assertFalse(any(book['favorite'] for book in self.app.books()['books']))
        invalid = [[], [ids[0]] * 501, ['../bad'], 'not-an-array', [123]]
        for selected in invalid:
            self.assertEqual(self.app.request('/api/books/batch', {'action': 'remove', 'ids': selected})[0], 400)
        result = self.app.request('/api/books/batch', {'action': 'favorite', 'ids': ids + [ids[0]]})
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]['updated'], ids)
        self.assertTrue(all(book['favorite'] for book in self.app.books()['books']))
        self.assertEqual(self.app.request('/api/books/batch', {'action': 'favorite', 'ids': ids, 'favorite': 'true'})[0], 400)
        self.assertEqual(self.app.request('/api/books/batch', {'action': 'favorite', 'ids': ids, 'favorite': False})[0], 200)
        self.assertFalse(any(book['favorite'] for book in self.app.books()['books']))

    def test_book_removal_clears_state_preserves_sources_and_survives_scans_restart(self):
        library = self.library(media_type='photo')
        other = self.library(name='同目录独立库', media_type='photo')
        original = source_snapshot(self.app)
        book = self.app.books(library['id'])['books'][0]
        route = f"/api/books/{book['id']}"
        self.app.request(route + '/progress', {'page': 1, 'completed': False})
        self.app.request(route + '/favorite', {'favorite': True})
        self.app.request(route + '/bookmarks', {'page': 1, 'note': '移除测试'})
        self.assertEqual(self.app.request(route + '/cover')[0], 200)
        self.assertTrue(list((self.app.data / 'covers').glob(book['id'] + '-*.jpg')))
        result = self.app.request(route + '/remove', {})
        self.assertEqual(result[0:2], (200, {'removed': [book['id']]}))
        self.assertEqual(self.app.request(route)[0], 404)
        self.assertEqual(self.app.request(route + '/pages/0')[0], 404)
        self.assertEqual(self.app.request(route + '/cover')[0], 404)
        self.assertEqual(list((self.app.data / 'covers').glob(book['id'] + '-*.jpg')), [])
        self.assertEqual(self.app.request('/api/bookmarks')[1]['bookmarks'], [])
        self.app.request('/api/scan', {})
        self.assertEqual(len(self.app.books(library['id'])['books']), 1)
        self.assertEqual(len(self.app.books(other['id'])['books']), 2)
        self.app.stop()
        self.app.start()
        self.assertEqual(len(self.app.books(library['id'])['books']), 1)
        self.assertEqual(len(self.app.books(other['id'])['books']), 2)
        self.assertEqual(source_snapshot(self.app), original)
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT active,removed FROM books WHERE id=?', (book['id'],)).fetchone(), (0, 1))
            for table in ('progress', 'favorites', 'bookmarks', 'book_models', 'book_metadata'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table} WHERE book_id=?', (book['id'],)).fetchone()[0], 0)

    def test_library_batch_scan_remove_preserves_shared_models_and_sources(self):
        first = self.library(media_type='photo')
        second = self.library(name='第二库', media_type='photo')
        third = self.library(name='保留库', media_type='photo')
        original = source_snapshot(self.app)
        before_models = self.app.request('/api/models')[1]['models']
        before_ids = {item['id'] for item in before_models}
        selected = [first['id'], second['id']]
        self.assertEqual(self.app.request('/api/libraries/batch', {'action': 'scan', 'ids': selected})[0], 202)
        self.app.books()
        removed_books = [book for book in self.app.books()['books'] if book['library_id'] in selected]
        for book in removed_books:
            route = f"/api/books/{book['id']}"
            self.app.request(route + '/progress', {'page': 0, 'completed': False})
            self.app.request(route + '/favorite', {'favorite': True})
            self.app.request(route + '/bookmarks', {'page': 0})
        result = self.app.request('/api/libraries/batch', {'action': 'remove', 'ids': selected})
        self.assertEqual(result[0:2], (200, {'removed': selected}))
        self.assertEqual([item['id'] for item in self.app.request('/api/libraries')[1]['libraries']], [third['id']])
        self.assertEqual(len(self.app.books()['books']), 2)
        self.assertEqual({item['id'] for item in self.app.request('/api/models')[1]['models']}, before_ids)
        self.assertEqual(self.app.request('/api/bookmarks')[1]['bookmarks'], [])
        self.app.stop()
        self.app.start()
        self.assertEqual(len(self.app.books()['books']), 2)
        self.assertEqual(source_snapshot(self.app), original)
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM books').fetchone()[0], 2)
        self.assertEqual(self.app.request(f"/api/libraries/{third['id']}/remove", {})[0], 200)
        self.assertEqual(self.app.books()['books'], [])
        self.assertEqual(source_snapshot(self.app), original)

    def test_v17_schema_migration_preserves_account_sessions_and_catalogue(self):
        library = self.library(media_type='photo')
        book = self.app.books()['books'][0]
        self.app.request('/api/profile', {'avatar_color': '#123456'})
        self.app.request(f"/api/books/{book['id']}/favorite", {'favorite': True})
        self.app.stop()
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as db, db:
            owner = db.execute('SELECT username,password_hash FROM owner_account').fetchone()
            sessions = db.execute('SELECT * FROM auth_sessions').fetchall()
            for table in ('owner_account', 'models'):
                db.execute(f'ALTER TABLE {table} DROP COLUMN avatar_file')
                db.execute(f'ALTER TABLE {table} DROP COLUMN avatar_revision')
            db.execute('ALTER TABLE books DROP COLUMN removed')
        self.app.start()
        books = self.app.books()['books']
        user = self.app.request('/api/session')[1]
        self.assertTrue(user['authenticated'])
        self.assertEqual(user['user']['avatar_color'], '#123456')
        self.assertEqual(user['user']['avatar_revision'], 0)
        self.assertTrue(user['user']['avatar_url'].startswith('/default-avatar.png'))
        self.assertEqual(len(books), 2)
        self.assertTrue(next(item for item in books if item['id'] == book['id'])['favorite'])
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT username,password_hash FROM owner_account').fetchone(), owner)
            self.assertEqual(db.execute('SELECT * FROM auth_sessions').fetchall(), sessions)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.app.request(f"/api/libraries/{library['id']}/remove", {})[0], 200)


class ScanRemovalRaceTests(unittest.TestCase):
    """Pause a real scan before publication, then remove through HTTP."""
    def setUp(self):
        scratch = Path(os.getenv('SUMI_TEST_TEMP') or APP / 'work' / 'test-runtime')
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='gravelle-race-', dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        folder = self.media / '作品'
        folder.mkdir(parents=True)
        self.source = folder / '1.png'
        self.original = png((20, 60, 120))
        self.source.write_bytes(self.original)
        self.library = Library(self.media, self.root / 'data', '', 0)
        self.addCleanup(self.library.close)
        self.library_id = self.library.save_library({'name': '竞争测试', 'path': str(self.media)})['library']['id']
        self.wait_scan()
        self.book_id = self.library.listing()['books'][0]['id']
        self.library.register({'username': 'race-reader', 'password': 'temporary-race-password'}, 'test')
        self.token = self.library.make_session(1)
        self.server = ReaderServer(('127.0.0.1', 0), self.library)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wait_scan(self):
        deadline = time.monotonic() + 5
        while self.library.scanning and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.library.scanning)

    def remove_during_scan(self, kind, identifier):
        prepared = threading.Event()
        release = threading.Event()
        original_book = self.library._book

        def paused_book(*args, **kwargs):
            result = original_book(*args, **kwargs)
            prepared.set()
            if not release.wait(5):
                raise RuntimeError('Test scan was not released')
            return result

        self.library._book = paused_book
        self.library.start_scan(self.library_id)
        try:
            self.assertTrue(prepared.wait(5), 'Scan did not reach its preparation boundary')
            request = urllib.request.Request(
                f'http://127.0.0.1:{self.server.server_port}/api/{kind}/{identifier}/remove',
                data=b'{}', headers={'Content-Type': 'application/json', 'Cookie': 'sumi_session=' + self.token})
            with self.client.open(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {'removed': [identifier]})
        finally:
            release.set()
            self.wait_scan()
            self.library._book = original_book
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_book_removed_while_scan_is_prepared_cannot_be_republished(self):
        self.remove_during_scan('books', self.book_id)
        self.assertEqual(self.library.listing()['books'], [])
        self.library.start_scan(self.library_id)
        self.wait_scan()
        self.assertEqual(self.library.listing()['books'], [])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_library_removed_while_scan_is_prepared_cannot_be_recreated(self):
        self.remove_during_scan('libraries', self.library_id)
        self.assertEqual(self.library.libraries()['libraries'], [])
        self.assertEqual(self.library.listing()['books'], [])
        with closing(self.library.connect()) as connection:
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM books').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()

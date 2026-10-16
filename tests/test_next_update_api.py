"""HTTP integration of the new management features using disposable images only."""
from contextlib import closing
import http.client
import json
import re
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PIL import Image
from server import COOKIE_NAME, Library, ReaderServer

ROOT = Path(__file__).resolve().parents[1]


class NextUpdateAPITests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('SUMI_TEST_TEMP', ROOT / 'work/next-update-api'))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        for index in range(5):
            album = self.media / f'Album {index}'
            album.mkdir(parents=True)
            Image.new('RGB', (64, 96), (index * 40, 80, 140)).save(album / '1.png')
        self.app = Library(self.media, self.root / 'data', '', 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = self.app.image_search.auto_start = False
        self.app.register({'username': 'api-admin', 'password': 'temporary-admin-password'}, 'test')
        self.library = self.app.save_library({'name': 'Generated', 'path': str(self.media), 'media_type': 'photo'})['library']
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)
        self.book = self.app.listing()['books'][0]
        self.child = self.app.save_user({'username': 'api-child', 'password': 'temporary-child-password', 'library_ids': [self.library['id']]})['user']
        self.cookies = {'admin': f'{COOKIE_NAME}={self.app.make_session(1)}', 'child': f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"}
        self.server = ReaderServer(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, body=None, actor='admin', headers=None):
        metadata = {'Cookie': self.cookies[actor]} if actor else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            metadata['Content-Type'] = 'application/json'
        metadata.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        try:
            connection.request('POST' if body is not None else 'GET', path, body=data, headers=metadata)
            response = connection.getresponse()
            content = response.read()
            if content and 'application/json' in response.getheader('Content-Type', ''):
                content = json.loads(content)
            return response.status, content, dict(response.headers)
        finally:
            connection.close()

    def test_administrator_management_routes_and_strict_pagination(self):
        for path in ('/api/cache', '/api/image-search/failures', f"/api/transcoding/books/{self.book['id']}/details?page=1&page_size=1"):
            status, result, _ = self.request(path)
            self.assertEqual(status, 200, (path, result))
        status, cache, _ = self.request('/api/cache')
        self.assertEqual(cache['settings']['cover_limit_mb'], 512)
        self.assertEqual(self.request('/api/cache/settings', {'expected_revision': cache['settings']['revision'], 'cover_limit_mb': 64})[0], 200)
        for query in ('page=0', 'page=1.5', 'page=1&page=2', 'other=1', 'page_size=101'):
            self.assertEqual(self.request('/api/image-search/failures?' + query)[0], 400, query)
        self.assertEqual(self.request('/api/cache?path=/tmp')[0], 400)
        self.assertEqual(self.request('/api/image-search/retry', {'items': []})[0], 400)

    def test_every_script_declared_by_the_page_is_served_before_login(self):
        status, html, _ = self.request('/', actor=None)
        self.assertEqual(status, 200)
        for script in re.findall(rb'<script src="([^"]+)"', html):
            status, content, headers = self.request(script.decode('ascii'), actor=None)
            self.assertEqual(status, 200, script)
            self.assertTrue(content)
            self.assertEqual(headers['Content-Type'].split(';', 1)[0], 'text/javascript')

    def test_child_accounts_cannot_read_or_mutate_admin_resources(self):
        for path in ('/api/cache', '/api/image-search/failures', f"/api/transcoding/books/{self.book['id']}/details"):
            self.assertEqual(self.request(path, actor='child')[0], 403, path)
        for path, body in (
            ('/api/cache/settings', {'expected_revision': 1, 'cover_limit_mb': 32}),
            ('/api/cache/clear', {'kind': 'covers', 'confirm': True}),
            ('/api/image-search/retry', {'items': []}),
            (f"/api/libraries/{self.library['id']}/cover/shuffle", {}),
        ):
            self.assertEqual(self.request(path, body, actor='child')[0], 403, path)

    def test_composite_conditional_read_rechecks_revoked_access(self):
        self.app.library_covers.metadata(self.library['id'])
        self.assertTrue(self.app.library_covers.run_once())
        path = f"/api/libraries/{self.library['id']}/cover"
        status, image, headers = self.request(path, actor='child')
        self.assertEqual(status, 200)
        self.assertTrue(image.startswith(b'\xff\xd8'))
        self.assertEqual(self.request(path, actor='child', headers={'If-None-Match': headers['ETag']})[0], 304)
        self.app.save_user({'library_ids': []}, self.child['id'])
        self.assertEqual(self.request(path, actor='child', headers={'If-None-Match': headers['ETag']})[0], 401)
        self.cookies['child'] = f"{COOKIE_NAME}={self.app.make_session(self.child['id'])}"
        self.assertEqual(self.request(path, actor='child', headers={'If-None-Match': headers['ETag']})[0], 404)
        self.assertEqual(self.request(path, actor=None, headers={'If-None-Match': headers['ETag']})[0], 401)

    def test_shuffle_updates_cached_selection_without_rescanning_or_editing_sources(self):
        with closing(self.app.connect()) as connection:
            before = connection.execute('SELECT last_scan FROM media_libraries WHERE id=?', (self.library['id'],)).fetchone()[0]
        status, result, _ = self.request(f"/api/libraries/{self.library['id']}/cover/shuffle", {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['count'], 4)
        self.assertTrue(result['pending'])
        self.assertEqual(self.request(f"/api/libraries/{self.library['id']}/cover/shuffle", {'path': '/tmp'})[0], 400)
        with closing(self.app.connect()) as connection:
            self.assertEqual(connection.execute('SELECT last_scan FROM media_libraries WHERE id=?', (self.library['id'],)).fetchone()[0], before)
        self.assertEqual(len(list(self.media.rglob('*.png'))), 5)


if __name__ == '__main__':
    unittest.main()

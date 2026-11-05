"""Large library pages stay readable without unbounded ordinary HTTP reads.

Only generated two-pixel PNGs are used. Sparse trailing padding makes their
actual file size exceed the previous cap without allocating huge pixel arrays.
"""
from contextlib import contextmanager
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from server import COOKIE_NAME, Library, ReaderError, ReaderServer, validate_archive_image
from test_integration import png


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
LARGE_SIZE = 64 * MIB + 1234


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(MIB):
            digest.update(chunk)
    return digest.hexdigest()


class LargeLibraryImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = Path(os.environ.get('SUMI_TEST_TEMP', ROOT / 'work' / 'large-image-tests'))
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='generated-large-', dir=scratch)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.media = cls.root / 'media'
        cls.album = cls.media / 'Generated large album'
        cls.album.mkdir(parents=True)
        cls.header = png((70, 150, 210))
        cls.source = cls.album / '1.png'
        with cls.source.open('wb') as handle:
            handle.write(cls.header)
            handle.truncate(LARGE_SIZE)
        (cls.album / '2.png').write_bytes(cls.header)
        (cls.album / 'empty.png').touch()
        cls.digest = file_digest(cls.source)
        cls.archive = cls.media / 'Generated large archive.cbz'
        with zipfile.ZipFile(cls.archive, 'w', compression=zipfile.ZIP_STORED) as handle:
            handle.write(cls.source, 'nested/1.png')
        cls.library = Library(cls.media, cls.root / 'data', '', 0)
        cls.addClassCleanup(cls.library.close)
        cls.library.transcoding.auto_start = cls.library.image_search.auto_start = False
        # Montage workers are outside this test's page-stream observations.
        cls.cover_reconcile = patch.object(cls.library.library_covers, 'reconcile')
        cls.cover_reconcile.start()
        cls.addClassCleanup(cls.cover_reconcile.stop)
        cls.library.register({'username': 'large-admin', 'password': 'generated-large-image-password'}, 'fixture')
        cls.library_id = cls.library.save_library({
            'name': 'Generated large image library', 'path': str(cls.media), 'media_type': 'photo'
        })['library']['id']
        deadline = time.monotonic() + 20
        while cls.library.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        if cls.library.scanning:
            raise AssertionError('Generated large-image scan did not finish')
        cls.books = {book['format']: book for book in cls.library.listing()['books']}
        cls.admin_cookie = f'{COOKIE_NAME}={cls.library.make_session(1)}'
        denied = cls.library.save_user({
            'username': 'large-denied', 'password': 'generated-denied-password', 'library_ids': []
        })['user']
        granted = cls.library.save_user({
            'username': 'large-granted', 'password': 'generated-granted-password', 'library_ids': [cls.library_id]
        })['user']
        cls.denied_cookie = f'{COOKIE_NAME}={cls.library.make_session(denied["id"])}'
        cls.granted_cookie = f'{COOKIE_NAME}={cls.library.make_session(granted["id"])}'
        cls.server = ReaderServer(('127.0.0.1', 0), cls.library)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.close_server)

    @classmethod
    def close_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def book(self, format_name):
        self.assertIn(format_name, self.books, self.library.listing()['errors'])
        return self.books[format_name]

    def request(self, path, *, method='GET', cookie=None, headers=None):
        metadata = {'Cookie': cookie or self.admin_cookie, **(headers or {})}
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        try:
            client.request(method, path, headers=metadata)
            response = client.getresponse()
            digest, total, beginning = hashlib.sha256(), 0, b''
            while chunk := response.read(MIB):
                if not beginning:
                    beginning = chunk[:len(self.header)]
                total += len(chunk)
                digest.update(chunk)
            return response.status, dict(response.headers), total, digest.hexdigest(), beginning
        finally:
            client.close()

    @contextmanager
    def observe_source_reads(self, format_name):
        sizes = []
        if format_name == 'folder':
            original = self.library._open_media_file
            key = 'Generated large album/1.png'

            class Observed:
                def __init__(self, source):
                    self.source = source

                def __getattr__(self, name):
                    return getattr(self.source, name)

                def read(self, size=-1):
                    sizes.append(size)
                    if size is None or size < 0 or size > 4 * MIB:
                        raise AssertionError('Ordinary image HTTP must read bounded blocks')
                    return self.source.read(size)

            @contextmanager
            def opened(root, relative):
                with original(root, relative) as source:
                    yield Observed(source) if relative == key else source

            with patch.object(self.library, '_open_media_file', opened):
                yield sizes
        else:
            original = zipfile.ZipExtFile.read

            def observed(source, size=-1):
                sizes.append(size)
                if size is None or size < 0 or size > 4 * MIB:
                    raise AssertionError('Archive image HTTP must read bounded blocks')
                return original(source, size)

            with patch.object(zipfile.ZipExtFile, 'read', observed):
                yield sizes

    def test_folder_scan_keeps_large_page_and_still_reports_empty_file(self):
        book = self.book('folder')
        self.assertGreater(self.source.stat().st_size, 64 * MIB)
        self.assertEqual(book['page_count'], 2)
        pages = json.loads(self.library.book(book['id'], internal=True)['pages'])
        self.assertEqual(pages, ['Generated large album/1.png', 'Generated large album/2.png'])
        errors = self.library.listing()['errors']
        self.assertTrue(any(error['path'].endswith('empty.png') and '为空' in error['message'] for error in errors))
        self.assertFalse(any('超过 64' in error['message'] for error in errors))

    def test_stored_archive_and_existing_bytes_api_keep_the_exact_large_page(self):
        book = self.book('cbz')
        self.assertEqual(book['page_count'], 1)
        with self.library.open_image(book['id'], 0) as (source, length, mime, etag):
            self.assertEqual(length, LARGE_SIZE)
            self.assertEqual(mime, 'image/png')
            digest = hashlib.sha256()
            while chunk := source.read(MIB):
                digest.update(chunk)
            self.assertEqual(digest.hexdigest(), self.digest)
            self.assertEqual(etag, '"' + self.digest + '"')
        data, mime, etag = self.library.image(book['id'], 0)
        self.assertEqual((len(data), mime, etag), (LARGE_SIZE, 'image/png', '"' + self.digest + '"'))
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.digest)

    def assert_http_page(self, format_name):
        self.library.source_pages.clear()
        book = self.book(format_name)
        path = f'/api/books/{book["id"]}/pages/0'
        with self.observe_source_reads(format_name) as reads:
            status, headers, total, digest, beginning = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual((total, digest, beginning), (LARGE_SIZE, self.digest, self.header))
            self.assertEqual(headers['Content-Length'], str(LARGE_SIZE))
            self.assertEqual(headers['Content-Type'], 'image/png')
            self.assertEqual(headers['ETag'], '"' + self.digest + '"')
            self.assertTrue(reads, 'HTTP unexpectedly bypassed the source stream')
            self.assertTrue(all(0 <= size <= 4 * MIB for size in reads), reads)
            status, head, total, _, _ = self.request(path, method='HEAD')
            self.assertEqual((status, total), (200, 0))
            self.assertEqual(head['Content-Length'], str(LARGE_SIZE))
            self.assertEqual(head['ETag'], headers['ETag'])
            status, cached, total, _, _ = self.request(path, headers={'If-None-Match': headers['ETag']})
            self.assertEqual((status, total), (304, 0))
            self.assertEqual(cached['ETag'], headers['ETag'])

    def test_http_folder_page_streams_and_preserves_head_and_cache_contracts(self):
        self.assert_http_page('folder')

    def test_http_stored_archive_page_streams_without_collecting_whole_entry(self):
        self.assert_http_page('cbz')

    def test_warm_get_head_and_304_skip_reopening_archive_member(self):
        self.library.source_pages.clear()
        book = self.book('cbz')
        path = f'/api/books/{book["id"]}/pages/0'
        with self.observe_source_reads('cbz') as reads:
            status, headers, size, digest, _ = self.request(path)
            self.assertEqual((status, size, digest), (200, LARGE_SIZE, self.digest))
            if not self.library.source_pages.snapshot()['entries']:
                self.skipTest('The source filesystem cannot safely identify revisions')
            self.assertLessEqual(sum(reads), LARGE_SIZE + 1)
            reads.clear()
            self.assertEqual(self.request(path, method='HEAD')[0], 200)
            self.assertEqual(self.request(path, headers={'If-None-Match': headers['ETag']})[0], 304)
            self.assertEqual(self.request(path)[2:4], (LARGE_SIZE, self.digest))
            self.assertEqual(reads, [])
        self.assertEqual(self.request(path, cookie=self.denied_cookie)[0], 404)

    def test_same_length_and_restored_mtime_cannot_revive_old_folder_page(self):
        book = self.book('folder')
        source = self.album / '2.png'
        stamp = source.stat()
        original = source.read_bytes()
        changed = original[:-1] + bytes([original[-1] ^ 1])
        path = f'/api/books/{book["id"]}/pages/1'
        first = self.request(path)
        try:
            source.write_bytes(changed)
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            second = self.request(path, headers={'If-None-Match': first[1]['ETag']})
            self.assertEqual(second[0], 200)
            self.assertEqual(second[3], hashlib.sha256(changed).hexdigest())
            self.assertNotEqual(first[1]['ETag'], second[1]['ETag'])
        finally:
            source.write_bytes(original)
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    def test_child_account_still_needs_library_access_before_any_source_read(self):
        book = self.book('folder')
        path = f'/api/books/{book["id"]}/pages/0'
        with patch.object(self.library, '_open_media_file', side_effect=AssertionError('Denied reader opened source')):
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(path, method=method, cookie=self.denied_cookie)[0], 404)
            preview = f'/api/transcoding/books/{book["id"]}/pages/0'
            self.assertEqual(self.request(preview, cookie=self.granted_cookie)[0], 403)
        status, headers, total, _, _ = self.request(path, method='HEAD', cookie=self.granted_cookie)
        self.assertEqual((status, headers['Content-Length'], total), (200, str(LARGE_SIZE), 0))

    def test_management_original_preview_streams_the_large_source(self):
        self.library.source_pages.clear()
        book = self.book('folder')
        with self.observe_source_reads('folder') as reads:
            status, headers, total, digest, _ = self.request(f'/api/transcoding/books/{book["id"]}/pages/0')
        self.assertEqual((status, total, digest), (200, LARGE_SIZE, self.digest))
        self.assertEqual(headers['Content-Length'], str(LARGE_SIZE))
        self.assertTrue(reads)

    def test_account_revoked_during_source_hash_receives_no_image(self):
        self.library.source_pages.clear()
        book = self.book('folder')
        user = self.library.save_user({
            'username': 'large-revoked', 'password': 'generated-revoked-password', 'library_ids': [self.library_id]
        })['user']
        cookie = f'{COOKIE_NAME}={self.library.make_session(user["id"])}'
        original = self.library._prepare_image_stream

        def revoked(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.library.acting_as(1):
                self.library.save_user({'enabled': False}, user['id'])
            return result

        with patch.object(self.library, '_prepare_image_stream', revoked):
            status, headers, total, _, _ = self.request(f'/api/books/{book["id"]}/pages/0', cookie=cookie)
        self.assertEqual(status, 401)
        self.assertTrue(headers['Content-Type'].startswith('application/json'))
        self.assertLess(total, 4096)

    def test_large_file_cover_remains_small_and_original_stays_unchanged(self):
        book = self.book('folder')
        data, mime, _ = self.library.cover(book['id'])
        self.assertEqual(mime, 'image/jpeg')
        self.assertLess(len(data), MIB)
        with Image.open(BytesIO(data)) as image:
            self.assertLessEqual(image.width, 400)
            self.assertLessEqual(image.height, 600)
        self.assertEqual(self.source.stat().st_size, LARGE_SIZE)
        self.assertEqual(file_digest(self.source), self.digest)

    def test_archive_compression_bomb_protection_is_not_removed(self):
        info = zipfile.ZipInfo('generated.png')
        info.file_size = 2 * MIB
        info.compress_size = 1
        info.compress_type = zipfile.ZIP_DEFLATED
        with self.assertRaises(ReaderError) as caught:
            validate_archive_image(info)
        self.assertIn('压缩比例异常', str(caught.exception))

    @contextmanager
    def prepared_file(self, *, mutate=False, fail_on_exit=False):
        """Inject a real stream at the post-hash boundary, never user media."""
        with tempfile.TemporaryDirectory(prefix='stream-race-', dir=self.root) as temporary:
            path = Path(temporary) / 'generated.png'
            data = self.header + bytes(2 * MIB + 123 - len(self.header))
            path.write_bytes(data)
            with path.open('rb') as source:
                prepared = self.library._prepare_image_stream(source, len(data), 'image/png')
                if mutate:
                    # Change padding after IEND without changing the file size
                    # or inode, after the advertised content digest is fixed.
                    with path.open('r+b') as writer:
                        writer.seek(len(self.header) + 1)
                        writer.write(b'\x01')
                    self.assertEqual(path.stat().st_size, len(data))
                yield prepared, data
            if fail_on_exit:
                raise ReaderError('Generated image context exit failure', 422)

    def test_bytes_consumer_rejects_source_changed_after_the_initial_hash(self):
        book = self.book('folder')

        @contextmanager
        def changed(*args, **kwargs):
            with self.prepared_file(mutate=True) as (prepared, _data):
                yield prepared

        with patch.object(self.library, 'open_image', changed):
            with self.assertRaises(ReaderError) as caught:
                self.library.image(book['id'], 0)
        self.assertEqual(caught.exception.status, 422)

    def test_http_same_size_change_does_not_complete_an_inconsistent_response(self):
        book = self.book('folder')

        @contextmanager
        def changed(*args, **kwargs):
            with self.prepared_file(mutate=True) as (prepared, _data):
                yield prepared

        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        try:
            with patch.object(self.library, 'open_image', changed):
                client.request('GET', f'/api/books/{book["id"]}/pages/0', headers={'Cookie': self.admin_cookie})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                with self.assertRaises(http.client.IncompleteRead) as caught:
                    response.read()
                self.assertLess(len(caught.exception.partial), int(response.getheader('Content-Length')))
                self.assertNotIn(b'Generated image', caught.exception.partial)
                self.assertNotIn(b'HTTP/1.', caught.exception.partial)
        finally:
            client.close()

    def test_stream_context_exit_error_does_not_append_a_second_http_response(self):
        book = self.book('folder')
        expected = self.header + bytes(2 * MIB + 123 - len(self.header))

        @contextmanager
        def failing_exit(*args, **kwargs):
            with self.prepared_file(fail_on_exit=True) as (prepared, _data):
                yield prepared

        with patch.object(self.library, 'open_image', failing_exit):
            with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=15) as connection:
                request = (f'GET /api/books/{book["id"]}/pages/0 HTTP/1.1\r\n'
                           f'Host: 127.0.0.1:{self.server.server_port}\r\n'
                           f'Cookie: {self.admin_cookie}\r\nConnection: close\r\n\r\n')
                connection.sendall(request.encode('ascii'))
                parts = []
                while part := connection.recv(64 * 1024):
                    parts.append(part)
        response = b''.join(parts)
        headers, body = response.split(b'\r\n\r\n', 1)
        self.assertIn(b' 200 ', headers.split(b'\r\n', 1)[0])
        self.assertEqual(len(body), len(expected))
        self.assertEqual(hashlib.sha256(body).digest(), hashlib.sha256(expected).digest())
        self.assertEqual(response.count(b'HTTP/1.'), 1)


if __name__ == '__main__':
    unittest.main()

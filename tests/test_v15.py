"""v1.5 contracts exercised through HTTP using only disposable synthetic media."""
import base64
from contextlib import closing
import hashlib
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import struct
import time
import unittest
import zipfile
import zlib

from PIL import Image

from test_integration import RunningApp, assert_cover, png


def image_bytes(color, size=(1200, 1800), image_format='PNG', **save_options):
    output = BytesIO()
    Image.new('RGB', size, color).save(output, image_format, **save_options)
    return output.getvalue()


def oversized_png():
    """Valid PNG header advertising >40M pixels, without allocating that image."""
    original = png((100, 50, 20))
    ihdr = struct.pack('!2I5B', 10000, 10000, 8, 2, 0, 0, 0)
    return (original[:8] + struct.pack('!I', len(ihdr)) + b'IHDR' + ihdr
            + struct.pack('!I', zlib.crc32(b'IHDR' + ihdr)) + original[33:])


class DisposableAppTest(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)

    def album(self, root, title='同名作品', images=None):
        folder = root / title
        folder.mkdir(parents=True)
        for index, content in enumerate(images or self.app.images, 1):
            (folder / f'{index}.png').write_bytes(content)
        return folder

    def books(self, library):
        return self.app.books(library['id'])['books']

    def library(self, library_id):
        return next(item for item in self.app.request('/api/libraries')[1]['libraries'] if item['id'] == library_id)

    def detail(self, book):
        return self.app.request('/api/books/' + book['id'])[1]

    def rescan(self, library):
        self.assertEqual(self.app.request('/api/libraries/' + library['id'] + '/scan', {})[0], 202)
        return self.books(library)

    def create_model(self, name):
        status, data, _ = self.app.request('/api/models', {'name': name})
        self.assertEqual(status, 201)
        return data['model']


class MultiPathTests(DisposableAppTest):
    def setUp(self):
        super().setUp()
        self.first = self.app.library / '目录甲'
        self.second = self.app.library / '目录乙'
        self.album(self.first)
        self.album(self.second, images=list(reversed(self.app.images)))

    def test_same_relative_names_are_isolated_and_paths_export_first_compatibility_path(self):
        library = self.app.create_library(paths=[str(self.first), str(self.second)])
        self.assertEqual(library['paths'], [str(self.first.resolve()), str(self.second.resolve())])
        self.assertEqual(library['path'], str(self.first.resolve()))
        books = self.books(library)
        self.assertEqual(len(books), 2)
        self.assertEqual({book['path'] for book in books}, {'同名作品'})
        self.assertEqual(len({book['id'] for book in books}), 2)
        by_root = {book['source_root']: book for book in books}
        for root, expected in ((self.first, self.app.images[0]), (self.second, self.app.images[2])):
            book = by_root[str(root.resolve())]
            route = '/api/books/' + book['id']
            self.assertEqual(self.app.request(route + '/pages/0')[1], expected)
            assert_cover(self, self.app.request(route + '/cover'), expected)
        first = by_root[str(self.first.resolve())]
        second = by_root[str(self.second.resolve())]
        route = '/api/books/' + first['id']
        self.assertEqual(self.app.request(route + '/progress', {'page': 1, 'completed': False})[0], 200)
        self.assertEqual(self.app.request(route + '/favorite', {'favorite': True})[0], 200)
        self.assertIsNone(self.detail(second)['progress'])
        self.assertFalse(self.detail(second)['favorite'])

    def test_append_reorder_remove_and_readd_preserve_identifiers_and_history(self):
        library = self.app.create_library(path=str(self.first))
        first = self.books(library)[0]
        route = '/api/books/' + first['id']
        library_route = '/api/libraries/' + library['id']
        self.app.request(route + '/progress', {'page': 1, 'completed': False})
        self.app.request(route + '/favorite', {'favorite': True})
        self.app.request(route + '/metadata', {'title': '保留编辑名', 'cover_page': 2})
        bookmark = self.app.request(route + '/bookmarks', {'page': 2, 'note': '保留位置'})[1]['bookmark']
        baseline = self.detail(first)
        self.assertEqual(self.app.request(library_route, {'paths': [str(self.first), str(self.second)]})[0], 200)
        appended = self.books(library)
        second = next(book for book in appended if book['id'] != first['id'])
        second_baseline = self.detail(second)
        for paths in ([self.second, self.first], [self.first]):
            self.assertEqual(self.app.request(library_route, {'paths': list(map(str, paths))})[0], 200)
            active = self.books(library)
            self.assertEqual(len(active), len(paths))
            current = self.detail(first)
            for field in ('id', 'title', 'progress', 'favorite', 'added_at', 'cover_page', 'source_root'):
                self.assertEqual(current[field], baseline[field], field)
            self.assertEqual(current['bookmarks'][0]['id'], bookmark['id'])
        self.assertEqual(self.app.request('/api/books/' + second['id'])[0], 404)
        self.assertEqual(self.app.request(library_route, {'paths': [str(self.second), str(self.first)]})[0], 200)
        self.books(library)
        restored = self.detail(second)
        self.assertEqual((restored['id'], restored['added_at']), (second_baseline['id'], second_baseline['added_at']))
        self.app.stop()
        self.app.start()
        self.assertEqual(self.library(library['id'])['paths'], [str(self.second.resolve()), str(self.first.resolve())])
        self.assertEqual(self.detail(first)['progress'], baseline['progress'])
        self.assertEqual(self.detail(second)['added_at'], second_baseline['added_at'])

    def test_all_roots_are_validated_atomically_before_create_or_edit(self):
        extra = [self.app.library / f'空目录{index}' for index in range(21)]
        for folder in extra:
            folder.mkdir()
        invalid = [None, '', [], [str(self.first), str(self.first)],
                   [str(self.first), str(self.first / '同名作品')],
                   [str(self.first / '同名作品'), str(self.first)],
                   [str(self.first), str(self.app.root)], [str(self.first), '.'],
                   [str(self.first), str(self.app.library / '..' / 'library')],
                   [str(self.first), str(self.app.library / 'missing')],
                   [str(self.first), 42], list(map(str, extra))]
        for paths in invalid:
            with self.subTest(stage='create', paths=paths):
                status, _, _ = self.app.request('/api/libraries', {'name': '不能创建', 'paths': paths})
                self.assertIn(status, (400, 403, 422))
        self.assertEqual(self.app.request('/api/libraries')[1]['libraries'], [])
        library = self.app.create_library(path=str(self.first))
        baseline = self.books(library)[0]
        for paths in invalid:
            with self.subTest(stage='edit', paths=paths):
                status, _, _ = self.app.request('/api/libraries/' + library['id'], {'name': '不能保存', 'paths': paths})
                self.assertIn(status, (400, 403, 422))
                current = self.library(library['id'])
                self.assertEqual((current['name'], current['paths']), ('测试书库', [str(self.first.resolve())]))
                self.assertEqual(self.detail(baseline)['id'], baseline['id'])

    def test_archive_and_folder_with_same_names_read_from_the_correct_root(self):
        for root, color in ((self.first, self.app.images[0]), (self.second, self.app.images[2])):
            with zipfile.ZipFile(root / '同名压缩包.cbz', 'w') as archive:
                archive.writestr('1.png', color)
        library = self.app.create_library(paths=list(map(str, (self.first, self.second))))
        books = self.books(library)
        self.assertEqual(len(books), 4)
        archives = [book for book in books if book['format'] == 'cbz']
        self.assertEqual(len(archives), 2)
        for book in archives:
            expected = self.app.images[0] if Path(book['source_root']) == self.first else self.app.images[2]
            route = '/api/books/' + book['id']
            self.assertEqual(self.app.request(route + '/pages/0')[1], expected)
            assert_cover(self, self.app.request(route + '/cover'), expected)

    def test_changing_selected_root_boundary_keeps_physical_book_and_progress(self):
        library = self.app.create_library(paths=[str(self.first), str(self.second)])
        first = next(book for book in self.books(library) if Path(book['source_root']) == self.first)
        route = '/api/books/' + first['id']
        self.app.request(route + '/progress', {'page': 1, 'completed': False})
        self.app.request(route + '/metadata', {'title': '保留单本资料'})
        baseline = self.detail(first)
        for selected_root in (self.first / '同名作品', self.first):
            self.assertEqual(self.app.request('/api/libraries/' + library['id'],
                                              {'paths': [str(selected_root), str(self.second)]})[0], 200)
            catalog = self.app.books(library['id'])
            self.assertEqual(len(catalog['books']), 2, (str(selected_root), catalog['errors']))
            current = self.detail(first)
            self.assertEqual(current['source_root'], str(selected_root.resolve()))
            for field in ('id', 'title', 'progress', 'added_at'):
                self.assertEqual(current[field], baseline[field], field)
            self.assertEqual(self.app.request(route + '/pages/0')[1], self.app.images[0])

    def test_legacy_relative_identifier_cannot_collide_with_another_roots_global_path(self):
        parent = self.app.library / '旧根'
        other_root = self.app.library / '新根'
        self.album(parent, '新根/作品')
        self.album(other_root, '作品', images=list(reversed(self.app.images)))
        library = self.app.create_library(path=str(parent))
        original = self.books(library)[0]
        # This is the actual released <=1.4 ID formula (relative to that
        # library's sole root). The new book's global path is also 新根/作品.
        legacy_id = hashlib.sha256((library['id'] + '\0folder\0新根/作品').encode()).hexdigest()[:24]
        self.app.stop()
        with closing(sqlite3.connect(self.app.data / 'library.sqlite3')) as db, db:
            db.execute('UPDATE books SET id=? WHERE id=?', (legacy_id, original['id']))
        self.app.start()
        self.app.books(library['id'])
        route = '/api/books/' + legacy_id
        self.assertEqual(self.app.request(route + '/progress', {'page': 1, 'completed': False})[0], 200)
        self.assertEqual(self.app.request('/api/libraries/' + library['id'],
                                          {'paths': [str(parent), str(other_root)]})[0], 200)
        books = self.books(library)
        self.assertEqual(len(books), 2)
        self.assertEqual(len({book['id'] for book in books}), 2)
        saved = self.app.request(route)[1]
        self.assertEqual(saved['progress']['page'], 1)
        self.assertEqual(saved['source_root'], str(parent.resolve()))
        self.assertEqual(self.app.request(route + '/pages/0')[1], self.app.images[0])
        added = next(book for book in books if book['id'] != legacy_id)
        self.assertIsNone(added['progress'])
        self.assertEqual(self.app.request('/api/books/' + added['id'] + '/pages/0')[1], self.app.images[2])


class DefaultAuthorTests(DisposableAppTest):
    def setUp(self):
        super().setUp()
        # Root images have no folder-derived model, so the selected default is used.
        (self.app.library / 'root.png').write_bytes(self.app.images[0])

    def test_single_selected_author_reused_renamed_and_cleared_across_restart(self):
        first, second = self.create_model('默认模特'), self.create_model('第二模特')
        library = self.app.create_library(media_type='photo', default_author_id=first['id'])
        root_book = next(book for book in self.books(library) if book['path'] == '.')
        self.assertEqual((library['default_author_id'], library['default_author']), (first['id'], first['name']))
        self.assertEqual([(model['id'], model['name']) for model in root_book['models']], [(first['id'], first['name'])])
        self.assertEqual(self.app.request('/api/models/' + first['id'], {'name': '改名后的模特'})[0], 200)
        self.assertEqual(self.library(library['id'])['default_author'], '改名后的模特')
        self.assertEqual(self.detail(root_book)['author'], '改名后的模特')
        route = '/api/libraries/' + library['id']
        self.assertEqual(self.app.request(route, {'default_author_id': second['id']})[0], 200)
        self.books(library)
        self.assertEqual([(model['id'], model['name']) for model in self.detail(root_book)['models']], [(second['id'], second['name'])])
        self.app.stop()
        self.app.start()
        self.assertEqual(self.library(library['id'])['default_author_id'], second['id'])
        self.assertEqual(self.app.request(route, {'default_author_id': None})[0], 200)
        self.books(library)
        current = self.library(library['id'])
        self.assertIsNone(current['default_author_id'])
        self.assertEqual(current['default_author'], '')
        self.assertEqual(self.detail(root_book)['models'], [])

    def test_invalid_author_id_is_atomic_and_does_not_create_registry_entries(self):
        model = self.create_model('已存在作者')
        library = self.app.create_library(default_author_id=model['id'])
        self.books(library)
        before_models = self.app.request('/api/models')[1]['models']
        for value in ([], [model['id']], {}, True, 42, '', 'unknown', '0' * 24):
            with self.subTest(value=value):
                status, _, _ = self.app.request('/api/libraries/' + library['id'],
                                                {'name': '不能保存', 'default_author_id': value})
                self.assertIn(status, (400, 404, 422))
                current = self.library(library['id'])
                self.assertEqual((current['name'], current['default_author_id']), ('测试书库', model['id']))
                self.assertEqual(self.app.request('/api/models')[1]['models'], before_models)

    def test_default_change_preserves_manual_model_selections_and_clears(self):
        first, second = self.create_model('默认一'), self.create_model('默认二')
        library = self.app.create_library(media_type='photo', default_author_id=first['id'])
        books = self.books(library)
        folder = next(book for book in books if book['path'] == '测试系列/第01话')
        root = next(book for book in books if book['path'] == '.')
        folder_route = '/api/books/' + folder['id']
        root_route = '/api/books/' + root['id']
        self.assertEqual(self.app.request(folder_route + '/metadata', {'model_ids': [first['id'], second['id']]})[0], 200)
        self.assertEqual(self.app.request(root_route + '/metadata', {'model_ids': []})[0], 200)
        self.assertEqual(self.app.request('/api/libraries/' + library['id'], {'default_author_id': second['id']})[0], 200)
        self.books(library)
        self.app.stop()
        self.app.start()
        self.rescan(library)
        self.assertEqual([model['id'] for model in self.detail(folder)['models']], [first['id'], second['id']])
        self.assertEqual(self.detail(root)['models'], [])


class CoverThumbnailTests(DisposableAppTest):
    def setUp(self):
        super().setUp()
        self.root = self.app.library / '封面测试'
        self.original = image_bytes((170, 30, 60))
        self.other = image_bytes((20, 150, 40), (1600, 1000))
        self.folder = self.album(self.root, images=[self.original, self.other, self.app.images[2]])
        self.library_record = self.app.create_library(path=str(self.root), media_type='photo')
        self.book = self.books(self.library_record)[0]
        self.route = '/api/books/' + self.book['id']

    def cached_files(self):
        # Authorized catalogue reads may have scheduled a library montage.
        # Snapshot persistent page covers only after that producer has settled.
        deadline = time.monotonic() + 10
        while self.library(self.library_record['id']).get('cover', {}).get('pending'):
            self.assertLess(time.monotonic(), deadline, 'Generated montage did not finish')
            time.sleep(.02)
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (self.app.data / 'covers').glob('*.jpg') if path.is_file()}

    def upload(self, content, **extra):
        return self.app.request(self.route + '/metadata', {
            'cover_source': 'upload', 'cover_upload': {'data': base64.b64encode(content).decode('ascii')}, **extra})

    def test_small_persistent_cache_and_original_page_bytes_survive_restart(self):
        response = self.app.request(self.route + '/cover')
        cover = assert_cover(self, response, self.original)
        self.assertLess(len(cover), len(self.original))
        with Image.open(BytesIO(cover)) as image:
            self.assertEqual(image.size, (400, 600))
        files = self.cached_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(next(iter(files.values()))[0], cover)
        self.assertEqual(self.app.request(self.route + '/pages/0')[1], self.original)
        self.assertEqual(self.app.request(self.route + '/cover')[1], cover)
        self.assertEqual(self.cached_files(), files)
        self.app.stop()
        self.app.start()
        self.assertEqual(self.app.request(self.route + '/cover')[1], cover)
        self.assertEqual(self.cached_files(), files)
        self.assertEqual(self.app.request(self.route + '/pages/0')[1], self.original)

    def test_page_selection_and_source_change_invalidate_only_the_generated_cache(self):
        baseline = self.detail(self.book)
        assert_cover(self, self.app.request(self.route + '/cover'), self.original)
        self.assertEqual(self.app.request(self.route + '/metadata', {'cover_source': 'page', 'cover_page': 1})[0], 200)
        selected = self.detail(self.book)
        self.assertEqual(selected['cover_source'], 'page')
        self.assertNotEqual(selected['cover_version'], baseline['cover_version'])
        assert_cover(self, self.app.request(self.route + '/cover'), self.other)
        before_cache = self.cached_files()
        changed = image_bytes((30, 50, 180), (1100, 1600))
        source_path = self.folder / '2.png'
        source_path.write_bytes(changed)
        os.utime(source_path, ns=(source_path.stat().st_atime_ns, source_path.stat().st_mtime_ns + 1_000_000_000))
        assert_cover(self, self.app.request(self.route + '/cover'), changed)
        self.assertNotEqual(self.cached_files(), before_cache)
        self.assertEqual(len(self.cached_files()), 1)
        self.rescan(self.library_record)
        self.assertNotEqual(self.detail(self.book)['cover_version'], selected['cover_version'])
        self.assertEqual(self.app.request(self.route + '/pages/1')[1], changed)
        self.assertEqual(self.app.request(self.route + '/pages/0')[1], self.original)

    def test_uploaded_cover_persists_through_scan_restart_and_can_reset_to_a_page(self):
        custom = image_bytes((70, 40, 190), (1800, 1200))
        status, result, _ = self.upload(custom)
        self.assertEqual(status, 200)
        uploaded = result['book']
        self.assertEqual(uploaded['cover_source'], 'upload')
        self.assertNotEqual(uploaded['cover_version'], self.book['cover_version'])
        generated = assert_cover(self, self.app.request(self.route + '/cover'), custom)
        files = self.cached_files()
        self.assertEqual(len(files), 1)
        self.assertIn('-upload-', next(iter(files)))
        self.rescan(self.library_record)
        self.app.stop()
        self.app.start()
        persisted = self.detail(self.book)
        self.assertEqual((persisted['cover_source'], persisted['cover_version']),
                         (uploaded['cover_source'], uploaded['cover_version']))
        self.assertEqual(self.app.request(self.route + '/cover')[1], generated)
        self.assertEqual(self.cached_files(), files)
        self.assertEqual(self.app.request(self.route + '/pages/0')[1], self.original)
        self.assertEqual(self.app.request(self.route + '/metadata', {'cover_source': 'page', 'cover_page': 2})[0], 200)
        self.assertEqual(self.detail(self.book)['cover_source'], 'page')
        self.assertNotEqual(self.detail(self.book)['cover_version'], uploaded['cover_version'])
        assert_cover(self, self.app.request(self.route + '/cover'), self.app.images[2])
        self.assertTrue(all('-upload-' not in name for name in self.cached_files()))

    def test_invalid_upload_and_metadata_are_atomic_with_no_orphan_files(self):
        custom = image_bytes((70, 40, 190), (400, 600))
        self.assertEqual(self.upload(custom)[0], 200)
        assert_cover(self, self.app.request(self.route + '/cover'), custom)
        before = self.detail(self.book)
        files = self.cached_files()
        invalid_payloads = [
            {'cover_source': 'other'}, {'cover_source': None}, {'cover_upload': None},
            {'cover_upload': 'invalid'}, {'cover_upload': {}}, {'cover_upload': {'data': 42}},
            {'cover_upload': {'data': '%%invalid%%'}}, {'cover_upload': {'data': ''}},
            {'cover_upload': {'data': base64.b64encode(b'fake image.php').decode()}},
            {'cover_upload': {'data': base64.b64encode(oversized_png()).decode()}},
            {'cover_source': 'page', 'cover_upload': {'data': base64.b64encode(custom).decode()}},
            {'cover_upload': {'data': base64.b64encode(custom).decode()}, 'cover_page': 999},
            {'cover_upload': {'data': base64.b64encode(custom).decode()}, 'model_ids': ['0' * 24]},
        ]
        for change in invalid_payloads:
            with self.subTest(fields=tuple(change), data_type=type(change.get('cover_upload')).__name__):
                status, _, _ = self.app.request(self.route + '/metadata', {'title': '不能保存', **change})
                self.assertIn(status, (400, 413, 422))
                current = self.detail(self.book)
                for field in ('title', 'cover_source', 'cover_version', 'cover_page', 'models'):
                    self.assertEqual(current[field], before[field], field)
                self.assertEqual(self.cached_files(), files)

    def test_upload_size_limit_and_missing_upload_are_rejected_without_changes(self):
        before = self.detail(self.book)
        files = self.cached_files()
        self.assertIn(self.app.request(self.route + '/metadata', {'cover_source': 'upload'})[0], (400, 422))
        oversize = base64.b64encode(b'a' * (6 * 1024 * 1024 + 1)).decode()
        status, _, _ = self.app.request(self.route + '/metadata',
                                        {'title': '不能保存', 'cover_upload': {'data': oversize}})
        self.assertEqual(status, 413)
        self.assertEqual(self.detail(self.book)['title'], before['title'])
        self.assertEqual(self.detail(self.book)['cover_version'], before['cover_version'])
        self.assertEqual(self.cached_files(), files)

    def test_transparency_and_exif_orientation_are_applied_to_uploaded_thumbnail(self):
        rgba = BytesIO()
        Image.new('RGBA', (100, 150), (255, 0, 0, 0)).save(rgba, 'PNG')
        self.assertEqual(self.upload(rgba.getvalue())[0], 200)
        white = assert_cover(self, self.app.request(self.route + '/cover'))
        with Image.open(BytesIO(white)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        exif = Image.Exif()
        exif[274] = 6
        oriented = image_bytes((30, 120, 60), (900, 600), 'JPEG', exif=exif)
        self.assertEqual(self.upload(oriented)[0], 200)
        response = assert_cover(self, self.app.request(self.route + '/cover'))
        with Image.open(BytesIO(response)) as image:
            self.assertEqual(image.size, (400, 600))
            self.assertNotIn(274, image.getexif())

    def test_cover_upload_and_multi_path_author_changes_require_auth_and_same_origin(self):
        baseline = self.cached_files()
        self.assertEqual(self.app.request('/api/logout', {})[0], 200)
        body = {'cover_upload': {'data': base64.b64encode(self.original).decode()}}
        library_body = {'paths': [str(self.root)], 'default_author_id': None}
        self.assertEqual(self.app.request(self.route + '/cover')[0], 401)
        self.assertEqual(self.app.request(self.route + '/metadata', body)[0], 401)
        self.assertEqual(self.app.request('/api/libraries/' + self.library_record['id'], library_body)[0], 401)
        self.assertEqual(self.app.request('/api/login', {'username': 'test-reader', 'password': 'test-only-strong-passphrase'})[0], 200)
        for path, payload in ((self.route + '/metadata', body),
                              ('/api/libraries/' + self.library_record['id'], library_body)):
            self.assertEqual(self.app.request(path, payload, {'Origin': 'https://example.invalid'})[0], 403)
        self.assertEqual(self.detail(self.book)['cover_source'], 'page')
        self.assertEqual(self.cached_files(), baseline)


class ReadingAndMigrationTests(DisposableAppTest):
    def test_webtoon_effective_direction_is_vertical_and_paged_override_still_works(self):
        library = self.app.create_library(reading_mode='webtoon', reading_direction='rtl')
        book = self.books(library)[0]
        self.assertEqual(book['reading_direction'], 'ttb')
        route = '/api/books/' + book['id']
        self.assertEqual(self.app.request(route + '/metadata', {'reading_direction': 'ltr'})[0], 200)
        self.assertEqual(self.detail(book)['reading_direction'], 'ttb')
        self.assertEqual(self.app.request(route + '/metadata', {'reading_mode': 'double', 'reading_direction': 'rtl'})[0], 200)
        self.assertEqual(self.detail(book)['reading_direction'], 'rtl')
        self.assertEqual(self.app.request(route + '/metadata', {'reading_mode': None, 'reading_direction': None})[0], 200)
        self.assertEqual(self.detail(book)['reading_direction'], 'ttb')
        self.app.stop()
        self.app.start()
        self.assertEqual(self.detail(book)['reading_direction'], 'ttb')

    def test_real_v14_migration_preserves_source_ids_metadata_and_default_author_registry(self):
        app = RunningApp(legacy='v1.4')
        self.addCleanup(app.close)
        library = app.request('/api/libraries')[1]['libraries'][0]
        self.assertEqual(library['paths'], [str(app.library.resolve())])
        self.assertEqual(library['path'], str(app.library.resolve()))
        self.assertTrue(library['hide_date_prefix'])
        self.assertEqual(library['default_author'], '原作者')
        author = next(model for model in app.request('/api/models')[1]['models'] if model['name'] == '原作者')
        self.assertEqual(library['default_author_id'], author['id'])
        # Automatic works now inherit the selected default; the legacy work's
        # manual model binding below must remain unchanged.
        self.assertEqual(author['book_count'], 2)
        book = app.request('/api/books/' + app.legacy_id)[1]
        self.assertEqual(book['source_root'], str(app.library.resolve()))
        self.assertEqual((book['id'], book['title'], book['release_date']),
                         (app.legacy_id, '原有自定义标题', '2020-01-02'))
        self.assertEqual((book['reading_mode_override'], book['reading_direction_override']), ('double', 'ltr'))
        self.assertEqual(book['cover_source'], 'page')
        self.assertEqual(book['cover_page'], 2)
        self.assertEqual(book['progress']['page'], 1)
        self.assertTrue(book['favorite'])
        self.assertEqual([model['id'] for model in book['models']], [app.legacy_model_id])
        assert_cover(self, app.request('/api/books/' + book['id'] + '/cover'), app.images[2])
        app.request('/api/libraries/' + library['id'] + '/scan', {})
        app.books(library['id'])
        app.stop()
        app.start()
        persisted = app.request('/api/books/' + app.legacy_id)[1]
        for field in ('id', 'title', 'release_date', 'reading_mode_override', 'reading_direction_override',
                      'cover_page', 'added_at', 'progress', 'favorite', 'models', 'source_root'):
            self.assertEqual(persisted[field], book[field], field)
        self.assertTrue(persisted['cover_version'])
        self.assertEqual(app.request('/api/libraries')[1]['libraries'][0]['default_author_id'], author['id'])
        # Migration records come from an old scan signature; a rescan can change
        # the version, but cannot change the chosen cover or user's saved state.
        assert_cover(self, app.request('/api/books/' + book['id'] + '/cover'), app.images[2])
        with closing(sqlite3.connect(app.data / 'library.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()

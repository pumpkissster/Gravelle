"""Real catalogue invalidation, bounded list reads and account-scoped status."""
from contextlib import closing
import http.client
import json
import threading
from unittest.mock import patch

from image_search import ALGORITHM_VERSION, LOCAL_VERSION
from server import COOKIE_NAME, Library, ReaderError, ReaderServer
from test_image_search import ImageSearchFixture


class CatalogPerformanceTests(ImageSearchFixture):
    def sql(self, statement, parameters=()):
        with closing(self.app.connect()) as connection, connection:
            connection.execute(statement, parameters)

    def token(self, user=1):
        with self.app.acting_as(user):
            return self.app.catalog_status()['revision']

    def assert_mutation_invalidates(self, statement, parameters=()):
        before = self.token()
        self.sql(statement, parameters)
        self.assertNotEqual(self.token(), before, statement)

    def private_library(self):
        folder = self.media / 'private' / 'private-album'
        folder.mkdir(parents=True)
        (folder / '1.png').write_bytes(self.source)
        identifier = self.create_library(folder.parent, 'Private fixture')
        return identifier, self.app.listing(identifier)['books'][0]['id']

    def seed_index(self, book_id=None, status='ready', local_state='ready'):
        book_id = book_id or self.book_id
        with closing(self.app.connect()) as connection, connection:
            row = connection.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            page = json.loads(row['pages'])[0]
            connection.execute('''INSERT OR REPLACE INTO is_pages
                (book_id,page_key,page_index,source_version,book_signature,algorithm,
                 status,local_state,local_version,updated_at)
                VALUES(?,?,0,'generated',?,?,?,?,?,1)''',
                (book_id, page, row['signature'], ALGORITHM_VERSION, status, local_state, LOCAL_VERSION))
            connection.execute('UPDATE is_settings SET enabled=1,paused=0,reconcile_pending=0')

    def test_revision_stable_for_reads_and_maintenance_only_writes(self):
        self.seed_index()
        before = self.token()
        self.assertEqual(self.token(), before)
        self.sql('UPDATE media_libraries SET last_scan=COALESCE(last_scan,0)+100 WHERE id=?', (self.library_id,))
        self.sql('UPDATE users SET updated_at=updated_at+100 WHERE id=1')
        self.sql('UPDATE is_pages SET updated_at=updated_at+1,local_state=?', ('pending',))
        self.sql('UPDATE library_cover_cache SET updated_at=updated_at+1')
        self.sql('UPDATE tc_books SET updated_at=updated_at+1,generation=generation+1')
        self.assertEqual(self.token(), before)
        self.sql('UPDATE books SET title=title WHERE id=?', (self.book_id,))
        self.assertEqual(self.token(), before, 'An unchanged scan upsert must not invalidate the catalogue')

    def test_restart_restores_revision_triggers_and_changes_instance_token(self):
        previous = self.token()
        with closing(self.app.connect()) as connection, connection:
            revision = connection.execute('SELECT value FROM catalog_revision WHERE id=1').fetchone()[0]
            connection.execute('DROP TRIGGER catalog_revision_books_update')
        self.app.close()
        self.app = Library(self.media, self.data, '', 0)
        self.manager = self.app.image_search
        self.manager.auto_start = self.app.transcoding.auto_start = False
        with closing(self.app.connect()) as connection:
            restored = connection.execute('''SELECT 1 FROM sqlite_master
                WHERE type='trigger' AND name='catalog_revision_books_update' ''').fetchone()
            self.assertIsNotNone(restored)
            self.assertGreaterEqual(connection.execute('SELECT value FROM catalog_revision WHERE id=1').fetchone()[0], revision)
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertNotEqual(self.token(), previous)
        self.assert_mutation_invalidates('UPDATE books SET title=title||? WHERE id=?', (' after restart', self.book_id))

    def test_visible_book_cover_and_personal_mutations_change_revision(self):
        self.assert_mutation_invalidates('UPDATE books SET title=title||? WHERE id=?', (' changed', self.book_id))
        self.assert_mutation_invalidates('UPDATE book_metadata SET cover_page=1 WHERE book_id=?', (self.book_id,))
        self.assert_mutation_invalidates('''INSERT INTO progress(user_id,book_id,page,completed,updated_at,position)
            VALUES(1,?,1,0,1,0)''', (self.book_id,))
        self.assert_mutation_invalidates('UPDATE progress SET updated_at=2 WHERE user_id=1 AND book_id=?', (self.book_id,))
        self.assert_mutation_invalidates('INSERT INTO favorites(user_id,book_id,favorite) VALUES(1,?,1)', (self.book_id,))
        self.assert_mutation_invalidates('''INSERT INTO reading_preferences(user_id,book_id,reading_mode)
            VALUES(1,?,'double')''', (self.book_id,))
        self.assert_mutation_invalidates('''INSERT INTO bookmarks(id,user_id,book_id,page,note,created_at,position)
            VALUES('generated-bookmark',1,?,0,'generated',1,0)''', (self.book_id,))
        self.assert_mutation_invalidates('UPDATE tc_books SET published=0 WHERE book_id=?', (self.book_id,))
        self.assert_mutation_invalidates('UPDATE tc_settings SET enabled=1 WHERE id=1')
        self.assert_mutation_invalidates('UPDATE library_cover_cache SET generation=generation+1 WHERE library_id=?', (self.library_id,))

    def test_account_and_visibility_changes_are_never_cached_across_users(self):
        child = self.child([self.library_id])
        self.assertNotEqual(self.token(), self.token(child))
        before = self.token(child)
        self.sql('INSERT INTO user_hidden_libraries(user_id,library_id) VALUES(?,?)', (child, self.library_id))
        self.assertNotEqual(self.token(child), before)
        before = self.token(child)
        self.sql('DELETE FROM user_hidden_libraries WHERE user_id=?', (child,))
        self.assertNotEqual(self.token(child), before)
        before = self.token(child)
        self.sql('DELETE FROM user_libraries WHERE user_id=?', (child,))
        self.assertNotEqual(self.token(child), before)
        before = self.token(child)
        self.sql('UPDATE users SET display_name=? WHERE id=?', ('Renamed child', child))
        self.assertNotEqual(self.token(child), before)
        self.sql('UPDATE users SET enabled=0 WHERE id=?', (child,))
        with self.app.acting_as(child), self.assertRaises(ReaderError) as caught:
            self.app.catalog_status()
        self.assertEqual(caught.exception.status, 401)

    def test_scan_flags_only_describe_visible_libraries_and_change_token_without_sql(self):
        private, _ = self.private_library()
        child = self.child([self.library_id])
        before = self.token(child)
        try:
            with self.app.state_lock:
                self.app.scanning = True
                self.app.current_scan = private
            with self.app.acting_as(child):
                hidden = self.app.catalog_status()
            self.assertFalse(hidden['scanning'])
            self.assertEqual(hidden['revision'], before)
            with self.app.state_lock:
                self.app.current_scan = self.library_id
            with self.app.acting_as(child):
                visible = self.app.catalog_status()
            self.assertTrue(visible['scanning'])
            self.assertNotEqual(visible['revision'], before)
        finally:
            with self.app.state_lock:
                self.app.scanning = False
                self.app.current_scan = None
        self.assertEqual(self.token(child), before)

    def test_light_status_never_builds_covers_or_reads_full_catalogue(self):
        statements = []
        connect = self.app.connect
        def traced():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(self.app, 'connect', side_effect=traced), \
                patch.object(self.app, 'listing', side_effect=AssertionError('Loaded directory')), \
                patch.object(self.app.library_covers, '_plan', side_effect=AssertionError('Planned a cover')):
            result = self.app.catalog_status()
        self.assertEqual(set(result), {'revision', 'scanning', 'cover_pending'})
        self.assertEqual(len(result['revision']), 64)
        self.assertFalse(any('FROM books' in sql or 'JOIN books' in sql for sql in statements))
        self.assertLessEqual(len([sql for sql in statements if sql.lstrip().startswith('SELECT')]), 4)

    def test_cover_retry_deadline_changes_status_even_without_database_write(self):
        self.sql('''UPDATE library_cover_cache SET ready_signature='',cache_file='',deferred=0,retry_after=200
            WHERE library_id=?''', (self.library_id,))
        with patch('server.time.time', return_value=199):
            before = self.app.catalog_status()
        with patch('server.time.time', return_value=201):
            after = self.app.catalog_status()
        self.assertFalse(before['cover_pending'])
        self.assertTrue(after['cover_pending'])
        self.assertNotEqual(before['revision'], after['revision'])

    def test_list_projection_omits_page_payload_but_internal_book_retains_it(self):
        with closing(self.app.connect()) as connection:
            rows = self.app._select(connection, visible=True)
            self.assertEqual(len(rows), 1)
            self.assertNotIn('pages', rows[0].keys())
            self.assertNotIn('pdf_sizes', rows[0].keys())
        book = self.app.book(self.book_id, internal=True)
        self.assertEqual(len(json.loads(book['pages'])), 2)
        self.assertIn('pdf_sizes', book)
        self.assertEqual(self.app.listing()['books'][0]['page_count'], 2)

    def seed_same_model_books(self, amount=40):
        model = 'e' * 24
        with closing(self.app.connect()) as connection, connection:
            connection.execute('INSERT INTO models(id,name,normalized_name) VALUES(?,?,?)', (model, 'Generated model', 'generated model'))
            template = dict(connection.execute('SELECT * FROM books WHERE id=?', (self.book_id,)).fetchone())
            ids = []
            for index in range(amount):
                identifier = f'{index + 1:024x}'
                ids.append(identifier)
                book = {**template, 'id': identifier, 'title': f'Generated {index}', 'path': f'generated-{index}'}
                fields = list(book)
                connection.execute('INSERT INTO books(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')',
                                   [book[key] for key in fields])
                connection.execute('INSERT INTO book_models(book_id,model_id,position) VALUES(?,?,0)', (identifier, model))
        return model, ids

    def test_model_fallback_is_aggregated_once_per_distinct_model(self):
        _, identifiers = self.seed_same_model_books()
        statements = []
        with closing(self.app.connect()) as connection:
            connection.set_trace_callback(statements.append)
            values = self.app._models_for_books(connection, identifiers)
        fallback = [sql for sql in statements if 'MIN(other.id)' in sql]
        self.assertEqual(len(fallback), 1)
        self.assertIn('GROUP BY obm.model_id', fallback[0])
        self.assertNotIn('SELECT MIN(other.id)', fallback[0])
        self.assertTrue(all(item[0]['avatar_url'] == f'/api/books/{min(identifiers)}/cover' for item in values.values()))

    def test_fallback_avatar_obeys_hidden_and_revoked_library_access(self):
        private, private_book = self.private_library()
        model = 'd' * 24
        with closing(self.app.connect()) as connection, connection:
            connection.execute('INSERT INTO models(id,name,normalized_name) VALUES(?,?,?)', (model, 'Shared model', 'shared model'))
            for book in (self.book_id, private_book):
                connection.execute('INSERT INTO book_models(book_id,model_id,position) VALUES(?,?,9)', (book, model))
        child = self.child([self.library_id, private])
        self.sql('INSERT INTO user_hidden_libraries(user_id,library_id) VALUES(?,?)', (child, private))
        with self.app.acting_as(child), closing(self.app.connect()) as connection:
            values = self.app._models_for_books(connection, [self.book_id])
        exported = next(value for value in values[self.book_id] if value['id'] == model)
        self.assertEqual(exported['avatar_url'], f'/api/books/{self.book_id}/cover')
        self.sql('DELETE FROM user_hidden_libraries WHERE user_id=?', (child,))
        self.sql('DELETE FROM user_libraries WHERE user_id=? AND library_id=?', (child, private))
        with self.app.acting_as(child), closing(self.app.connect()) as connection:
            values = self.app._models_for_books(connection, [self.book_id])
        self.assertEqual(next(value for value in values[self.book_id] if value['id'] == model)['avatar_url'],
                         f'/api/books/{self.book_id}/cover')

    def test_child_image_status_uses_existence_checks_and_respects_visibility(self):
        private, private_book = self.private_library()
        self.seed_index(private_book)
        child = self.child([self.library_id])
        statements = []
        connect = self.app.connect
        def traced():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection
        with self.app.acting_as(child), patch.object(self.app, 'connect', side_effect=traced):
            result = self.app.image_search.snapshot()
        self.assertEqual(result, {'enabled': True, 'available': False, 'indexing': False})
        self.assertFalse(any('COUNT(' in sql.upper() for sql in statements))
        self.seed_index(status='ready', local_state='pending')
        with self.app.acting_as(child):
            self.assertEqual(self.app.image_search.snapshot(), {'enabled': True, 'available': True, 'indexing': True})
        self.sql('UPDATE is_settings SET paused=1')
        with self.app.acting_as(child):
            self.assertEqual(self.app.image_search.snapshot(), {'enabled': True, 'available': True, 'indexing': False})

    def test_catalogue_api_authentication_and_account_tokens(self):
        child = self.child([self.library_id])
        server = ReaderServer(('127.0.0.1', 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cookies = {name: COOKIE_NAME + '=' + self.app.make_session(user) for name, user in [('admin', 1), ('child', child)]}
        def request(actor=None, path='/api/catalog/status'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('GET', path, headers={'Cookie': cookies[actor]} if actor else {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            self.assertEqual(request()[0], 401)
            admin_status, admin = request('admin')
            child_status, restricted = request('child')
            self.assertEqual((admin_status, child_status), (200, 200))
            self.assertNotEqual(admin['revision'], restricted['revision'])
            self.assertEqual(request('child', '/api/catalog/status?library_id=private')[0], 400)
            self.sql('UPDATE users SET enabled=0 WHERE id=?', (child,))
            self.assertEqual(request('child')[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

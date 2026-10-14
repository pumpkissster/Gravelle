"""Independent HTTP authorization checks using disposable libraries and accounts."""
from contextlib import contextmanager
import http.cookiejar
import json
import unittest
import urllib.request

from test_integration import RunningApp


class MultiuserSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)
        self.admin_client = self.app.client
        self.admin_password = 'test-only-strong-passphrase'
        self.visible_path = self.app.library / 'allowed'
        self.hidden_path = self.app.library / 'private'
        for path in (self.visible_path, self.hidden_path):
            (path / 'album').mkdir(parents=True)
            for index, image in enumerate(self.app.images):
                (path / 'album' / f'{index}.png').write_bytes(image)
        self.visible = self.app.create_library(name='allowed-library', path=str(self.visible_path),
                                               media_type='photo', default_author='VisibleModel')
        self.hidden = self.app.create_library(name='private-library', path=str(self.hidden_path),
                                              media_type='photo', default_author='PrivateModel')
        self.visible_book = self.app.books(self.visible['id'])['books'][0]
        self.hidden_book = self.app.books(self.hidden['id'])['books'][0]
        self.password = 'test-only-child-passphrase'
        self.child = self.create_user('child-reader', [self.visible['id']])
        self.child_client = self.new_client()
        self.login_child()

    def new_client(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    @contextmanager
    def as_client(self, client):
        previous = self.app.client
        self.app.client = client
        try:
            yield
        finally:
            self.app.client = previous

    def child_request(self, path, data=None, headers=None, method=None):
        with self.as_client(self.child_client):
            return self.app.request(path, data, headers, method)

    def create_user(self, username, libraries):
        status, payload, _ = self.app.request('/api/users', {
            'username': username, 'password': self.password, 'enabled': True,
            'library_ids': libraries})
        self.assertEqual(status, 201, payload)
        return payload['user']

    def login_child(self):
        status, payload, _ = self.child_request('/api/login', {
            'username': self.child['username'], 'password': self.password})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['user']['role'], 'user')

    def test_private_library_models_and_files_are_not_disclosed(self):
        for path in ('/api/libraries', '/api/library', '/api/models', '/api/bookmarks'):
            status, payload, _ = self.child_request(path)
            self.assertEqual(status, 200, payload)
            serialized = json.dumps(payload)
            for private in (self.hidden['id'], self.hidden_book['id'], 'PrivateModel',
                            self.hidden['name'], str(self.hidden_path)):
                self.assertNotIn(private, serialized)
        self.assertEqual(self.child_request('/api/library?library_id=' + self.hidden['id'])[0], 404)
        self.assertEqual(self.child_request('/api/library?library_id=' + self.visible['id'])[0], 200)
        for suffix in ('', '/cover', '/pages/0'):
            path = '/api/books/' + self.hidden_book['id'] + suffix
            self.assertEqual(self.child_request(path)[0], 404)
            self.assertEqual(self.child_request(path, headers={'If-None-Match': '*'}, method='HEAD')[0], 404)
        private_model = self.hidden_book['models'][0]['id']
        self.assertEqual(self.child_request('/api/avatars/models/' + private_model)[0], 404)

    def test_privileged_routes_reject_read_only_account(self):
        for path in ('/api/users', '/api/directories', '/api/access', '/api/updates'):
            self.assertEqual(self.child_request(path)[0], 403, path)
        for path, payload in (
            ('/api/users', {'username': 'forged-user', 'password': self.password}),
            ('/api/libraries', {'name': 'forged', 'path': str(self.hidden_path)}),
            ('/api/libraries/' + self.visible['id'], {'name': 'forged'}),
            ('/api/libraries/' + self.visible['id'] + '/remove', {}),
            ('/api/scan', {'library_id': self.visible['id']}),
            ('/api/books/' + self.visible_book['id'] + '/metadata', {'title': 'forged'}),
            ('/api/books/' + self.visible_book['id'] + '/remove', {}),
            ('/api/models', {'name': 'forged-model'}),
        ):
            self.assertEqual(self.child_request(path, payload)[0], 403, path)
        # Profile payloads cannot elevate a role or grant permissions.
        status, _, _ = self.child_request('/api/profile', {'username': self.child['username'],
            'role': 'admin', 'permissions': {'scan_libraries': True}, 'library_ids': [self.hidden['id']]})
        self.assertIn(status, (200, 400, 403))
        self.assertEqual(self.child_request('/api/users')[0], 403)
        self.assertEqual(self.child_request('/api/scan', {'library_id': self.visible['id']})[0], 403)

    def test_personal_history_favorites_bookmarks_and_preferences_are_isolated(self):
        route = '/api/books/' + self.visible_book['id']
        self.assertEqual(self.app.request(route + '/progress', {'page': 1, 'completed': False})[0], 200)
        self.assertEqual(self.app.request(route + '/favorite', {'favorite': True})[0], 200)
        _, saved, _ = self.app.request(route + '/bookmarks', {'page': 1, 'note': 'admin-private'})
        admin_bookmark = saved['bookmark']['id']
        child_book = self.child_request(route)[1]
        self.assertIsNone(child_book['progress'])
        self.assertFalse(child_book['favorite'])
        self.assertEqual(child_book['bookmarks'], [])
        self.assertEqual(self.child_request('/api/bookmarks/' + admin_bookmark + '/delete', {})[0], 404)
        self.assertEqual(self.child_request(route + '/progress', {'page': 2, 'completed': False, 'user_id': 1})[0], 200)
        self.assertEqual(self.child_request(route + '/favorite', {'favorite': False, 'user_id': 1})[0], 200)
        self.assertIn(self.child_request(route + '/bookmarks', {'page': 1, 'note': 'child-private'})[0], (200, 201))
        self.assertEqual(self.child_request(route + '/reading-preferences', {
            'reading_mode': 'double', 'reading_direction': 'rtl'})[0], 200)
        admin_book = self.app.request(route)[1]
        self.assertEqual(admin_book['progress']['page'], 1)
        self.assertTrue(admin_book['favorite'])
        self.assertEqual([item['note'] for item in admin_book['bookmarks']], ['admin-private'])
        self.assertEqual(admin_book['reading_mode'], 'single')
        self.assertEqual(admin_book['reading_direction'], 'ltr')
        child_book = self.child_request(route)[1]
        self.assertEqual(child_book['progress']['page'], 2)
        self.assertEqual(child_book['reading_mode'], 'double')
        self.assertEqual(child_book['reading_direction'], 'rtl')

    def test_vertical_single_page_preferences_are_private_and_do_not_grant_management(self):
        route = '/api/books/' + self.visible_book['id']
        preferences = route + '/reading-preferences'
        status, result, _ = self.child_request(preferences, {'reading_mode': 'single', 'reading_direction': 'ttb'})
        self.assertEqual(status, 200, result)
        self.assertEqual((result['book']['reading_mode'], result['book']['reading_direction']), ('single', 'ttb'))
        self.assertIsNone(result['book']['reading_direction_override'])
        self.assertEqual(self.app.request(route)[1]['reading_direction'], 'ltr')
        second = self.create_user('second-reader', [self.visible['id']])
        with self.as_client(self.new_client()):
            self.assertEqual(self.app.request('/api/login', {'username': second['username'], 'password': self.password})[0], 200)
            book = self.app.request(route)[1]
            self.assertEqual(book['reading_direction'], 'ltr')
            self.assertEqual(book['reading_preferences'], {'reading_mode': None, 'reading_direction': None})
        for mode, effective in (('double', 'ltr'), ('webtoon', 'ttb'), ('single', 'ttb')):
            status, result, _ = self.child_request(preferences, {'reading_mode': mode})
            self.assertEqual(status, 200, result)
            self.assertEqual(result['book']['reading_direction'], effective)
            self.assertEqual(result['book']['reading_preferences']['reading_direction'], 'ttb')
        self.app.stop()
        self.app.start()
        self.assertEqual(self.child_request(route)[1]['reading_direction'], 'ttb')
        self.assertEqual(self.child_request('/api/books/' + self.hidden_book['id'] + '/reading-preferences',
                                            {'reading_mode': 'single', 'reading_direction': 'ttb'})[0], 404)
        self.assertEqual(self.child_request(route + '/metadata', {'reading_direction': 'ttb'})[0], 403)
        self.assertEqual(self.child_request('/api/libraries/' + self.visible['id'], {'reading_direction': 'ttb'})[0], 403)
        status, result, _ = self.child_request(preferences, {'reading_mode': None, 'reading_direction': None})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['book']['reading_direction'], 'ltr')

    def test_read_only_account_cannot_gain_management_permissions_or_submit_batches(self):
        self.assertIn(self.app.request('/api/users/' + str(self.child['id']), {
            'permissions': {'edit_metadata': True, 'remove_books': True, 'scan_libraries': True}})[0], (400, 403))
        route = '/api/books/' + self.visible_book['id']
        self.assertEqual(self.child_request(route + '/metadata', {'title': 'forged-edit'})[0], 403)
        self.assertEqual(self.child_request('/api/libraries/' + self.visible['id'] + '/scan', {})[0], 403)
        for action, extra in (('remove', {}), ('favorite', {'favorite': True})):
            status, _, _ = self.child_request('/api/books/batch', {'action': action,
                'ids': [self.visible_book['id'], self.hidden_book['id']], **extra})
            self.assertIn(status, (403, 404))
            self.assertEqual(self.app.request(route)[0], 200)
        self.assertIn(self.child_request('/api/books/' + self.hidden_book['id'] + '/metadata', {'title': 'forged'})[0], (403, 404))

    def test_personal_library_visibility_is_reversible_and_does_not_change_authorization(self):
        status, payload, _ = self.child_request('/api/my-libraries')
        self.assertEqual(status, 200, payload)
        self.assertEqual([(item['id'], item['visible']) for item in payload['libraries']], [(self.visible['id'], True)])
        self.assertEqual(self.child_request('/api/my-libraries', {'visible_library_ids': []})[0], 200)
        self.assertEqual(self.child_request('/api/libraries')[1]['libraries'], [])
        self.assertEqual(self.child_request('/api/library')[1]['books'], [])
        self.assertEqual(self.child_request('/api/models')[1]['models'], [])
        self.assertEqual(self.child_request('/api/bookmarks')[1]['bookmarks'], [])
        # Display preferences are not a new access grant or a permission revocation.
        self.assertEqual(self.child_request('/api/books/' + self.visible_book['id'])[0], 200)
        self.assertEqual(self.child_request('/api/books/' + self.hidden_book['id'])[0], 404)
        self.assertEqual(len(self.app.request('/api/libraries')[1]['libraries']), 2)
        self.assertFalse(self.child_request('/api/my-libraries')[1]['libraries'][0]['visible'])
        self.assertEqual(self.child_request('/api/my-libraries', {'visible_library_ids': [self.visible['id']]})[0], 200)
        self.assertEqual(len(self.child_request('/api/library')[1]['books']), 1)

    def test_hidden_authorized_library_exposes_raw_direction_and_library_mode_for_direct_reading(self):
        route = '/api/books/' + self.visible_book['id']
        preferences = route + '/reading-preferences'
        self.assertEqual(self.app.request('/api/libraries/' + self.visible['id'], {
            'reading_mode': 'single', 'reading_direction': 'ttb'})[0], 200)
        self.assertEqual(self.child_request('/api/my-libraries', {'visible_library_ids': []})[0], 200)
        self.assertEqual(self.child_request('/api/libraries')[1]['libraries'], [])
        status, result, _ = self.child_request(preferences, {'reading_mode': 'double', 'reading_direction': None})
        self.assertEqual(status, 200, result)
        book = self.child_request(route)[1]
        self.assertEqual(book['reading_direction'], 'ltr')
        self.assertEqual(book['reading_direction_stored'], 'ttb')
        self.assertEqual(book['reading_preferences'], {'reading_mode': 'double', 'reading_direction': None})
        self.assertEqual(book['library_reading_mode'], 'single')
        status, result, _ = self.child_request(preferences, {'reading_mode': 'single'})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['book']['reading_direction'], 'ttb')
        self.assertEqual(result['book']['reading_direction_stored'], 'ttb')
        status, result, _ = self.child_request(preferences, {'reading_mode': 'webtoon', 'reading_direction': 'rtl'})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['book']['reading_mode'], 'webtoon')
        self.assertEqual(result['book']['library_reading_mode'], 'single')
        self.assertEqual(result['book']['reading_direction'], 'ttb')
        self.assertEqual(result['book']['reading_direction_stored'], 'rtl')
        self.assertEqual(self.child_request('/api/books/' + self.hidden_book['id'])[0], 404)

    def test_personal_visibility_rejects_unauthorized_ids_without_partial_changes(self):
        status, _, _ = self.child_request('/api/my-libraries', {
            'visible_library_ids': [self.visible['id'], self.hidden['id']]})
        self.assertIn(status, (400, 403, 404))
        self.assertTrue(self.child_request('/api/my-libraries')[1]['libraries'][0]['visible'])
        self.assertEqual(self.child_request('/api/my-libraries', {'visible_library_ids': 'all'})[0], 400)
        self.assertEqual(self.child_request('/api/library?library_id=' + self.hidden['id'])[0], 404)

    def test_admin_visibility_is_personal_and_user_assignments_keep_all_libraries(self):
        self.assertEqual(self.app.request('/api/my-libraries', {'visible_library_ids': []})[0], 200)
        for path, field in (('/api/libraries', 'libraries'), ('/api/library', 'books'),
                            ('/api/models', 'models')):
            self.assertEqual(self.app.request(path)[1][field], [], path)
        visibility = self.app.request('/api/my-libraries')[1]['libraries']
        self.assertEqual(len(visibility), 2)
        self.assertTrue(all(not library['visible'] for library in visibility))
        self.assertEqual(len(self.app.request('/api/users')[1]['libraries']), 2)
        self.assertEqual(self.app.request('/api/books/' + self.visible_book['id'])[0], 200)
        self.assertEqual(len(self.child_request('/api/libraries')[1]['libraries']), 1)
        self.assertEqual(self.app.request('/api/my-libraries', {
            'visible_library_ids': [self.visible['id'], self.hidden['id']]})[0], 200)
        self.assertEqual(len(self.app.request('/api/libraries')[1]['libraries']), 2)

    def test_disable_delete_and_password_reset_revoke_only_target_account(self):
        user_route = '/api/users/' + str(self.child['id'])
        status, payload, _ = self.app.request(user_route, {'enabled': False})
        self.assertEqual(status, 200, payload)
        self.assertFalse(self.child_request('/api/session')[1]['authenticated'])
        self.assertEqual(self.child_request('/api/library')[0], 401)
        self.assertIn(self.child_request('/api/login', {'username': self.child['username'], 'password': self.password})[0], (401, 403))
        self.assertEqual(self.app.request(user_route, {'enabled': True, 'password': 'replacement-child-passphrase'})[0], 200)
        self.password = 'replacement-child-passphrase'
        self.login_child()
        route = '/api/books/' + self.visible_book['id']
        self.child_request(route + '/favorite', {'favorite': True})
        self.app.request(route + '/favorite', {'favorite': True})
        original = (self.visible_path / 'album' / '0.png').read_bytes()
        self.assertEqual(self.app.request(user_route + '/delete', {})[0], 200)
        self.assertFalse(self.child_request('/api/session')[1]['authenticated'])
        self.assertEqual(self.app.request('/api/users')[0], 200)
        self.assertTrue(self.app.request(route)[1]['favorite'])
        self.assertEqual((self.visible_path / 'album' / '0.png').read_bytes(), original)

    def test_registration_admin_protection_and_csrf(self):
        self.assertEqual(self.child_request('/api/register', {'username': 'outsider', 'password': self.password})[0], 409)
        self.assertFalse(self.child_request('/api/session')[1]['registration_required'])
        for route, payload in (('/api/users/1/delete', {}), ('/api/users/1', {'enabled': False}),
                                ('/api/users/1', {'role': 'user'})):
            self.assertIn(self.app.request(route, payload)[0], (400, 403, 409))
        self.assertEqual(self.app.request('/api/users', {'username': 'cross-site', 'password': self.password},
            {'Origin': 'https://example.invalid'})[0], 403)


if __name__ == '__main__':
    unittest.main()

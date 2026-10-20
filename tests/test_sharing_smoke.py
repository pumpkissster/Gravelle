"""Check the frozen smoke driver's independent crypto/closure assertions."""
from __future__ import annotations

import base64
from unittest import TestCase
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from desktop.smoke_windows import sample_png, sharing_smoke


class ScriptedService:
    """A minimal wire fixture; deliberately imports no application crypto."""
    token = 's' * 43
    identifier = 'a' * 32
    password = 'generated-disposable-password'
    key = bytes(range(32))

    def __init__(self, *, corrupt=False, ignore_close=False, lifetime_days=3,
                 reuse_capability=False, interrupt_new_share=False,
                 leak_passwords=False, wrong_reveal=False, guest_reveal=False,
                 count_reused_sessions=False, inherit_visits=False,
                 ignore_logout=False, logout_all_visitors=False):
        self.original = sample_png()
        self.corrupt = corrupt
        self.ignore_close = ignore_close
        self.lifetime_days = lifetime_days
        self.reuse_capability = reuse_capability
        self.interrupt_new_share = interrupt_new_share
        self.leak_passwords = leak_passwords
        self.wrong_reveal = wrong_reveal
        self.guest_reveal = guest_reveal
        self.count_reused_sessions = count_reused_sessions
        self.inherit_visits = inherit_visits
        self.ignore_logout = ignore_logout
        self.logout_all_visitors = logout_all_visitors
        self.shares = {}
        self.sessions = {}
        self.session_count = 0
        self.create_requests = []
        self.responses = 0

    def client(self, admin=False):
        service = self

        class Client:
            def __init__(self):
                self.last_headers = {}
                self.session = None

            def request(self, path, body=None, headers=None):
                self.last_headers = {}
                if admin and path == '/api/shares' and body is None:
                    rows = [{key: value for key, value in row.items() if key not in ('password', 'key', 'closed')}
                            for row in service.shares.values()]
                    if service.leak_passwords and rows:
                        rows[0]['password'] = service.password
                    return 200, {'shares': rows}, 'application/json'
                if admin and path == '/api/shares' and body is not None:
                    service.create_requests.append(dict(body))
                    number = len(service.create_requests) - 1
                    public = service.token if not number or service.reuse_capability else 't' * 43
                    identifier = service.identifier if not number or service.reuse_capability else 'c' * 32
                    password = service.password if not number or service.reuse_capability else service.password + '-new'
                    row = {'token': public, 'id': identifier, 'mode': 'time', 'created_at': 1,
                           'expires_at': 1 + service.lifetime_days * 86400,
                           'password': password, 'key': bytes((value + number) % 256 for value in service.key),
                           'closed': False, 'visits': 2 if number and service.inherit_visits else 0,
                           'password_available': True}
                    service.shares[public] = row
                    return 201, {'share': {key: value for key, value in row.items() if key not in ('password', 'key', 'closed')},
                                 'password': password}, 'application/json'
                if path.startswith('/api/shares/') and path.endswith('/password'):
                    if not admin and not service.guest_reveal:
                        return 401, {}, 'application/json'
                    identifier = path.split('/')[3]
                    row = next((value for value in service.shares.values() if value['id'] == identifier), None)
                    if row is None:
                        return 404, {}, 'application/json'
                    self.last_headers = {'Cache-Control': 'no-store'}
                    return 200, {'password': 'wrong-fixture-password' if service.wrong_reveal else row['password']}, 'application/json'
                if admin and path.endswith('/delete'):
                    identifier = path.split('/')[3]
                    row = next((value for value in service.shares.values() if value['id'] == identifier), None)
                    if row is None:
                        return 404, {}, 'application/json'
                    if not row['closed']:
                        return 409, {}, 'application/json'
                    del service.shares[row['token']]
                    if service.interrupt_new_share:
                        service.shares.clear()
                    return 200, {}, 'application/json'
                if admin and path.endswith('/close'):
                    identifier = path.split('/')[3]
                    row = next(value for value in service.shares.values() if value['id'] == identifier)
                    row['closed'] = True
                    return 200, {}, 'application/json'
                if path.startswith('/share/'):
                    self.last_headers = {'Cache-Control': 'no-store'}
                    return 200, b'<script src="/share-reader.js"></script>', 'text/html'
                static = {'/share-reader.js': ('text/javascript', b'GravelleShareReader'),
                          '/share-reader.css': ('text/css', b'.share-reader'),
                          '/share-crypto.js': ('text/javascript', b'GravelleShareCrypto'),
                          '/vendor/noble-gcm.js': ('text/javascript', b'GravelleNobleGCM'),
                          '/vendor/noble-ciphers-LICENSE.txt': ('text/plain', b'Paul Miller')}
                if path in static:
                    mime, data = static[path]
                    return 200, data, mime
                if not path.startswith('/api/share/'):
                    return 401, {}, 'application/json'
                public = path.split('/')[3]
                row = service.shares.get(public)
                if row is None:
                    return 404, {}, 'application/json'
                if row['closed'] and not service.ignore_close:
                    return 410, {}, 'application/json'
                session_id = (headers or {}).get('Cookie', '').removeprefix('guest=') or self.session
                session = service.sessions.get(session_id)
                authorized = session is not None and session['public'] == public
                if path.endswith('/logout'):
                    if service.logout_all_visitors:
                        service.sessions = {identifier: session for identifier, session in service.sessions.items()
                                            if session['public'] != public}
                    elif not service.ignore_logout:
                        service.sessions.pop(session_id, None)
                    self.session = None
                    self.last_headers = {'Set-Cookie': 'guest=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/api/share/' + public,
                                         'Cache-Control': 'no-store'}
                    return 200, {'logged_out': True}, 'application/json'
                if path.endswith('/unlock'):
                    if body['password'] != row['password']:
                        return 403, {}, 'application/json'
                    if not authorized or service.count_reused_sessions:
                        row['visits'] += 1
                    if not authorized:
                        service.session_count += 1
                        session_id = 'fixture' + str(service.session_count)
                        session = {'public': public, 'key': bytes((value + service.session_count) % 256 for value in row['key'])}
                        service.sessions[session_id] = session
                    self.session = session_id
                    self.last_headers = {'Set-Cookie': 'guest=' + session_id + '; HttpOnly; SameSite=Strict; Path=/api/share/' + public}
                    return 200, {'key': base64.urlsafe_b64encode(session['key']).rstrip(b'=').decode(),
                                 'manifest': {'page_count': 1}}, 'application/json'
                if not authorized:
                    return 401, {}, 'application/json'
                if path.endswith('/manifest'):
                    return 200, {}, 'application/json'
                service.responses += 1
                nonce = service.responses.to_bytes(12, 'big')
                header = b'GSH1' + nonce + b'\x02'
                packet = header + AESGCM(session['key']).encrypt(nonce, service.original,
                            header + ('gravelle-share-v1:' + public + ':0').encode())
                if service.corrupt:
                    packet = packet[:-1] + bytes([packet[-1] ^ 1])
                self.last_headers = {'Cache-Control': 'no-store'}
                return 200, packet, 'application/octet-stream'

        return Client()


class SharingSmokeDriverTests(TestCase):
    def check(self, service):
        with patch('desktop.smoke_windows.Client', side_effect=lambda _: service.client()):
            return sharing_smoke(service.client(admin=True), 'http://127.0.0.1:12345', 'b' * 24, service.original)

    def test_independent_decryption_accepts_wire_protocol_without_reporting_secrets(self):
        service = ScriptedService()
        result = self.check(service)
        self.assertEqual(result['plaintext_bytes'], len(service.original))
        self.assertTrue(result['authenticated_header'])
        self.assertTrue(result['closed_images_rejected'])
        self.assertEqual(result['valid_days'], 3)
        self.assertTrue(result['reshare_uses_new_capability'])
        self.assertTrue(result['active_delete_rejected'])
        self.assertTrue(result['closed_record_deleted'])
        self.assertTrue(result['new_share_remains_readable'])
        self.assertTrue(result['creator_password_reveal'])
        self.assertTrue(result['listing_excludes_credentials'])
        self.assertTrue(result['visits_count_new_sessions_only'])
        self.assertTrue(result['independent_visit_counts'])
        self.assertTrue(result['logout_revokes_server_session'])
        self.assertTrue(result['logout_preserves_other_visitors'])
        self.assertTrue(result['logout_requires_password_again'])
        self.assertTrue(result['logout_preserves_visit_statistics'])
        self.assertEqual(service.create_requests, [{'book_id': 'b' * 24, 'mode': 'time', 'days': 3}] * 2)
        for secret in (service.password, service.token, base64.urlsafe_b64encode(service.key).decode()):
            self.assertNotIn(secret, str(result))

    def test_independent_driver_rejects_a_modified_authentication_tag(self):
        with self.assertRaises(InvalidTag):
            self.check(ScriptedService(corrupt=True))

    def test_driver_rejects_closed_shares_that_still_return_images(self):
        with self.assertRaisesRegex(AssertionError, 'closed share still allowed'):
            self.check(ScriptedService(ignore_close=True))

    def test_driver_rejects_the_wrong_share_lifetime(self):
        with self.assertRaisesRegex(AssertionError, 'three-day deadline'):
            self.check(ScriptedService(lifetime_days=1))

    def test_driver_rejects_recreation_that_reuses_the_old_capability(self):
        with self.assertRaisesRegex(AssertionError, 'reused an old link'):
            self.check(ScriptedService(reuse_capability=True))

    def test_driver_rejects_old_record_deletion_that_interrupts_the_new_share(self):
        with self.assertRaisesRegex(AssertionError, 'deletion interrupted'):
            self.check(ScriptedService(interrupt_new_share=True))

    def test_driver_rejects_passwords_disclosed_in_a_management_listing(self):
        with self.assertRaisesRegex(AssertionError, 'listing disclosed'):
            self.check(ScriptedService(leak_passwords=True))

    def test_driver_rejects_an_incorrect_revealed_password(self):
        with self.assertRaisesRegex(AssertionError, 'creator could not reveal'):
            self.check(ScriptedService(wrong_reveal=True))

    def test_driver_rejects_password_reveal_by_anonymous_visitors(self):
        with self.assertRaisesRegex(AssertionError, 'guest cookie incorrectly authenticated'):
            self.check(ScriptedService(guest_reveal=True))

    def test_driver_rejects_reused_sessions_counted_as_new_visits(self):
        with self.assertRaisesRegex(AssertionError, 'visit statistics counted'):
            self.check(ScriptedService(count_reused_sessions=True))

    def test_driver_rejects_new_shares_inheriting_old_visit_counts(self):
        with self.assertRaisesRegex(AssertionError, 'inherited visits'):
            self.check(ScriptedService(inherit_visits=True))

    def test_driver_rejects_cookie_only_logout_that_leaves_server_session_usable(self):
        with self.assertRaisesRegex(AssertionError, 'old server session usable'):
            self.check(ScriptedService(ignore_logout=True))

    def test_driver_rejects_logout_that_interrupts_other_visitors(self):
        with self.assertRaisesRegex(AssertionError, 'logout interrupted another visitor'):
            self.check(ScriptedService(logout_all_visitors=True))

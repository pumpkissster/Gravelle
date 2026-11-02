"""Private certificate import, rollback, and actual authenticated HTTPS requests."""
import base64
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ObjectIdentifier

import server as reader
from https_settings import (CERTIFICATE_DIRECTORY, MAX_CERTIFICATE_BASE64,
                            MAX_CERTIFICATE_BODY_BYTES, CertificateUploadStore,
                            HTTPSSettingsError, decode_certificate_upload)
from test_https_settings import certificate, free_port


def scratch():
    parent = Path(__file__).resolve().parents[1] / 'work' / 'https-upload-tests'
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=parent)


def upload(raw, name='用户证书.PFX'):
    return {'name': name, 'data': base64.b64encode(raw).decode('ascii')}


class CertificateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = scratch(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CertificateUploadStore(self.root)

    def test_upload_names_types_and_base64_are_strict_before_disk_access(self):
        invalid = [None, {}, {'name': 'a.pfx', 'data': 'Zg==', 'path': '/tmp'},
                   upload(b'x', '../test.pfx'), upload(b'x', r'C:\test.p12'),
                   upload(b'x', 'bad\n.pfx'), upload(b'x', 'bad.pem'),
                   {'name': 'a.pfx', 'data': ''}, {'name': 'a.p12', 'data': 'Zg==\n'},
                   {'name': 'a.pfx', 'data': 'Zh=='}, {'name': 'a.pfx', 'data': 42}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(HTTPSSettingsError):
                self.store.stage(value)
        self.assertFalse(self.store.path.exists())
        with patch('https_settings.base64.b64decode') as decoder:
            with self.assertRaises(HTTPSSettingsError):
                decode_certificate_upload({'name': 'a.pfx', 'data': 'A' * (MAX_CERTIFICATE_BASE64 + 4)})
            decoder.assert_not_called()

    def test_private_storage_never_uses_client_filename_and_cleanup_is_scoped(self):
        target = self.store.stage(upload(b'generated-test-content', 'friendly.p12'))
        self.assertEqual(target.parent, self.root / CERTIFICATE_DIRECTORY)
        self.assertRegex(target.name, r'^[a-f0-9]{32}\.p12$')
        self.assertEqual(target.read_bytes(), b'generated-test-content')
        if os.name != 'nt':
            self.assertEqual(target.stat().st_mode & 0o077, 0)
            self.assertEqual(target.parent.stat().st_mode & 0o077, 0)
        external = self.root / ('a' * 32 + '.pfx'); external.write_bytes(b'keep')
        unowned = target.parent / 'manual.pfx'; unowned.write_bytes(b'keep')
        self.assertFalse(self.store.discard(external)); self.assertFalse(self.store.discard(unowned))
        self.assertEqual(external.read_bytes(), b'keep'); self.assertEqual(unowned.read_bytes(), b'keep')
        self.assertTrue(self.store.discard(target)); self.assertFalse(target.exists())

    def test_symlink_directory_is_rejected_without_writing_to_its_target(self):
        external = self.root / 'elsewhere'; external.mkdir()
        try:
            self.store.path.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating directory symlinks is not permitted on this platform')
        with self.assertRaises(HTTPSSettingsError):
            self.store.stage(upload(b'private'))
        self.assertFalse(any(external.iterdir()))

    def test_cleanup_rejects_hardlinks_and_symlinks(self):
        original = self.store.stage(upload(b'keep'))
        hardlink = original.parent / ('a' * 32 + '.pfx')
        try:
            os.link(original, hardlink)
        except (OSError, NotImplementedError):
            self.skipTest('Hard links are unavailable on this platform')
        self.assertFalse(self.store.discard(original)); self.assertFalse(self.store.discard(hardlink))
        self.assertEqual(original.read_bytes(), b'keep')
        hardlink.unlink()
        linked = original.parent / ('b' * 32 + '.p12')
        try:
            linked.symlink_to(original)
        except (OSError, NotImplementedError):
            return
        self.assertFalse(self.store.discard(linked)); self.assertEqual(original.read_bytes(), b'keep')


class CertificateUploadHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_temp = scratch(); root = Path(cls.cert_temp.name)
        cls.plain, plain_ca = certificate(root, 'plain')
        cls.password = '  上传证书 密码  '
        cls.protected, protected_ca = certificate(root, 'locked', cls.password)
        cls.context = ssl.create_default_context(cafile=str(plain_ca))
        cls.context.load_verify_locations(cafile=str(protected_ca))
        cls.expired, _ = certificate(root, 'expired', expired=True)
        cls.without_key, _ = certificate(root, 'no-key', missing_key=True)

    @classmethod
    def tearDownClass(cls):
        cls.cert_temp.cleanup()

    def setUp(self):
        self.temp = scratch(); self.root = Path(self.temp.name)
        media = self.root / 'media'; media.mkdir()
        self.library = reader.Library(media, self.root / 'data', '', 0)
        self.library.transcoding.auto_start = self.library.image_search.auto_start = False
        self.library.register({'username': 'upload-admin', 'password': 'generated-test-password'}, 'fixture')
        child = self.library.save_user({'username': 'upload-child', 'password': 'generated-child-password', 'library_ids': []})['user']
        self.admin = f'{reader.COOKIE_NAME}={self.library.make_session(1)}'
        self.child = f'{reader.COOKIE_NAME}={self.library.make_session(child["id"])}'
        self.share_port, self.https_port = free_port(), free_port()
        self.server = reader.ReaderServer(('127.0.0.1', 0), self.library,
                                          share_port=self.share_port, https_port=self.https_port, runtime='source')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.library.close(); self.temp.cleanup()

    def files(self):
        store = self.library.network.certificates.path
        return set(store.iterdir()) if store.exists() else set()

    def request(self, path, body=None, *, tls=False, port=None, cookie=None, context=None, headers=None):
        port = port or (self.https_port if tls else self.server.server_port)
        metadata = {'Content-Type': 'application/json'} if body is not None else {}
        if cookie:
            metadata['Cookie'] = cookie
        metadata.update(headers or {})
        connection = (http.client.HTTPSConnection('127.0.0.1', port, context=context or self.context, timeout=4)
                      if tls else http.client.HTTPConnection('127.0.0.1', port, timeout=4))
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body) if body is not None else None, metadata)
            response = connection.getresponse(); raw = response.read()
            result = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
            return response.status, result
        finally:
            connection.close()

    def configure(self, **body):
        return self.request('/api/network-settings', body, cookie=self.admin)

    def import_certificate(self, path=None, **extra):
        status, result = self.configure(connection_mode='https', certificate_upload=upload((path or self.plain).read_bytes()), **extra)
        self.assertEqual(status, 200, result)
        return result

    def test_upload_runs_verified_tls_and_private_share_without_exposing_file_content(self):
        result = self.import_certificate(self.protected, certificate_password=self.password, share_port_enabled=True)
        target = Path(result['certificate_path'])
        self.assertEqual(target.read_bytes(), self.protected.read_bytes())
        self.assertTrue(result['https_listening']); self.assertTrue(result['share_listening'])
        self.assertTrue(result['certificate_password_set'])
        for key in ('certificate_upload', 'certificate_password', 'certificate_password_ciphertext'):
            self.assertNotIn(key, result)
        self.assertNotIn(self.password, json.dumps(self.library.network.current(), ensure_ascii=False))
        self.assertEqual(self.request('/api/health', tls=True)[0], 200)
        self.assertEqual(self.request('/share-reader.js', tls=True, port=self.share_port)[0], 200)
        self.assertEqual(self.request('/api/network-settings', {}, tls=True, port=self.share_port, cookie=self.admin)[0], 404)
        self.assertEqual(self.request('/https-certificates/' + target.name, cookie=self.admin)[0], 404)
        self.assertEqual(self.request('/' + target.name, tls=True, cookie=self.admin)[0], 404)

    def test_valid_pfx_above_normal_json_limit_is_accepted_only_here(self):
        key, leaf, _ = pkcs12.load_key_and_certificates(self.plain.read_bytes(), None)
        large_leaf = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
                      .public_key(key.public_key()).serial_number(x509.random_serial_number())
                      .not_valid_before(leaf.not_valid_before_utc).not_valid_after(leaf.not_valid_after_utc)
                      .add_extension(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False)
                      .add_extension(x509.UnrecognizedExtension(ObjectIdentifier('1.2.3.4.5'), b'x' * 20000), critical=False)
                      .sign(key, hashes.SHA256()))
        raw = pkcs12.serialize_key_and_certificates(b'large fixture', key, large_leaf, None, serialization.NoEncryption())
        body = {'connection_mode': 'https', 'certificate_upload': upload(raw)}
        self.assertGreater(len(json.dumps(body)), reader.MAX_BODY_BYTES)
        self.assertEqual(self.request('/api/network-settings', body, cookie=self.admin)[0], 200)
        context = ssl.create_default_context(cadata=large_leaf.public_bytes(serialization.Encoding.PEM).decode())
        self.assertEqual(self.request('/api/health', tls=True, context=context)[0], 200)
        self.assertEqual(self.request('/api/logout', body, cookie=self.admin)[0], 413)

    def test_bad_password_invalid_expired_or_keyless_certificates_leave_old_tls(self):
        before = self.import_certificate(); existing = self.files()
        for data, password in ((self.protected.read_bytes(), 'wrong'), (b'not a certificate', ''),
                               (self.expired.read_bytes(), ''), (self.without_key.read_bytes(), '')):
            with self.subTest(password=password, size=len(data)):
                self.assertEqual(self.configure(connection_mode='https', certificate_upload=upload(data), certificate_password=password)[0], 400)
                self.assertEqual(self.server.network_snapshot(), before)
                self.assertEqual(self.files(), existing)
                self.assertEqual(self.request('/api/health', tls=True)[0], 200)

    def test_managed_replacement_removes_only_old_managed_file_and_keeps_password_by_default(self):
        external = self.root / 'external.pfx'; external.write_bytes(self.plain.read_bytes())
        self.assertEqual(self.configure(connection_mode='https', certificate_path=str(external))[0], 200)
        first = self.import_certificate(self.protected, certificate_password=self.password)
        self.assertTrue(external.exists()); old = Path(first['certificate_path'])
        second = self.import_certificate(self.protected)
        self.assertFalse(old.exists()); self.assertTrue(Path(second['certificate_path']).is_file())
        self.assertTrue(external.exists()); self.assertEqual(self.library.network.certificate_password(), self.password)
        self.assertEqual(self.configure(connection_mode='http')[0], 200)
        self.assertTrue(Path(second['certificate_path']).exists())

    def test_database_failure_and_new_share_port_failure_clean_new_files_and_keep_old_tls(self):
        before = self.import_certificate(); existing = self.files()
        with patch.object(self.library.network, 'persist', side_effect=sqlite3.OperationalError('generated DB failure')):
            with self.assertLogs(reader.LOG, level='ERROR'):
                self.assertEqual(self.configure(connection_mode='https', certificate_upload=upload(self.protected.read_bytes()), certificate_password=self.password)[0], 500)
        self.assertEqual(self.files(), existing); self.assertEqual(self.server.network_snapshot(), before)
        self.assertEqual(self.request('/api/health', tls=True)[0], 200)
        with socket.socket() as occupied:
            if os.name == 'nt':
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(('127.0.0.1', self.share_port)); occupied.listen()
            self.assertEqual(self.configure(connection_mode='https', certificate_upload=upload(self.protected.read_bytes()),
                                            certificate_password=self.password, share_port_enabled=True)[0], 409)
        self.assertEqual(self.files(), existing); self.assertEqual(self.server.network_snapshot(), before)
        self.assertEqual(self.request('/api/health', tls=True)[0], 200)

    def test_cleanup_failure_after_commit_does_not_report_failure(self):
        first = self.import_certificate()
        with patch.object(self.library.network.certificates, 'discard', side_effect=OSError('generated cleanup failure')):
            second = self.import_certificate()
        self.assertNotEqual(first['certificate_path'], second['certificate_path'])
        self.assertEqual(self.server.network_snapshot(), second)
        self.assertEqual(self.request('/api/health', tls=True)[0], 200)

    def test_stale_form_cannot_restore_deleted_managed_path_and_delete_current_certificate(self):
        first = self.import_certificate()
        second = self.import_certificate()
        self.assertFalse(Path(first['certificate_path']).exists())
        before = self.server.network_snapshot(); existing = self.files()
        status, _ = self.configure(connection_mode='http', certificate_path=first['certificate_path'])
        self.assertEqual(status, 400)
        self.assertEqual(self.server.network_snapshot(), before)
        self.assertEqual(self.files(), existing)
        self.assertTrue(Path(second['certificate_path']).is_file())
        self.assertEqual(self.request('/api/health', tls=True)[0], 200)
        # Turning HTTPS off without overwriting an unedited certificate path
        # preserves the newer certificate for a later re-enable.
        status, disabled = self.configure(connection_mode='http')
        self.assertEqual(status, 200); self.assertEqual(disabled['certificate_path'], second['certificate_path'])
        self.assertTrue(Path(second['certificate_path']).is_file())

    def test_upload_requires_https_and_rejects_ambiguous_external_path(self):
        before = self.server.network_snapshot()
        for extra in ({}, {'connection_mode': 'http'}, {'connection_mode': 'proxy'},
                      {'connection_mode': 'https', 'certificate_path': str(self.plain)}):
            with self.subTest(extra=extra):
                self.assertEqual(self.configure(certificate_upload=upload(self.plain.read_bytes()), **extra)[0], 400)
        self.assertEqual(self.server.network_snapshot(), before); self.assertFalse(self.files())

    def test_receiving_upload_does_not_block_other_requests_and_rechecks_revoked_session(self):
        original = reader.ReaderHandler.read_body
        for revoked in (False, True):
            entered = threading.Event()
            session = self.library.make_session(1)
            cookie = f'{reader.COOKIE_NAME}={session}'
            raw = json.dumps({'connection_mode': 'https', 'certificate_upload': upload(self.plain.read_bytes())}).encode()
            def observed(handler, *args, **kwargs):
                entered.set()
                return original(handler, *args, **kwargs)
            with self.subTest(revoked=revoked), patch.object(reader.ReaderHandler, 'read_body', observed):
                with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=4) as slow:
                    headers = (f'POST /api/network-settings HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'
                               f'Cookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n').encode()
                    slow.sendall(headers + raw[:20]); self.assertTrue(entered.wait(2))
                    health = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
                    try:
                        health.request('GET', '/api/health'); response = health.getresponse()
                        self.assertEqual(response.status, 200); response.read()
                    finally:
                        health.close()
                        if revoked:
                            self.library.revoke_session(session)
                        slow.sendall(raw[20:])
                    response = http.client.HTTPResponse(slow); response.begin(); response.read()
                    self.assertEqual(response.status, 401 if revoked else 200)

    def test_early_rejections_close_before_sensitive_body_or_pipeline_is_logged(self):
        self.assertEqual(self.configure(share_port_enabled=True)[0], 200)
        marker = 'PRIVATE_CERTIFICATE_BODY_MUST_NOT_BE_LOGGED'
        for cookie, origin, port, length, extras, expected in (
                ('', '', self.server.server_port, 100, '', 401),
                (self.child, '', self.server.server_port, 100, '', 403),
                (self.admin, 'https://wrong.example', self.server.server_port, 100, '', 403),
                (self.admin, '', self.share_port, 100, '', 404),
                (self.admin, '', self.server.server_port, MAX_CERTIFICATE_BODY_BYTES + 1, '', 413),
                (self.admin, '', self.server.server_port, 100, 'Content-Encoding: gzip\r\n', 400),
                (self.admin, '', self.server.server_port, 100, 'Content-Length: 100\r\n', 400),
                (self.admin, '', self.server.server_port, 100, 'Transfer-Encoding: chunked\r\n', 400)):
            with self.subTest(expected=expected, port=port, headers=extras), self.assertLogs(reader.LOG, level='INFO') as logs:
                metadata = f'Cookie: {cookie}\r\n' if cookie else ''
                metadata += f'Origin: {origin}\r\n' if origin else ''
                raw = (f'POST /api/network-settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                       f'Content-Type: application/json\r\nContent-Length: {length}\r\n{metadata}{extras}\r\n'
                       + marker + '\r\nGET /api/health HTTP/1.1\r\nHost: ignored\r\n\r\n').encode()
                with socket.create_connection(('127.0.0.1', port), timeout=4) as connection:
                    connection.sendall(raw); received = b''
                    while True:
                        try:
                            block = connection.recv(8192)
                        except ConnectionResetError:
                            break
                        if not block:
                            break
                        received += block
                self.assertIn(f'HTTP/1.1 {expected}'.encode(), received)
                self.assertEqual(received.count(b'HTTP/1.1 '), 1)
                self.assertNotIn(marker, '\n'.join(logs.output))
        self.assertFalse(self.files())


if __name__ == '__main__':
    unittest.main()

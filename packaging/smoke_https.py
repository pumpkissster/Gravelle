#!/usr/bin/env python3
"""Verify HTTPS in disposable native packages and fresh CI containers.

This is a destructive test of its *generated fixtures*, never a diagnostic for
an existing installation. --executable creates its own data and private child;
--disposable-url requires explicit confirmation and an unused, empty database.
No system trust store or TLS verification settings are changed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from http.cookiejar import CookieJar
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler,
                            ProxyHandler, Request, build_opener)
import zlib

MARKER = '.gravelle-generated-https-fixture.json'
IMAGE = 'Generated HTTPS fixture/001.png'
MAX_RESPONSE = 2 * 1024 * 1024


def require(value, message):
    if not value:
        raise RuntimeError(message)


def loopback_origin(value):
    url = urlsplit(value)
    try:
        address = ipaddress.ip_address(url.hostname or '')
        port = url.port
    except ValueError:
        raise ValueError('Select a literal loopback HTTP origin') from None
    if (url.scheme != 'http' or not address.is_loopback or not port
            or url.username or url.password or url.path not in ('', '/')
            or url.query or url.fragment):
        raise ValueError('Select a literal loopback HTTP origin without credentials or paths')
    return f'http://{url.netloc}'


def generated_png():
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
    pixels = (b'\0' + bytes((234, 80, 152)) * 24) * 32
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 24, 32, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def prepare_library(path):
    path = Path(path).resolve()
    require(not path.exists() or not any(path.iterdir()), 'Fixture library must be empty')
    (path / Path(IMAGE).parent).mkdir(parents=True)
    source = generated_png()
    (path / IMAGE).write_bytes(source)
    (path / MARKER).write_text(json.dumps({'fixture': secrets.token_hex(16),
        'sha256': hashlib.sha256(source).hexdigest()}), encoding='utf-8')
    return path


def fixture_digest(path):
    marker = Path(path) / MARKER
    require(marker.is_file() and not marker.is_symlink(), 'Missing generated fixture marker')
    value = json.loads(marker.read_text(encoding='utf-8'))
    digest = hashlib.sha256((Path(path) / IMAGE).read_bytes()).hexdigest()
    require(isinstance(value.get('fixture'), str) and len(value['fixture']) == 32
            and value.get('sha256') == digest, 'Generated fixture was changed')
    return digest


def empty_database(data):
    database = Path(data) / 'library.sqlite3'
    require(database.is_file() and not database.is_symlink(), 'Expected a fresh disposable database')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ('users', 'owner_account', 'media_libraries', 'books', 'auth_sessions'):
            require(name in tables and connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] == 0,
                    'HTTPS smoke refuses an existing installation or previously used database')
        row = connection.execute('SELECT settings FROM network_settings WHERE id=1').fetchone()
        value = json.loads(row[0]) if row else {}
        require(value.get('connection_mode') == 'http' and value.get('security_entry_enabled') is False
                and not value.get('security_entry_code') and value.get('share_port_enabled') is False
                and not value.get('public_share_url') and not value.get('certificate_path')
                and not value.get('certificate_password_ciphertext'), 'Expected untouched default network settings')


def certificate(directory, password):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Generated HTTPS smoke CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    leaf = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, None, None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                            x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    bundle = pkcs12.serialize_key_and_certificates(b'generated-smoke', key, leaf, [ca],
                      serialization.BestAvailableEncryption(password.encode('utf-8')))
    path, trust = Path(directory) / 'server.pfx', Path(directory) / 'ca.pem'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        output.write(bundle)
    trust.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return path, trust


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class Client:
    def __init__(self, origin, context=None):
        self.origin = origin
        handlers = [ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(CookieJar())]
        if context is not None:
            require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,
                    'TLS verification must remain enabled')
            handlers.append(HTTPSHandler(context=context))
        self.opener = build_opener(*handlers)

    def request(self, path, body=None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        headers = {'Origin': self.origin}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.origin + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            content = response.read(MAX_RESPONSE + 1)
            require(len(content) <= MAX_RESPONSE, 'Unexpectedly large smoke response')
            value = (json.loads(content.decode('utf-8')) if response.headers.get_content_type() == 'application/json'
                     else content)
            return response.status, value, dict(response.headers)


def ports_free(ports):
    sockets = []
    try:
        for port in ports:
            candidate = socket.socket()
            sockets.append(candidate)
            if os.name == 'nt':
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        for candidate in sockets:
            candidate.close()


def random_ports():
    held = []
    try:
        for _ in range(3):
            candidate = socket.socket(); held.append(candidate)
            candidate.bind(('127.0.0.1', 0))
        return tuple(candidate.getsockname()[1] for candidate in held)
    finally:
        for candidate in held:
            candidate.close()


class Child:
    def __init__(self, executable, library, data, ports, directory, entry_script=None):
        self.ports = ports
        instance = secrets.token_hex(16)
        command = [str(executable)] + ([str(entry_script)] if entry_script else [])
        command += ['--serve', '--library', str(library), '--data', str(data), '--host', '127.0.0.1',
                    '--port', str(ports[0]), '--share-port', str(ports[1]), '--https-port', str(ports[2]),
                    '--instance', instance]
        self.log = (Path(directory) / 'child.log').open('wb')
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.log, cwd=str(executable.parent), env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.messages = queue.Queue()
        threading.Thread(target=self._messages, daemon=True).start()
        deadline = time.monotonic() + 90
        try:
            while time.monotonic() < deadline:
                require(self.process.poll() is None, 'Owned child exited before readiness')
                try:
                    value = self.messages.get(timeout=.1)
                except queue.Empty:
                    continue
                if value.get('ready') is False:
                    raise RuntimeError('Owned child rejected disposable startup')
                if value.get('ready') is True and value.get('instance') == instance and value.get('port') == ports[0]:
                    return
            raise RuntimeError('Private readiness handshake timed out')
        except BaseException:
            self.close()
            raise

    def _messages(self):
        for raw in iter(self.process.stdout.readline, b''):
            if len(raw) > 16384:
                continue
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    self.messages.put(value)
            except (ValueError, UnicodeError):
                pass

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.write(b'stop\n'); self.process.stdin.flush()
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=10)
                    raise RuntimeError('Owned child required forced cleanup') from None
            require(self.process.returncode == 0, 'Owned child did not shut down cleanly')
        finally:
            self.process.stdin.close(); self.process.stdout.close(); self.log.close()


def exercise(origin, library, data, ports, scratch, report):
    original = fixture_digest(library)
    empty_database(data)
    admin = Client(origin)
    status, health, _ = admin.request('/api/health')
    require(status == 200 and health.get('status') == 'ok' and health.get('https_port') == ports[2]
            and health.get('connection_mode') == 'http' and health.get('https_listening') is False,
            'Unexpected initial HTTP health contract')
    report['version'] = health['version']
    checks = report['checks']
    checks.append('empty_disposable_database_and_actual_https_port')
    credentials = {'username': 'https-smoke-' + secrets.token_hex(8), 'password': secrets.token_urlsafe(32)}
    status, _, _ = admin.request('/api/register', credentials)
    require(status == 201, 'Disposable registration failed')
    # Verify this HTTP target really created its account in the selected new DB.
    with closing(sqlite3.connect((Path(data) / 'library.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        require(connection.execute('SELECT username FROM users').fetchall() == [(credentials['username'],)],
                'HTTP target is not the selected disposable database')
    status, result, _ = admin.request('/api/libraries', {'name': 'Generated HTTPS smoke', 'path': str(library), 'media_type': 'photo'})
    require(status == 200 or status == 201, 'Generated library creation failed')
    deadline = time.monotonic() + 30
    books = []
    while time.monotonic() < deadline:
        status, listing, _ = admin.request('/api/library')
        if status == 200:
            books = listing.get('books', [])
            if len(books) == 1:
                break
        time.sleep(.1)
    require(len(books) == 1, 'Generated photo was not scanned')
    password = '  ' + secrets.token_urlsafe(24) + ' TLS 密码  '
    path, trust = certificate(scratch, password)
    context = ssl.create_default_context(cafile=str(trust))
    request = {'connection_mode': 'https', 'certificate_path': str(path),
               'certificate_password': password, 'share_port_enabled': True}
    try:
        with socket.socket() as blocker:
            blocker.bind(('127.0.0.1', ports[2])); blocker.listen()
            require(admin.request('/api/network-settings', request)[0] == 409, 'Occupied HTTPS port was accepted')
        before = admin.request('/api/network-settings')[1]
        require(before['connection_mode'] == 'http' and not before['https_listening'] and not before['share_listening'],
                'Initial port conflict changed saved settings')
        checks.append('occupied_https_port_keeps_http_recovery_and_saved_settings')
        status, saved, _ = admin.request('/api/network-settings', request)
        require(status == 200 and saved['https_listening'] and saved['share_listening']
                and saved['share_scheme'] == 'https' and saved['certificate_password_set'], 'TLS listeners did not start')
        require('certificate_password' not in saved and 'certificate_password_ciphertext' not in saved,
                'Network API disclosed a saved secret')
        main = Client(f'https://127.0.0.1:{ports[2]}', context)
        shared = Client(f'https://127.0.0.1:{ports[1]}', context)
        require(main.request('/api/health')[1]['https_listening'] and main.request('/')[0] == 200,
                'Strictly verified main TLS failed')
        status, _, headers = main.request('/api/login', credentials)
        require(status == 200 and 'Secure' in headers.get('Set-Cookie', ''), 'TLS login cookie must be Secure')
        require(main.request('/api/network-settings')[0] == 200, 'TLS administrative session failed')
        checks.append('verified_ca_chain_and_ip_san_with_secure_login_cookie')
        for negative_context, hostname in ((ssl.create_default_context(), 'localhost'), (context, 'wrong-host.invalid')):
            try:
                with socket.create_connection(('127.0.0.1', ports[2]), timeout=5) as raw:
                    with negative_context.wrap_socket(raw, server_hostname=hostname):
                        raise RuntimeError('Untrusted CA or incorrect hostname was accepted')
            except ssl.SSLCertVerificationError:
                pass
        checks.append('negative_controls_reject_unknown_ca_and_wrong_hostname')
        for route in ('/', '/api/health', '/api/session', '/api/library', '/api/network-settings'):
            require(shared.request(route)[0] == 404, 'TLS share listener exposed administration')
        require(shared.request('/share-reader.js')[0] == 200, 'TLS sharing resources are missing')
        checks.append('independent_tls_share_listener_refuses_management_routes')
        status, created, _ = admin.request('/api/shares', {'book_id': books[0]['id'], 'mode': 'time', 'days': 1})
        require(status == 201, 'Generated share creation failed')
        token = created['share']['token']; base = '/api/share/' + token
        require(shared.request('/share/' + token)[0] == 200 and shared.request(base + '/manifest')[0] == 401,
                'TLS share shell or protected manifest failed')
        require(shared.request(base + '/unlock', {'password': 'wrong-' + secrets.token_hex(8)})[0] == 403,
                'Wrong share password accepted')
        status, unlocked, headers = shared.request(base + '/unlock', {'password': created['password']})
        require(status == 200 and unlocked['manifest']['page_count'] == 1
                and 'Secure' in headers.get('Set-Cookie', ''), 'TLS share unlock or Secure visitor cookie failed')
        require(shared.request(base + '/pages/0')[0] == 200 and admin.request('/share/' + token)[0] == 404,
                'TLS-only encrypted sharing or HTTP isolation failed')
        checks.append('password_protected_share_and_encrypted_page_over_verified_tls')
        before = admin.request('/api/network-settings')[1]
        require(admin.request('/api/network-settings', {'certificate_password': secrets.token_urlsafe(24)})[0] == 400,
                'Incorrect PFX password was accepted')
        require(admin.request('/api/network-settings')[1] == before and main.request('/')[0] == 200
                and shared.request('/share-reader.js')[0] == 200, 'Bad password damaged the previous TLS listeners')
        require(admin.request('/api/network-settings')[0] == 200 and admin.request('/api/health')[0] == 200,
                'Original HTTP recovery was lost')
        checks.append('incorrect_pfx_password_rolls_back_and_http_management_remains')
    finally:
        status, _, _ = admin.request('/api/network-settings', {'connection_mode': 'http', 'share_port_enabled': False})
        require(status == 200, 'Could not disable disposable TLS listeners')
        require(ports_free(ports[1:]), 'Disposable TLS/share listeners were not released')
        checks.append('disabled_tls_and_share_listeners_release_ports')
        require(fixture_digest(library) == original, 'Source photo was changed')
        checks.append('generated_source_photo_unchanged')


def run(arguments):
    started = time.monotonic()
    report = {'status': 'running', 'checks': [], 'strict_ca_and_hostname_verification': True,
              'system_trust_modified': False, 'native_gui_opened': False,
              'mode': 'native-child' if arguments.executable else 'disposable-container'}
    work = arguments.work_root.resolve()
    work.mkdir(parents=True, exist_ok=True)
    child = None
    try:
        with tempfile.TemporaryDirectory(prefix='gravelle-https-smoke-', dir=work) as temporary:
            scratch = Path(temporary).resolve()
            if arguments.executable:
                executable = arguments.executable.resolve(strict=True)
                entry = arguments.entry_script.resolve(strict=True) if arguments.entry_script else None
                require(executable.is_file(), 'Native executable is missing')
                media = prepare_library(scratch / 'library'); data = scratch / 'data'
                ports = random_ports()
                child = Child(executable, media, data, ports, scratch, entry)
                report['executable_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
                report['checks'].append('private_readiness_handshake_for_owned_child')
                origin = f'http://127.0.0.1:{ports[0]}'
            else:
                require(arguments.confirm_disposable and arguments.data and arguments.library,
                        'Container mode requires --confirm-disposable, --data and --library')
                origin = loopback_origin(arguments.disposable_url)
                require(urlsplit(origin).hostname == '127.0.0.1', 'Container smoke requires IPv4 loopback')
                ports = (urlsplit(origin).port, 8081, 8443)
                require(ports[0] not in ports[1:], 'HTTP and TLS ports overlap')
                media, data = arguments.library.resolve(strict=True), arguments.data.resolve(strict=True)
            try:
                exercise(origin, media, data, ports, scratch, report)
            finally:
                if child:
                    child.close()
                    require(ports_free(ports), 'Owned child listeners were not all released')
                    report['checks'].append('private_stop_exits_child_and_releases_all_ports')
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error)[:300])
    finally:
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--executable', type=Path)
    mode.add_argument('--disposable-url')
    mode.add_argument('--prepare-library', type=Path)
    parser.add_argument('--entry-script', type=Path, help='Optional source launcher for development-only smoke')
    parser.add_argument('--work-root', type=Path, default=Path(tempfile.gettempdir()) / 'gravelle-https-smoke')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--data', type=Path)
    parser.add_argument('--library', type=Path)
    parser.add_argument('--confirm-disposable', action='store_true')
    arguments = parser.parse_args(argv)
    if arguments.prepare_library:
        prepare_library(arguments.prepare_library)
        print(json.dumps({'status': 'prepared', 'generated_only': True}))
        return 0
    if not arguments.report:
        parser.error('--report is required')
    if arguments.entry_script and not arguments.executable:
        parser.error('--entry-script requires --executable')
    report = run(arguments)
    print(json.dumps({'status': report['status'], 'checks': len(report['checks']),
                      'report': str(arguments.report), 'duration_seconds': report['duration_seconds']}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

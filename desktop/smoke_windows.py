#!/usr/bin/env python3
"""Exercise the actual frozen server using disposable data and random ports.

The test driver requires Pillow for generated screenshot/crop fixtures and
cryptography for independent share-response decryption. CI runs it with the
build environment's Python; the service itself runs only from the EXE.

Example: python desktop/smoke_windows.py --executable
dist/windows/portable/Gravelle/GravelleServer.exe --work-root work/windows-smoke
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import http.cookiejar
from io import BytesIO
import json
import locale
import math
from pathlib import Path
import random
import re
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import zlib
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.core import LauncherConfig, check_port
from desktop.runtime import ServerController


def sample_png(color=(234, 80, 152), size=(2, 2)) -> bytes:
    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data +
                struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff))
    # Keep the original tiny-image checks independent of extra image codecs.
    width, height = size
    pixels = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def screenshot_and_crop_fixtures() -> tuple[bytes, bytes, bytes]:
    """A generated source and two queries that need real local-feature matching.

    Use Pillow, already an application dependency, only to make disposable test
    illustrations. Neither query contains a user photograph or downloaded image.
    The smoke driver deliberately does not import the application's search code.
    """
    from PIL import Image, ImageDraw, ImageFont

    randomizer = random.Random(701)
    width, height = 960, 1280
    source = Image.new("RGB", (width, height), (238, 229, 220))
    draw = ImageDraw.Draw(source)
    draw.polygon(((0, 0), (width * .56, 0), (width * .2, height), (0, height)),
                 fill=(45, 65, 91))
    draw.ellipse((width * .36, height * .08, width * 1.2, height * .83),
                 fill=(166, 83, 111))
    font = ImageFont.load_default(size=15)
    for row in range(16):
        for column in range(12):
            x = column * width // 12 + randomizer.randrange(5, 21)
            y = row * height // 16 + randomizer.randrange(5, 21)
            w, h = randomizer.randrange(32, 55), randomizer.randrange(30, 57)
            color = tuple(randomizer.randrange(20, 235) for _ in range(3))
            variant = randomizer.randrange(3)
            if variant == 0:
                draw.rectangle((x, y, x + w, y + h), fill=color, outline="white", width=2)
            elif variant == 1:
                draw.ellipse((x, y, x + w, y + h), fill=color, outline="black", width=3)
            else:
                draw.polygon(((x, y + h), (x + w // 3, y), (x + w, y + h // 3)),
                             fill=color, outline="white", width=2)
            draw.line((x + 4, y + h // 3, x + w - 4, y + h - 4),
                      fill=(250, 247, 235), width=randomizer.randrange(2, 5))
            draw.text((x, y + 5), f"{randomizer.randrange(1000):03}", font=font,
                      fill=(10, 10, 15), stroke_width=1, stroke_fill=(245, 245, 240))
            for _ in range(3):
                dx, dy = x + randomizer.randrange(w), y + randomizer.randrange(h)
                draw.line((dx, dy, dx + randomizer.randrange(5, 17),
                           dy + randomizer.randrange(4, 14)), fill="black", width=2)
    draw.text((32, height - 44), "Generated fixture 701", font=font,
              fill="white", stroke_width=2, stroke_fill="black")

    def encoded(image, format_name, quality=65):
        output = BytesIO()
        image.save(output, format_name, quality=quality)
        return output.getvalue()

    content = source.copy()
    content.thumbnail((660, 880), Image.Resampling.LANCZOS)
    screenshot = Image.new("RGB", (1000, 1120), (28, 27, 31))
    chrome = ImageDraw.Draw(screenshot)
    chrome.rectangle((0, 0, 1000, 42), fill=(244, 240, 243))
    chrome.rounded_rectangle((105, 7, 820, 35), radius=10, fill=(218, 215, 219))
    chrome.text((125, 14), "localhost / generated screenshot", fill=(60, 60, 65))
    chrome.rectangle((0, 42, 1000, 112), fill=(49, 45, 49))
    chrome.text((30, 70), "Fixture reader", fill=(240, 230, 237))
    screenshot.paste(content, ((1000 - content.width) // 2, 136))
    chrome.rounded_rectangle((72, 1054, 928, 1097), radius=14, fill=(72, 65, 72))
    chrome.line((118, 1075, 875, 1075), fill=(234, 80, 152), width=5)

    retained_area = .45
    crop_width, crop_height = round(width * math.sqrt(retained_area)), round(height * math.sqrt(retained_area))
    left, top = round((width - crop_width) * .22), round((height - crop_height) * .72)
    crop = source.crop((left, top, left + crop_width, top + crop_height))
    crop.thumbnail((900, 900), Image.Resampling.LANCZOS)
    return (encoded(source, "PNG"), encoded(screenshot, "JPEG", quality=68),
            encoded(crop, "JPEG", quality=57))


def sample_pdf() -> bytes:
    """Three generated pages; no personal file, network content, or PDF parser."""
    from PIL import Image, ImageDraw
    pages = []
    try:
        for index, color in enumerate(((234, 80, 152), (80, 120, 160), (160, 180, 90))):
            image = Image.new('RGB', (240, 360), color)
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 30, 220, 330), outline='white', width=8)
            draw.text((45, 130), f'Generated PDF page {index + 1}', fill='black')
            pages.append(image)
        output = BytesIO()
        pages[0].save(output, format='PDF', resolution=72, save_all=True, append_images=pages[1:])
        return output.getvalue()
    finally:
        for image in pages:
            image.close()


def sample_epub() -> bytes:
    """Three pictures across two spine documents, including an SVG wrapper."""
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml',
                        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                        '<rootfiles><rootfile full-path="OPS/book.opf" '
                        'media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('OPS/book.opf',
                        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                        '<dc:identifier id="id">generated-frozen-epub-smoke</dc:identifier>'
                        '<dc:title>Generated EPUB reader fixture</dc:title>'
                        '<dc:creator>Gravelle smoke fixture</dc:creator><dc:language>en</dc:language>'
                        '</metadata><manifest>'
                        '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                        '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
                        '<item id="green" href="green.png" media-type="image/png"/>'
                        '<item id="blue" href="blue.png" media-type="image/png"/>'
                        '</manifest><spine><itemref idref="second"/><itemref idref="first"/></spine></package>')
        archive.writestr('OPS/nav.xhtml',
                        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                        '<body><nav epub:type="toc"><ol><li><a href="first.xhtml#start">First chapter</a></li>'
                        '<li><a href="second.xhtml#start">Second chapter</a></li></ol></nav></body></html>')
        archive.writestr('OPS/cover.png', sample_png((214, 73, 139), (200, 300)))
        archive.writestr('OPS/green.png', sample_png((20, 160, 70), (180, 270)))
        archive.writestr('OPS/blue.png', sample_png((30, 60, 210), (220, 330)))
        for filename, title, target in (('first.xhtml', 'First chapter', 'second.xhtml'),
                                        ('second.xhtml', 'Second chapter', 'first.xhtml')):
            archive.writestr('OPS/' + filename,
                            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>' + title + '</title>'
                            '<script>smokeScriptMustBeRemoved()</script></head><body><h1 id="start">' + title + '</h1>'
                            '<p>Generated local EPUB reading text.</p>' +
                            ('<svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="blue.png"/></svg>'
                             if filename == 'second.xhtml' else '<img src="cover.png"/><img src="green.png"/>') +
                            '<a href="' + target + '#start">Next chapter</a></body></html>')
    return output.getvalue()


def random_port() -> int:
    return random_ports(1)[0]


def random_ports(count: int) -> list[int]:
    """Reserve all selected ports together so the service listeners differ."""
    sockets = []
    try:
        for _ in range(count):
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(candidate)
            candidate.bind(("127.0.0.1", 0))
        return [candidate.getsockname()[1] for candidate in sockets]
    finally:
        for candidate in sockets:
            candidate.close()


def ports_released(config: LauncherConfig) -> bool:
    """Check only the disposable listeners selected by this smoke run."""
    sockets = []
    try:
        for port in (config.port, config.share_port):
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(candidate)
            # Match the server's Unix restart policy without permitting a
            # second active listener. Windows keeps exclusive bind semantics.
            if sys.platform != 'win32':
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        for candidate in sockets:
            candidate.close()


class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))
        self.last_headers = {}

    def request(self, path: str, body=None, *, image=None, headers=None):
        request_headers = {"Origin": self.url, **(headers or {})}
        payload = None
        if image is not None:
            payload = image
            request_headers["Content-Type"] = "image/jpeg" if image.startswith(b"\xff\xd8") else "image/png"
        elif body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=payload, headers=request_headers)
        try:
            response = self.opener.open(request, timeout=65 if path == "/api/image-search/query" else 5)
        except HTTPError as error:
            response = error
        with response:
            self.last_headers = response.headers
            raw = response.read(8 * 1024 * 1024)
            content_type = response.headers.get_content_type()
            value = json.loads(raw) if content_type == "application/json" else raw
            return response.status, value, content_type


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def network_settings_smoke(client: Client, url: str, config: LauncherConfig,
                           book_id: str, expected_image: bytes) -> dict:
    """Probe the packaged service's boundaries without importing its policy.

    Runs after ordinary account/share checks because it intentionally leaves the
    disposable database protected by an entry and a separate share listener.
    All entry codes, passwords, tokens and cookies stay out of the report.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    status, settings, _ = client.request('/api/network-settings')
    require(status == 200 and settings.get('runtime') == 'desktop'
            and settings.get('share_port') == config.share_port
            and settings.get('security_entry_enabled') is False
            and settings.get('share_port_enabled') is False,
            'Frozen network settings did not use the launcher share port and defaults')
    for route, marker in (('/network-settings-ui.js', b'GravelleNetworkSettings'),
                          ('/network-settings-ui.css', b'.network-settings')):
        status, content, _ = client.request(route)
        require(status == 200 and marker in content,
                'Frozen network settings browser resource is missing: ' + route)
    require(Client(url).request('/api/network-settings')[0] == 401,
            'Frozen network settings must require an administrator')
    require(client.request('/api/network-settings', {'share_port': config.share_port + 1})[0] == 400,
            'Frozen web settings unexpectedly accepted a listener port change')

    code = secrets.token_urlsafe(18)
    payload = {'security_entry_enabled': True, 'security_entry_code': code,
               'share_port_enabled': True, 'public_share_url': 'https://share.example.com'}
    status, settings, _ = client.request('/api/network-settings', payload)
    require(status == 200 and settings.get('share_listening') is True
            and all(settings.get(key) == value for key, value in payload.items()),
            'Frozen service could not save entry/share settings or bind its second listener')
    cookie = client.last_headers.get('Set-Cookie', '')
    require('gravelle_entry=' in cookie and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie,
            'Frozen network save did not grant the administrator a protected entry cookie')
    require(client.request('/api/network-settings')[0] == 200,
            'Frozen network save locked its current administrator out')

    anonymous = Client(url)
    for route in ('/', '/index.html', '/api/session', '/api/network-settings'):
        require(anonymous.request(route)[0] == 404,
                'Frozen security entry leaked an ordinary route: ' + route)
    require(anonymous.request('/api/login', {'username': 'unused', 'password': 'unused'})[0] == 404,
            'Frozen security entry left the login endpoint reachable')
    require(anonymous.request('/api/health')[0] == 200,
            'Frozen security entry prevented the launcher health probe')
    status, html, mime = anonymous.request('/' + code)
    require(status == 200 and mime == 'text/html' and b'/network-settings-ui.js' in html,
            'Frozen configured entry did not serve the ordinary reader shell')
    require(anonymous.request('/api/network-settings')[0] == 401,
            'Frozen entry cookie bypassed account authentication')

    status, created, _ = client.request('/api/shares', {'book_id': book_id, 'mode': 'time', 'days': 1})
    require(status == 201 and isinstance(created.get('password'), str),
            'Frozen network administrator could not create the isolated share fixture')
    token = created.get('share', {}).get('token')
    require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token),
            'Frozen network share fixture returned no usable capability')
    page_path, base = '/share/' + token, '/api/share/' + token
    for route in (page_path, base, base + '/manifest', base + '/pages/0'):
        require(client.request(route)[0] == 404,
                'Frozen management listener still exposed a separate-port share')

    share_url = 'http://127.0.0.1:' + str(config.share_port)
    guest = Client(share_url)
    for route in ('/', '/index.html', '/api/health', '/api/session', '/api/network-settings',
                  '/api/library', '/app.js', '/network-settings-ui.js', '/' + code):
        require(guest.request(route)[0] == 404,
                'Frozen share listener exposed an ordinary route: ' + route)
    require(guest.request('/api/login', {'username': 'unused', 'password': 'unused'})[0] == 404,
            'Frozen share listener exposed account login')
    status, html, mime = guest.request(page_path)
    require(status == 200 and mime == 'text/html' and b'/reader-model.js' in html,
            'Frozen separate-port share did not serve the complete reader shell')
    # The exact dependencies of share.html are intentionally checked instead of
    # importing SHARE_STATIC: a stale policy must fail this independent probe.
    for route, marker in (('/styles.css', b'.reader'), ('/reader-model.js', b'SumiReaderModel'),
                          ('/share-reader.css', b'.share-reader'),
                          ('/share-reader.js', b'GravelleShareReader'),
                          ('/share-crypto.js', b'GravelleShareCrypto'),
                          ('/vendor/noble-gcm.js', b'GravelleNobleGCM'),
                          ('/gravelle-icon.png', b'\x89PNG\r\n\x1a\n'),
                          ('/gravelle-wordmark.svg', b'<svg')):
        status, content, _ = guest.request(route)
        require(status == 200 and marker in content,
                'Frozen isolated share reader resource is missing: ' + route)
    status, visitor, _ = guest.request(base + '/unlock', {'password': created['password']})
    require(status == 200 and isinstance(visitor.get('key'), str),
            'Frozen independent share listener could not unlock its fixture')
    status, packet, mime = guest.request(base + '/pages/0')
    require(status == 200 and mime == 'application/octet-stream' and packet[:4] == b'GSH1',
            'Frozen independent share listener returned no encrypted page')
    plaintext = AESGCM(base64.urlsafe_b64decode(visitor['key'] + '=')).decrypt(
        packet[4:16], packet[17:], packet[:17] + ('gravelle-share-v1:' + token + ':0').encode())
    require(plaintext == expected_image,
            'Frozen independent share page did not decrypt to the generated source')
    require(client.request('/api/shares/' + created['share']['id'] + '/close', {})[0] == 200
            and guest.request(base + '/pages/0')[0] == 410,
            'Frozen independent share remained readable after closure')
    return {'entry_gate_and_cookie': True, 'administrator_required': True,
            'web_port_changes_rejected': True, 'separate_listener_bound': True,
            'management_share_routes_closed': True, 'share_admin_routes_closed': True,
            'complete_share_reader_resources': True, 'independent_page_decrypted': True,
            'closed_share_rejected': True}


def network_recovery_smoke(executable: Path, data: Path) -> dict:
    """Exercise recovery in the frozen CLI, while its service is stopped."""
    def snapshot(database):
        with closing(sqlite3.connect(database)) as connection:
            return (json.loads(connection.execute('SELECT settings FROM network_settings WHERE id=1').fetchone()[0]),
                    connection.execute('SELECT COUNT(*) FROM owner_account').fetchone()[0],
                    connection.execute('SELECT COUNT(*) FROM shares').fetchone()[0])

    before, accounts, shares = snapshot(data / 'library.sqlite3')
    require(before['security_entry_enabled'] is True and before['share_port_enabled'] is True,
            'Frozen entry/share configuration was not persisted before recovery')
    backups = set((data / 'backups').glob('before-security-entry-reset-*.sqlite3'))
    child = subprocess.run([str(executable), '--reset-security-entry', '--data', str(data)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=45)
    try:
        output = child.stdout.decode('utf8')
    except UnicodeDecodeError:
        # The recovery CLI's localized message may use the Windows console
        # encoding; its ASCII JSON keys still require an exact successful CLI.
        output = child.stdout.decode(locale.getpreferredencoding(False))
    require(child.returncode == 0 and json.loads(output).get('ok') is True,
            'Frozen offline recovery CLI failed')
    after, restored_accounts, restored_shares = snapshot(data / 'library.sqlite3')
    require(after['security_entry_enabled'] is False and after['security_entry_code'] == ''
            and after['entry_epoch'] != before['entry_epoch']
            and after['share_port_enabled'] is True
            and after['public_share_url'] == before['public_share_url']
            and restored_accounts == accounts and restored_shares == shares,
            'Frozen recovery did not rotate the entry while preserving accounts and share isolation')
    added = set((data / 'backups').glob('before-security-entry-reset-*.sqlite3')) - backups
    require(len(added) == 1 and snapshot(next(iter(added))) == (before, accounts, shares),
            'Frozen entry recovery did not back up the exact original configuration and accounts')
    return {'offline_cli': True, 'backup_verified': True, 'entry_epoch_rotated': True,
            'share_isolation_preserved': True, 'accounts_and_shares_preserved': True}


def sharing_smoke(client: Client, url: str, book_id: str, expected_image: bytes) -> dict:
    """Decrypt a real service response using only the driver's external library.

    This intentionally does not import sharing.py or share_crypto.py. The frozen
    service must load its own native crypto and the driver checks its wire data.
    Credentials, capability tokens, and keys must never enter the smoke report.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    status, created, _ = client.request('/api/shares', {
        'book_id': book_id, 'mode': 'time', 'days': 3})
    require(status == 201 and isinstance(created.get('password'), str)
            and len(created['password']) >= 8, 'Frozen share creation did not return a generated access password')
    share = created.get('share', {})
    token, share_id = share.get('token'), share.get('id')
    require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token)
            and isinstance(share_id, str) and re.fullmatch(r'[a-f0-9]{32}', share_id),
            'Frozen share creation did not return usable share identifiers')
    require(share.get('mode') == 'time' and 'max_visits' not in share
            and isinstance(share.get('created_at'), (int, float))
            and isinstance(share.get('expires_at'), (int, float))
            and abs(share['expires_at'] - share['created_at'] - 3 * 86400) < 1,
            'Frozen share did not retain the three-day deadline')
    require(share.get('visits') == 0 and share.get('password_available') is True,
            'Frozen new share did not start at zero visits with a recoverable password')
    private_values = [created['password']]

    def listed_share(identifier, expected_visits):
        status, payload, _ = client.request('/api/shares')
        require(status == 200 and isinstance(payload.get('shares'), list),
                'Frozen administrator could not read share visit statistics')
        serialized = json.dumps(payload, ensure_ascii=False)
        require(not any(value in serialized for value in private_values),
                'Frozen share listing disclosed an access password or visitor key')
        forbidden = {'password', 'password_hash', 'password_encrypted', 'encryption_key', 'session', 'session_hash', 'key', 'master_key'}
        for row in payload['shares']:
            require(not forbidden.intersection(row),
                    'Frozen share listing disclosed private credential fields')
        row = next((row for row in payload['shares'] if row.get('id') == identifier), None)
        require(row is not None and type(row.get('visits')) is int and row['visits'] == expected_visits,
                'Frozen share visit statistics counted an unsuccessful or reused session incorrectly')
        require(row.get('password_available') is True,
                'Frozen share password could not be made available to its creator')
        return row

    def reveal(identifier, password):
        status, payload, _ = client.request('/api/shares/' + identifier + '/password', {})
        require(status == 200 and payload == {'password': password},
                'Frozen share creator could not reveal the saved access password')
        require('no-store' in client.last_headers.get('Cache-Control', '').lower(),
                'Frozen revealed share passwords must not be cached')

    listed_share(share_id, 0)
    reveal(share_id, created['password'])
    listed_share(share_id, 0)
    require(client.request('/api/shares/' + share_id + '/delete', {})[0] == 409,
            'Frozen share allowed deletion of an active share record')
    base = '/api/share/' + token
    page_route = base + '/pages/0'
    guest = Client(url)
    require(guest.request(page_route)[0] == 401
            and guest.request(base + '/manifest')[0] == 401,
            'Frozen share exposed a page or its manifest before password verification')
    status, shell, mime = guest.request('/share/' + token)
    require(status == 200 and mime == 'text/html' and b'/share-reader.js' in shell
            and b'/app.js' not in shell and guest.last_headers.get('Cache-Control') == 'no-store',
            'Frozen anonymous share shell is missing or exposes the signed-in application')
    for route, expected_mime, marker in (
            ('/share-reader.js', 'text/javascript', b'GravelleShareReader'),
            ('/share-reader.css', 'text/css', b'.share-reader'),
            ('/share-crypto.js', 'text/javascript', b'GravelleShareCrypto'),
            ('/vendor/noble-gcm.js', 'text/javascript', b'GravelleNobleGCM'),
            ('/vendor/noble-ciphers-LICENSE.txt', 'text/plain', b'Paul Miller')):
        status, resource, mime = guest.request(route)
        require(status == 200 and mime == expected_mime and marker in resource,
                'Frozen anonymous share is missing a local browser component or notice: ' + route)
    require(guest.request(base + '/unlock', {'password': 'wrong-disposable-password'})[0] == 403,
            'Frozen share accepted an incorrect access password')
    listed_share(share_id, 0)
    status, visitor, _ = guest.request(base + '/unlock', {'password': created['password']})
    require(status == 200 and isinstance(visitor.get('key'), str)
            and re.fullmatch(r'[A-Za-z0-9_-]{43}', visitor['key'])
            and visitor.get('manifest', {}).get('page_count', 0) > 0,
            'Frozen guest could not unlock the single shared work')
    cookie = guest.last_headers.get('Set-Cookie', '')
    require('HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Path=' + base in cookie,
            'Frozen guest session cookie did not retain its isolated share scope')
    private_values.append(visitor['key'])
    listed_share(share_id, 1)
    require(guest.request('/api/library')[0] == 401 and guest.request('/api/shares')[0] == 401
            and guest.request(f'/api/books/{book_id}/pages/0')[0] == 401
            and guest.request('/api/shares/' + share_id + '/password', {})[0] == 401,
            'Frozen guest cookie incorrectly authenticated a normal account route')
    status, reused, _ = guest.request(base + '/unlock', {'password': created['password']})
    require(status == 200 and reused.get('key') == visitor['key'],
            'Frozen same-cookie unlock did not reuse its existing valid visitor session')
    listed_share(share_id, 1)
    status, packet, mime = guest.request(page_route)
    require(status == 200 and mime == 'application/octet-stream' and isinstance(packet, bytes)
            and packet.startswith(b'GSH1') and len(packet) == len(expected_image) + 33
            and packet[16] == 2 and expected_image not in packet,
            'Frozen share did not return the authenticated PNG ciphertext envelope')
    require('no-store' in guest.last_headers.get('Cache-Control', '').lower().split(', ')
            and not guest.last_headers.get('ETag'),
            'Frozen shared images must not be cached or bypass authorization through an ETag')
    key = base64.urlsafe_b64decode(visitor['key'] + '=')
    plaintext = AESGCM(key).decrypt(packet[4:16], packet[17:], packet[:17] +
                                   ('gravelle-share-v1:' + token + ':0').encode('utf-8'))
    require(plaintext == expected_image,
            'Frozen AES-GCM response did not decrypt to the original generated page bytes')
    status, second_packet, _ = guest.request(page_route)
    require(status == 200 and second_packet != packet and second_packet[4:16] != packet[4:16],
            'Frozen share reused the same nonce/ciphertext for a later image response')
    require(guest.request(base + '/manifest')[0] == 200,
            'Frozen share visitor could not refresh its existing manifest')
    listed_share(share_id, 1)
    fresh_guest = Client(url)
    require(fresh_guest.request(page_route)[0] == 401
            and fresh_guest.request(base + '/unlock', {'password': created['password']})[0] == 200,
            'Frozen time share did not isolate image access or allow a new authorized visitor')
    listed_share(share_id, 2)
    require(guest.request(base + '/manifest')[0] == 200,
            'Frozen time share discarded an existing valid session')
    # Keep the old cookie independently: clearing a browser cookie alone is not
    # enough, because the service must also invalidate the old server session.
    saved_cookie = cookie.split(';', 1)[0]
    status, logged_out, _ = guest.request(base + '/logout', {})
    expired_cookie = guest.last_headers.get('Set-Cookie', '')
    require(status == 200 and logged_out == {'logged_out': True}
            and 'Max-Age=0' in expired_cookie and 'Path=' + base in expired_cookie
            and 'HttpOnly' in expired_cookie and 'SameSite=Strict' in expired_cookie
            and 'no-store' in guest.last_headers.get('Cache-Control', '').lower(),
            'Frozen guest logout did not clear its isolated session cookie')
    require(guest.request(base + '/manifest')[0] == 401
            and guest.request(page_route)[0] == 401,
            'Frozen logged-out guest still returned to its reader without a password')
    replay = Client(url)
    require(replay.request(base + '/manifest', headers={'Cookie': saved_cookie})[0] == 401
            and replay.request(page_route, headers={'Cookie': saved_cookie})[0] == 401,
            'Frozen logout left the old server session usable through its saved cookie')
    require(guest.request(base + '/logout', {})[0] == 200,
            'Frozen guest logout was not idempotent')
    listed_share(share_id, 2)
    require(fresh_guest.request(base + '/manifest')[0] == 200
            and fresh_guest.request(page_route)[0] == 200,
            'Frozen guest logout interrupted another visitor session')
    require(guest.request(base + '/unlock', {'password': 'wrong-disposable-password'})[0] == 403
            and guest.request(base + '/manifest')[0] == 401,
            'Frozen logged-out guest recovered access with an incorrect password')
    listed_share(share_id, 2)
    status, returned, _ = guest.request(base + '/unlock', {'password': created['password']})
    require(status == 200 and isinstance(returned.get('key'), str)
            and returned['key'] != visitor['key']
            and guest.request(base + '/manifest')[0] == 200 and guest.request(page_route)[0] == 200,
            'Frozen guest could not establish a fresh session after logout')
    private_values.append(returned['key'])
    listed_share(share_id, 3)
    require(fresh_guest.request(base + '/manifest')[0] == 200,
            'Frozen guest return interrupted another visitor session')
    require(client.request('/api/shares/' + share_id + '/close', {})[0] == 200,
            'Frozen administrator could not close the share')
    require(guest.request(page_route, headers={'If-None-Match': '*'})[0] == 410
            and guest.request(base + '/manifest')[0] == 410,
            'Frozen closed share still allowed an old guest to read images or the manifest')
    reveal(share_id, created['password'])
    listed_share(share_id, 3)

    # Sharing the same work again creates a separate capability. Closing or
    # deleting the old record must neither revive it nor close the new one.
    status, recreated, _ = client.request('/api/shares', {
        'book_id': book_id, 'mode': 'time', 'days': 3})
    renewed = recreated.get('share', {})
    new_token, new_id = renewed.get('token'), renewed.get('id')
    require(status == 201 and isinstance(new_token, str)
            and re.fullmatch(r'[A-Za-z0-9_-]{43}', new_token) and new_token != token
            and isinstance(new_id, str) and re.fullmatch(r'[a-f0-9]{32}', new_id) and new_id != share_id
            and isinstance(recreated.get('password'), str) and len(recreated['password']) >= 8
            and recreated['password'] != created['password'],
            'Frozen share recreation reused an old link, identifier, or generated password')
    private_values.append(recreated['password'])
    require(renewed.get('visits') == 0 and renewed.get('password_available') is True,
            'Frozen recreated share inherited visits or lost its separately saved password')
    listed_share(new_id, 0)
    reveal(new_id, recreated['password'])
    new_base = '/api/share/' + new_token
    renewed_guest = Client(url)
    status, new_visitor, _ = renewed_guest.request(new_base + '/unlock', {'password': recreated['password']})
    require(status == 200 and isinstance(new_visitor.get('key'), str)
            and new_visitor['key'] != visitor['key'],
            'Frozen recreated share could not issue an independent visitor session')
    private_values.append(new_visitor['key'])
    listed_share(new_id, 1)
    listed_share(share_id, 3)
    require(guest.request(page_route)[0] == 410,
            'Frozen share recreation incorrectly restored the old closed link')
    require(client.request('/api/shares/' + share_id + '/delete', {})[0] == 200,
            'Frozen administrator could not delete the closed share record')
    require(guest.request(page_route)[0] == 404 and guest.request(base)[0] == 404,
            'Frozen deleted share record still resolved through its old link')
    require(client.request('/api/shares/' + share_id + '/password', {})[0] == 404,
            'Frozen deleted share still exposed its stored access password')
    require(renewed_guest.request(new_base + '/manifest')[0] == 200
            and renewed_guest.request(new_base + '/pages/0')[0] == 200,
            'Frozen old-record deletion interrupted the separately recreated share')
    listed_share(new_id, 1)
    reveal(new_id, recreated['password'])
    return {'ciphertext_bytes': len(packet), 'plaintext_bytes': len(plaintext),
            'cipher': 'AES-256-GCM', 'authenticated_header': True,
            'valid_days': 3, 'copied_url_requires_session': True, 'closed_images_rejected': True,
            'reshare_uses_new_capability': True, 'active_delete_rejected': True,
            'closed_record_deleted': True, 'new_share_remains_readable': True,
            'creator_password_reveal': True, 'listing_excludes_credentials': True,
            'visits_count_new_sessions_only': True, 'independent_visit_counts': True,
            'logout_revokes_server_session': True, 'logout_preserves_other_visitors': True,
            'logout_requires_password_again': True, 'logout_preserves_visit_statistics': True}


def run_smoke(executable: Path, work_root: Path) -> dict:
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "executable": str(executable), "work_root": str(work_root), "checks": []}
    started = time.monotonic()
    controller = None
    log_path = None

    def passed(name, **details):
        report["checks"].append({"name": name, "status": "passed", **details})

    with tempfile.TemporaryDirectory(prefix="gravelle-frozen-smoke-", dir=work_root) as temporary:
        test_root = Path(temporary).resolve()
        require(test_root.parent == work_root and test_root.name.startswith("gravelle-frozen-smoke-"),
                "The temporary directory escaped the explicitly selected work root")
        media = test_root / "media"
        book_folder = media / "几何样例" / "2026-10-11 几何图形"
        book_folder.mkdir(parents=True)
        image = sample_png()
        source_image = book_folder / "001.png"
        source_image.write_bytes(image)
        source_digest = hashlib.sha256(image).hexdigest()
        second_media = test_root / "第二根目录"
        second_folder = second_media / "几何样例" / "2026-10-11 几何图形"
        second_folder.mkdir(parents=True)
        second_image = sample_png((80, 120, 160))
        second_source = second_folder / "001.png"
        second_source.write_bytes(second_image)
        data = test_root / "data"
        log_path = test_root / "server.log"
        port, share_port = random_ports(2)
        config = LauncherConfig(port=port, share_port=share_port,
                                library_paths=[str(media), str(second_media)])
        report["port"] = config.port
        controller = ServerController(data, log_path, command_prefix=[str(executable)])
        try:
            url = controller.start(config, timeout=35)
            passed("private_ready_handshake", local_url=url)
            client = Client(url)
            status, health, _ = client.request("/api/health")
            require(status == 200 and health.get("status") == "ok" and health.get("version"),
                    "Frozen health endpoint did not return the reader version")
            report["version"] = health["version"]
            passed("health", version=health["version"])

            status, index, mime = client.request("/index.html")
            require(status == 200 and mime == "text/html" and b'id="check-updates"' in index,
                    "Frozen static/index.html is missing the update-check button")
            require(b"gravelle-wordmark.svg" in index and b"gravelle-icon.png" in index,
                    "Frozen HTML does not reference the supplied branding")
            passed("packaged_html_and_update_button")
            for route, expected_mime, signature in (("/gravelle-wordmark.svg", "image/svg+xml", b"<svg"),
                                                     ("/gravelle-icon.png", "image/png", b"\x89PNG\r\n\x1a\n")):
                status, content, mime = client.request(route)
                require(status == 200 and mime == expected_mime and signature in content[:1024],
                        f"Frozen branding resource failed: {route}")
                passed("packaged_asset", route=route, bytes=len(content))
            status, _, _ = client.request("/api/libraries")
            require(status == 401, "Library data must require authentication")
            passed("unauthenticated_libraries_401")

            credentials = {"username": "frozen-smoke", "password": "disposable-frozen-smoke-only"}
            status, account, _ = client.request("/api/register", credentials)
            require(status == 201 and account.get("authenticated"), "Disposable registration failed")
            status, _, _ = client.request("/api/logout", {})
            require(status == 200, "Disposable logout failed")
            status, account, _ = client.request("/api/login", credentials)
            require(status == 200 and account.get("authenticated"), "Disposable login failed")
            passed("registration_logout_login")

            status, roots, _ = client.request("/api/directories")
            require(status == 200 and roots.get("root_paths") == [str(media), str(second_media)],
                    "Frozen launcher did not pass both allowed roots")
            passed("multiple_allowed_roots")
            status, helper, _ = client.request("/batch-selection.js")
            require(status == 200 and b"GravelleBatchSelection" in helper,
                    "Frozen bundle is missing the batch-selection frontend module")
            passed("packaged_batch_selection_module")
            status, helper, _ = client.request("/page-router.js")
            require(status == 200 and b"GravellePageRouter" in helper,
                    "Frozen bundle is missing the page-router frontend module")
            passed("packaged_page_router_module")
            status, helper, mime = client.request("/cache-management-ui.js")
            require(status == 200 and mime == "text/javascript" and b"GravelleCacheManagementUI" in helper
                    and b'/cache-management-ui.js' in index,
                    "Frozen bundle is missing the cache-management frontend module/reference")
            passed("packaged_cache_management_module")
            status, cache, _ = client.request("/api/cache")
            kinds = {item["kind"] for item in cache.get("categories", [])}
            require(status == 200 and cache.get("settings", {}).get("cover_limit_mb") == 512
                    and {"covers", "library_covers", "optimized"} <= kinds,
                    "Frozen administrator cache inventory/default budget failed")
            require(Client(url).request("/api/cache")[0] == 401, "Cache inventory must require a login")
            status, changed_cache, _ = client.request("/api/cache/settings", {
                "expected_revision": cache["settings"]["revision"], "cover_limit_mb": 64})
            require(status == 200 and changed_cache["settings"]["cover_limit_mb"] == 64,
                    "Frozen cover cache budget did not persist")
            status, _, _ = client.request("/api/cache/settings", {
                "expected_revision": changed_cache["settings"]["revision"], "cover_limit_mb": 512})
            require(status == 200, "Disposable cache budget restoration failed")
            passed("frozen_cache_inventory_settings_and_login")

            status, result, _ = client.request("/api/libraries", {
                "name": "冻结包几何样例", "paths": [str(media)], "media_type": "photo",
                "reading_mode": "single", "reading_direction": "ltr", "hide_date_prefix": True})
            require(status == 201 and result.get("library", {}).get("id"), "Disposable photo library creation failed")
            library_id = result["library"]["id"]
            deadline = time.monotonic() + 20
            listing = None
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(0.1)
            require(status == 200 and not listing.get("scanning") and len(listing.get("books", [])) == 1,
                    "Frozen scanner did not catalogue exactly one disposable book")
            book = listing["books"][0]
            require(book.get("page_count") == 1 and book.get("title") == "几何图形",
                    "Photo page counting or date-prefix processing failed")
            status, page, mime = client.request(f"/api/books/{book['id']}/pages/0")
            require(status == 200 and mime == "image/png" and page == image,
                    "Frozen reader did not serve the original geometric sample page")
            status, cover, mime = client.request(f"/api/books/{book['id']}/cover")
            require(status == 200 and mime == "image/jpeg" and cover.startswith(b"\xff\xd8"),
                    "Frozen Pillow did not generate a JPEG cover")
            require(hashlib.sha256(source_image.read_bytes()).hexdigest() == source_digest,
                    "The smoke scan modified its source image")
            passed("photo_library_scan_page_and_cover", books=1, pages=1)

            status, created, _ = client.request("/api/libraries", {
                "name": "冻结包第二根目录", "paths": [str(second_media)], "media_type": "photo"})
            require(status == 201, "Frozen service rejected a library in its second root")
            second_library_id = created["library"]["id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + second_library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(.1)
            require(status == 200 and not listing.get("scanning") and len(listing.get("books", [])) == 1,
                    "Frozen scanner failed in the second root")
            second_book = listing["books"][0]
            require(second_book["id"] != book["id"], "Same-named folders across roots aliased a book ID")
            status, page, _ = client.request(f"/api/books/{second_book['id']}/pages/0")
            require(status == 200 and page == second_image, "Frozen reader served the wrong root's page")
            passed("second_root_scan_and_original_page")

            # Exercise only the two existing generated books. One-book libraries
            # may regenerate the same visual group but must advance its version.
            def wait_library_cover(target_id):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    status, libraries, _ = client.request("/api/libraries")
                    item = next((item for item in libraries.get("libraries", []) if item["id"] == target_id), None)
                    cover_state = item.get("cover", {}) if item else {}
                    require(status == 200 and not cover_state.get("error"), "Frozen montage task failed")
                    if cover_state.get("count") == 1 and cover_state.get("pending") is False:
                        return cover_state
                    time.sleep(.1)
                raise AssertionError("Frozen montage background task did not become ready")

            montage_state = wait_library_cover(library_id)
            cover_route = f"/api/libraries/{library_id}/cover"
            require(Client(url).request(cover_route)[0] == 401, "Library montage must require authentication")
            status, montage, mime = client.request(cover_route)
            require(status == 200 and mime == "image/jpeg" and montage.startswith(b"\xff\xd8"),
                    "Frozen library montage did not serve a JPEG")
            from PIL import Image
            with Image.open(BytesIO(montage)) as decoded:
                require(decoded.size == (960, 480), "Frozen montage cache has an unexpected size")
            montage_etag = client.last_headers.get("ETag")
            require(montage_etag and client.request(cover_route, headers={"If-None-Match": montage_etag})[0] == 304,
                    "Frozen montage conditional request did not return 304")
            status, shuffled, _ = client.request(cover_route + "/shuffle", {})
            require(status == 200 and shuffled.get("version") != montage_state["version"],
                    "Frozen administrator cover shuffle did not queue a new generation")
            require(wait_library_cover(library_id)["version"] != montage_state["version"],
                    "Frozen montage shuffle did not publish its new generation")
            passed("frozen_library_montage_cache_shuffle_and_304", selected=1, width=960, height=480)

            child_credentials = {"username": "frozen-smoke-child", "password": "disposable-child-smoke-only"}
            status, created_child, _ = client.request("/api/users", {
                **child_credentials, "enabled": True, "library_ids": [library_id]})
            require(status == 201 and created_child.get("user", {}).get("id"), "Disposable child creation failed")
            child_id = created_child["user"]["id"]
            child_client = Client(url)
            require(child_client.request("/api/login", child_credentials)[0] == 200, "Disposable child login failed")
            require(child_client.request(cover_route)[0] == 200, "Authorized child could not view library montage")
            child_etag = child_client.last_headers.get("ETag")
            require(child_client.request(cover_route + "/shuffle", {})[0] == 403,
                    "Child must not shuffle shared library covers")
            require(child_client.request("/api/cache")[0] == 403, "Child must not view administrator cache paths")
            require(child_client.request("/api/my-libraries", {"visible_library_ids": []})[0] == 200,
                    "Disposable child could not hide its granted library")
            require(child_client.request(cover_route, headers={"If-None-Match": child_etag})[0] == 404,
                    "Hidden library montage must not return cached data or 304")
            require(child_client.request("/api/my-libraries", {"visible_library_ids": [library_id]})[0] == 200
                    and child_client.request(cover_route)[0] == 200,
                    "Disposable child library visibility restoration failed")
            require(client.request(f"/api/users/{child_id}", {"library_ids": []})[0] == 200,
                    "Disposable child grant revocation failed")
            require(child_client.request(cover_route, headers={"If-None-Match": child_etag})[0] in (401, 404),
                    "Revoked library montage must not return cached data or 304")
            require(child_client.request("/api/login", child_credentials)[0] == 200
                    and child_client.request(cover_route, headers={"If-None-Match": child_etag})[0] == 404,
                    "Relogged child must not regain revoked montage access")
            passed("frozen_montage_child_hidden_revoked_and_admin_boundaries")

            require(client.request("/api/cache/clear", {"kind": "covers", "confirm": False})[0] == 400,
                    "Cache removal must require explicit confirmation")
            status, cleared_cache, _ = client.request("/api/cache/clear", {"kind": "covers", "confirm": True})
            require(status == 200 and cleared_cache.get("cleared", {}).get("kind") == "covers"
                    and cleared_cache["cleared"].get("files", 0) >= 1,
                    "Frozen disposable page-cover cleanup did not remove generated thumbnails")
            require(client.request(f"/api/books/{book['id']}/cover")[0] == 200
                    and source_image.read_bytes() == image and second_source.read_bytes() == second_image,
                    "Frozen page-cover cleanup could not regenerate safely")
            passed("frozen_cover_cache_confirmation_clear_and_regeneration")

            model_ids = []
            for name in ("冻结演示模特甲", "冻结演示模特乙"):
                status, created, _ = client.request("/api/models", {"name": name})
                require(status == 201 and created.get("model", {}).get("id"), "Frozen model creation failed")
                model_ids.append(created["model"]["id"])
            ids = [book["id"], second_book["id"]]
            status, changed, _ = client.request("/api/books/batch", {
                "action": "models", "ids": ids, "model_ids": model_ids})
            require(status == 200 and changed.get("updated") == ids, "Frozen batch model replacement failed")
            for book_id in ids:
                status, detail, _ = client.request(f"/api/books/{book_id}")
                require(status == 200 and [item["id"] for item in detail["models"]] == model_ids,
                        "Frozen batch model associations were not persisted")
            require(source_image.read_bytes() == image and second_source.read_bytes() == second_image,
                    "Multi-root or model operations changed original files")
            passed("batch_model_replacement_across_roots")

            status, helper, _ = client.request("/transcoding-ui.js")
            require(status == 200 and b"GravelleTranscodingUI" in helper,
                    "Frozen bundle is missing the transcoding frontend module")
            passed("packaged_transcoding_module")
            tiny = sample_png((80, 100, 130), (1, 1))
            tiny_source = book_folder / "002.png"
            tiny_source.write_bytes(tiny)
            status, _, _ = client.request("/api/scan", {"library_id": library_id})
            require(status in (200, 202), "Disposable rescan failed")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(.1)
            require(listing["books"][0]["page_count"] == 2, "Frozen rescan missed the unmatched tiny page")
            status, snapshot, _ = client.request("/api/transcoding")
            require(status == 200 and snapshot["rules"]["enabled"] is False,
                    "Transcoding must stay disabled until the administrator enables it")
            rules = {**snapshot["rules"], "enabled": True, "file_size_mb": 0,
                     "max_source_pixels": 3, "max_source_side": 0,
                     "max_output_side": 512, "min_free_space_mb": 0}
            status, impact, _ = client.request("/api/transcoding/rules/preview", {"rules": rules})
            require(status == 200 and impact["totals"]["newly_matched"] == 2,
                    "Frozen rule preview must count individual matched pages")
            status, snapshot, _ = client.request("/api/transcoding/rules", {
                "rules": rules, "expected_revision": snapshot["revision"]})
            require(status == 200 and snapshot["summary"]["pending_books"] == 2,
                    "Frozen rule save did not create persistent pending records")
            status, reminder, _ = client.request("/api/transcoding/summary")
            require(status == 200 and reminder["summary"]["pending_books"] == 2
                    and len(reminder["pending_book_ids"]) == 2
                    and book["id"] in reminder["pending_book_ids"]
                    and isinstance(reminder["scanning"], bool)
                    and len(reminder["pending_records"]) == 2,
                    "Frozen reminder summary must report consistent pending IDs and count")
            passed("frozen_lightweight_optimization_reminders")
            require(client.request(f"/api/books/{book['id']}")[0] == 404,
                    "Undecided work must be excluded from ordinary detail routes")
            require(client.request(f"/api/transcoding/books/{book['id']}/pages/0")[1] == image,
                    "Administrator preview must retain the untouched source")
            passed("transcoding_rule_preview_and_pending_visibility")
            status, snapshot, _ = client.request("/api/transcoding/actions", {
                "ids": [book["id"]], "action": "transcode"})
            require(status == 200, "Frozen transcoding start failed")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, snapshot, _ = client.request("/api/transcoding")
                record = next(item for item in snapshot["books"] if item["id"] == book["id"])
                if record["status"] in ("ready", "failed", "paused"):
                    break
                time.sleep(.1)
            require(record["status"] == "ready", f"Frozen image worker failed: {record.get('error')}")
            require(record["matched_count"] == record["output_count"] == 1 and record["actual_bytes"] > 0,
                    "Frozen worker must only create the one matched page")
            status, optimized, mime = client.request(f"/api/books/{book['id']}/pages/0")
            require(status == 200 and mime == "image/webp" and optimized != image,
                    "Frozen Pillow/worker did not publish a WebP derivative")
            require(client.request(f"/api/books/{book['id']}/pages/1")[1] == tiny,
                    "Frozen worker changed an unmatched source page")
            require(source_image.read_bytes() == image and tiny_source.read_bytes() == tiny,
                    "Frozen transcoding changed source files")
            passed("frozen_isolated_transcode_worker", matched=1, outputs=record["output_count"],
                   actual_bytes=record["actual_bytes"])
            details_route = f"/api/transcoding/books/{book['id']}/details"
            status, details, _ = client.request(details_route + "?page=1&page_size=1")
            require(status == 200 and details.get("pagination") == {"page": 1, "page_size": 1, "total": 2, "pages": 2},
                    "Frozen optimization details pagination failed")
            active_page = details["pages"][0]
            require(details["book"]["pending_count"] == 0 and details["book"]["completed_at"] > 0
                    and details["target_parameters"]["quality"] == rules["quality"]
                    and details["target_parameters"]["rule_revision"] == details["target_rule_revision"]
                    and len(details["active_profiles"]) == 1
                    and details["active_profiles"][0]["parameters"]["quality"] == rules["quality"]
                    and active_page["active_parameters"]["quality"] == rules["quality"]
                    and active_page["active_created_at"] > 0 and active_page["output_created_at"] > 0
                    and active_page["original_path"] == str(source_image)
                    and active_page["active_path"].startswith(str(data / "transcoded"))
                    and active_page["active_bytes"] == len(optimized),
                    "Frozen completed-output parameters, dates or per-image paths are missing")
            status, unmatched_details, _ = client.request(details_route + "?page=2&page_size=1")
            require(status == 200 and unmatched_details["pages"][0]["status"] == "unmatched"
                    and unmatched_details["pages"][0]["original_path"] == str(tiny_source),
                    "Frozen optimization details missed the unmatched original page")
            require(child_client.request(details_route)[0] == 403,
                    "Child must not read administrator optimization paths")
            passed("frozen_completed_optimization_details_parameters_dates_and_paths", total_pages=2,
                   quality=rules["quality"], target_rule_revision=details["target_rule_revision"])
            status, snapshot, _ = client.request("/api/transcoding/actions", {
                "ids": [second_book["id"]], "action": "original"})
            require(status == 200 and any(item["id"] == second_book["id"] and item["status"] == "original"
                                         for item in snapshot["books"]),
                    "Original choice must remain in the management list")
            require(client.request(f"/api/books/{second_book['id']}/pages/0")[1] == second_image,
                    "Frozen original choice did not serve original bytes")
            status, cleared, _ = client.request("/api/transcoding/cleanup", {})
            require(status == 200 and cleared["cleanup"]["removed_files"] == 0,
                    "Cleanup must preserve every active derivative")
            require(client.request(f"/api/books/{book['id']}/pages/0")[1] == optimized,
                    "Cleanup removed the active reading image")
            passed("frozen_original_choice_and_active_cache_protection")

            # The preceding optimization test intentionally matches every page
            # larger than three pixels. Choose original output before adding the
            # detailed source, so rescanning cannot start an extra transcode job.
            status, _, _ = client.request("/api/transcoding/actions", {
                "ids": [book["id"]], "action": "original"})
            require(status == 200, "Frozen original choice failed before adding the detailed page")
            detailed, screenshot_query, cropped_query = screenshot_and_crop_fixtures()
            detailed_source = book_folder / "003.png"
            detailed_source.write_bytes(detailed)
            detailed_digest = hashlib.sha256(detailed).hexdigest()
            status, _, _ = client.request("/api/scan", {"library_id": library_id})
            require(status in (200, 202), "Disposable detailed-page rescan failed")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(.1)
            require(status == 200 and not listing.get("scanning"),
                    "Frozen rescan of the detailed image did not finish")
            status, detail, _ = client.request(f"/api/books/{book['id']}")
            require(status == 200 and detail.get("page_count") == 3,
                    "Frozen rescan missed the detailed third source page")

            status, helper, _ = client.request("/image-search-ui.js")
            require(status == 200 and b"GravelleImageSearchUI" in helper,
                    "Frozen bundle is missing the image-search module")
            require(client.request("/api/image-search/status")[1]["enabled"] is False,
                    "Image search must remain disabled until the administrator enables it")
            require(client.request("/api/image-search/settings", {"enabled": True})[0] == 200,
                    "Frozen image-search enable failed")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, search_status, _ = client.request("/api/image-search/status")
                summary = search_status.get("summary", {})
                if summary.get("total", 0) >= 3 and summary.get("pending") == 0 and not search_status["indexing"]:
                    break
                time.sleep(.1)
            require(summary.get("indexed") == summary.get("total") and summary.get("total", 0) >= 3,
                    f"Frozen image-search index worker failed: {search_status.get('error')}")
            for failure_page in (1, 2):
                status, failures, _ = client.request(f"/api/image-search/failures?page={failure_page}&page_size=1")
                require(status == 200 and failures == {"items": [], "total": 0, "page": failure_page, "page_size": 1},
                        "Frozen empty index-failure pagination was not readable")
            require(client.request("/api/image-search/failures?page=0")[0] == 400,
                    "Frozen index failure pagination must reject page zero")
            require(child_client.request("/api/image-search/failures")[0] == 403,
                    "Child must not read shared index failure records")
            passed("frozen_empty_index_failure_pagination_and_admin_boundary")
            status, matches, _ = client.request("/api/image-search/query", image=image)
            match = next((item for item in matches.get("results", [])
                          if item["book"]["id"] == book["id"] and item["page"] == 0), None)
            require(status == 200 and match and match["match"] == "exact",
                    "Frozen stdin image-query worker did not find the exact source page")
            require("source_root" not in match["book"] and "path" not in match["book"],
                    "Image-search results must not expose source directories")
            status, preview, mime = client.request(f"/api/image-search/thumbnail/{book['id']}/0")
            require(status == 200 and mime == "image/jpeg" and preview.startswith(b"\xff\xd8"),
                    "Frozen search thumbnail did not return a bounded preview")
            require(source_image.read_bytes() == image and tiny_source.read_bytes() == tiny
                    and hashlib.sha256(detailed_source.read_bytes()).hexdigest() == detailed_digest,
                    "Image indexing modified original files")
            passed("frozen_isolated_image_search_workers", indexed=summary["indexed"],
                   match_page=match["page"], preview_bytes=len(preview))

            for name, query, details in (
                    ("frozen_full_screenshot_geometry_match", screenshot_query,
                     {"browser_chrome": True, "query_format": "JPEG"}),
                    ("frozen_medium_crop_geometry_match", cropped_query,
                     {"retained_source_area": .45, "query_format": "JPEG", "jpeg_quality": 57})):
                status, matches, _ = client.request("/api/image-search/query", image=query)
                results = matches.get("results", [])
                require(status == 200 and results and results[0]["book"]["id"] == book["id"]
                        and results[0]["page"] == 2 and results[0]["match"] == "similar",
                        f"Frozen local-feature worker did not find the third source page: {name}")
                require("source_root" not in results[0]["book"] and "path" not in results[0]["book"],
                        "Screenshot/crop results must not expose source directories")
                require(source_image.read_bytes() == image and tiny_source.read_bytes() == tiny
                        and second_source.read_bytes() == second_image
                        and hashlib.sha256(detailed_source.read_bytes()).hexdigest() == detailed_digest,
                        "Screenshot/crop search modified an original source file")
                passed(name, match_page=results[0]["page"], query_bytes=len(query), **details)

            # Keep PDFs separate until the existing exact-count photo/search
            # checks finish. The frozen executable alone must render these pages;
            # the smoke driver's Pillow is used only to generate this fixture.
            pdf_folder = second_media / 'PDF 测试'
            pdf_folder.mkdir()
            pdf_source = pdf_folder / '分页样例.pdf'
            pdf_bytes = sample_pdf()
            pdf_source.write_bytes(pdf_bytes)
            pdf_digest = hashlib.sha256(pdf_bytes).hexdigest()
            status, created, _ = client.request('/api/libraries', {
                'name': '冻结包 PDF 样例', 'paths': [str(pdf_folder)], 'media_type': 'photo'})
            require(status == 201 and created.get('library', {}).get('id'), 'Frozen PDF library creation failed')
            pdf_library_id = created['library']['id']
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                status, pdf_listing, _ = client.request('/api/library?library_id=' + pdf_library_id)
                if status == 200 and not pdf_listing.get('scanning'):
                    break
                time.sleep(.1)
            require(status == 200 and not pdf_listing.get('scanning') and len(pdf_listing.get('books', [])) == 1,
                    'Frozen scanner did not catalogue the disposable PDF')
            pdf_book = pdf_listing['books'][0]
            require(pdf_book.get('format') == 'pdf' and pdf_book.get('page_count') == 3,
                    'Frozen isolated PDF probe did not report its three pages')
            status, pdf_detail, _ = client.request(f"/api/books/{pdf_book['id']}")
            require(status == 200 and len(pdf_detail.get('page_sizes', [])) == 3
                    and len(pdf_detail.get('page_revision', '')) == 24,
                    'Frozen PDF detail is missing page sizes or its source revision')
            pdf_page_route = f"/api/books/{pdf_book['id']}/pages/0?width=800&v={pdf_detail['page_revision']}"
            status, pdf_page, mime = client.request(pdf_page_route)
            require(status == 200 and mime == 'image/jpeg' and pdf_page.startswith(b'\xff\xd8'),
                    'Frozen PDF worker did not render a reading JPEG')
            with Image.open(BytesIO(pdf_page)) as decoded:
                require(decoded.width == 800 and decoded.height == 1200,
                        'Frozen PDF worker produced the wrong page dimensions')
            pdf_etag = client.last_headers.get('ETag')
            require(pdf_etag and client.request(pdf_page_route, headers={'If-None-Match': pdf_etag})[0] == 304,
                    'Frozen PDF conditional page request did not return 304')
            require(Client(url).request(pdf_page_route)[0] == 401
                    and child_client.request(pdf_page_route, headers={'If-None-Match': pdf_etag})[0] == 404,
                    'Frozen PDF page cache bypassed login or library access checks')
            status, pdf_cover, mime = client.request(f"/api/books/{pdf_book['id']}/cover")
            require(status == 200 and mime == 'image/jpeg' and pdf_cover.startswith(b'\xff\xd8'),
                    'Frozen PDF first-page cover generation failed')
            require(client.request(f"/api/books/{pdf_book['id']}/progress", {'page': 1, 'completed': False})[0] == 200,
                    'Frozen PDF reading progress could not be saved')
            require(client.request(f"/api/books/{pdf_book['id']}")[1].get('progress', {}).get('page') == 1,
                    'Frozen PDF reading progress did not persist')
            status, pdf_cache, _ = client.request('/api/cache')
            category = next((item for item in pdf_cache.get('categories', []) if item.get('kind') == 'pdf'), {})
            require(status == 200 and category.get('files', 0) >= 1 and category.get('bytes', 0) > 0,
                    'Frozen PDF rendered-page cache is missing from cache management')
            require(client.request('/api/cache/clear', {'kind': 'pdf', 'confirm': False})[0] == 400,
                    'Frozen PDF cache removal did not require confirmation')
            require(client.request('/api/cache/clear', {'kind': 'pdf', 'confirm': True})[0] == 200
                    and client.request(pdf_page_route)[0] == 200,
                    'Frozen PDF page could not regenerate after cache removal')
            require(hashlib.sha256(pdf_source.read_bytes()).hexdigest() == pdf_digest,
                    'PDF scanning, reading, or cache removal changed the source PDF')
            with closing(sqlite3.connect(data / 'library.sqlite3')) as connection:
                require(connection.execute('SELECT COUNT(*) FROM tc_pages WHERE book_id=?', (pdf_book['id'],)).fetchone()[0] == 0
                        and connection.execute('SELECT COUNT(*) FROM is_pages WHERE book_id=?', (pdf_book['id'],)).fetchone()[0] == 0,
                        'PDF pages were incorrectly queued for image optimization or image search')
            passed('frozen_pdf_probe_render_cover_progress_and_cache', pages=3, width=800, height=1200,
                   rendered_bytes=len(pdf_page), source_unmodified=True)

            # Append an independent EPUB library after exact-count image/search
            # checks. The driver creates data, while only the frozen application
            # parses the package, sanitizes text and authorizes resource reads.
            epub_folder = second_media / 'EPUB 测试'
            epub_folder.mkdir()
            epub_source = epub_folder / '图片样例.epub'
            epub_bytes = sample_epub()
            epub_source.write_bytes(epub_bytes)
            epub_digest = hashlib.sha256(epub_bytes).hexdigest()
            for route, mime_expected, marker in (('/epub-reader.js', 'text/javascript', b'GravelleEpubReader'),
                                                 ('/epub-reader.css', 'text/css', b'.epub-reader')):
                status, helper, mime = client.request(route)
                require(status == 200 and mime == mime_expected and marker in helper
                        and route.encode() in index,
                        'Frozen bundle is missing an EPUB browser resource: ' + route)
            status, created, _ = client.request('/api/libraries', {
                'name': '冻结包 EPUB 样例', 'paths': [str(epub_folder)], 'media_type': 'photo'})
            require(status == 201 and created.get('library', {}).get('id'), 'Frozen EPUB library creation failed')
            epub_library_id = created['library']['id']
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, epub_listing, _ = client.request('/api/library?library_id=' + epub_library_id)
                if status == 200 and not epub_listing.get('scanning'):
                    break
                time.sleep(.1)
            require(status == 200 and not epub_listing.get('scanning') and len(epub_listing.get('books', [])) == 1,
                    'Frozen scanner did not catalogue the disposable EPUB')
            epub_book = epub_listing['books'][0]
            require(epub_book.get('format') == 'epub' and epub_book.get('page_count') == 3,
                    'Frozen EPUB scanner did not report its three picture pages')
            epub_base = f"/api/books/{epub_book['id']}"
            status, epub_detail, _ = client.request(epub_base)
            require(status == 200 and epub_detail.get('reader_kind') == 'images'
                    and epub_detail.get('document_author') == 'Gravelle smoke fixture'
                    and len(epub_detail.get('page_revision', '')) == 24,
                    'Frozen EPUB detail is missing its picture reading metadata')
            expected_images = (sample_png((30, 60, 210), (220, 330)),
                               sample_png((214, 73, 139), (200, 300)),
                               sample_png((20, 160, 70), (180, 270)))
            for number, expected_image in enumerate(expected_images):
                epub_page_route = epub_base + '/pages/' + str(number)
                status, epub_image, mime = client.request(epub_page_route)
                require(status == 200 and mime == 'image/png' and epub_image == expected_image,
                        'Frozen EPUB page order or source bytes changed')
                epub_etag = client.last_headers.get('ETag')
                require(epub_etag and client.request(epub_page_route, headers={'If-None-Match': epub_etag})[0] == 304,
                        'Frozen EPUB page conditional request did not return 304')
                require(Client(url).request(epub_page_route)[0] == 401
                        and child_client.request(epub_page_route, headers={'If-None-Match': epub_etag})[0] == 404,
                        'Frozen EPUB picture bypassed authentication or child grants')
            status, epub_cover, mime = client.request(epub_base + '/cover')
            require(status == 200 and mime == 'image/jpeg' and epub_cover.startswith(b'\xff\xd8'),
                    'Frozen EPUB picture could not generate a reading thumbnail')
            require(client.request(epub_base + '/progress', {'page': 2, 'completed': False})[0] == 200,
                    'Frozen EPUB picture position could not be saved')
            epub_saved = client.request(epub_base)[1].get('progress', {})
            require(epub_saved.get('page') == 2 and 'position' not in epub_saved,
                    'Frozen EPUB picture progress did not persist')
            status, epub_mark, _ = client.request(epub_base + '/bookmarks', {
                'page': 2, 'note': 'Generated EPUB picture bookmark'})
            require(status == 201 and epub_mark.get('bookmark', {}).get('page') == 2,
                    'Frozen EPUB bookmark lost its picture page')
            require(client.request(epub_base + '/document')[0] == 404,
                    'Frozen EPUB must not expose a raw document download route')
            require(hashlib.sha256(epub_source.read_bytes()).hexdigest() == epub_digest,
                    'EPUB scanning, reading, cover or location saving changed the original EPUB')
            with closing(sqlite3.connect(data / 'library.sqlite3')) as connection:
                require(connection.execute('SELECT COUNT(*) FROM tc_pages WHERE book_id=?', (epub_book['id'],)).fetchone()[0] == 0
                        and connection.execute('SELECT COUNT(*) FROM is_pages WHERE book_id=?', (epub_book['id'],)).fetchone()[0] == 0,
                        'EPUB chapters were incorrectly queued for image optimization or image search')
            passed('frozen_epub_picture_order_svg_pages_cover_locations_and_grants', pages=3,
                   image_bytes=len(epub_image), source_unmodified=True)

            # Run capability tests after all exact-count library/search checks.
            # Reuse the generated photo and leave its source unchanged; crypto
            # runs inside the frozen service and is checked independently here.
            share_checks = sharing_smoke(client, url, book['id'], image)
            require(source_image.read_bytes() == image,
                    'Frozen share creation or reading changed the generated source image')
            passed('frozen_encrypted_share_create_unlock_decrypt_and_close',
                   source_unmodified=True, **share_checks)

            network_checks = network_settings_smoke(client, url, config, book['id'], image)
            passed('frozen_network_entry_and_independent_share_listener', **network_checks)

            child = controller.process
            controller.stop()
            require(child.returncode == 0, f"stdin stop returned exit code {child.returncode}")
            require(check_port(config)["available"], "Service port was not released after stop")
            require(ports_released(config), 'Frozen service did not release both listeners after stop')
            passed("stdin_stop_and_port_release", returncode=child.returncode)
            with closing(sqlite3.connect(data / "library.sqlite3")) as connection:
                require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                        "Disposable SQLite integrity check failed")
                require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                        "Disposable SQLite foreign key check failed")
                require(connection.execute("SELECT count(*) FROM owner_account").fetchone()[0] == 1,
                        "Disposable owner account was not persisted")
            passed("sqlite_integrity_and_persistence")

            controller.start(config, timeout=35)
            require(Client(url).request('/')[0] == 404
                    and Client('http://127.0.0.1:' + str(config.share_port)).request('/reader-model.js')[0] == 200,
                    'Frozen network settings or isolated reader resources did not survive a service restart')
            passed('frozen_network_settings_survive_restart')
            child = controller.process
            child.stdin.close()
            require(child.wait(timeout=20) == 0, "EOF on parent pipe did not gracefully stop the frozen server")
            controller.stop()
            require(check_port(config)["available"], "Service port was not released after parent pipe EOF")
            require(ports_released(config), 'Frozen service did not release both listeners after parent pipe EOF')
            passed("stdin_eof_and_port_release", returncode=child.returncode)
            passed('frozen_offline_security_entry_recovery', **network_recovery_smoke(executable, data))
            controller.start(config, timeout=35)
            require(Client(url).request('/')[0] == 200
                    and Client(url).request('/api/network-settings')[0] == 401
                    and Client('http://127.0.0.1:' + str(config.share_port)).request('/')[0] == 404
                    and Client('http://127.0.0.1:' + str(config.share_port)).request('/reader-model.js')[0] == 200,
                    'Frozen recovery restart did not restore the login shell with share isolation intact')
            controller.stop()
            require(ports_released(config), 'Frozen recovery service did not release both listeners')
            passed('frozen_recovery_restart_and_dual_port_release')
            report["status"] = "passed"
        except Exception as error:
            report["status"] = "failed"
            report["error"] = str(error)
        finally:
            if controller is not None:
                try:
                    controller.stop()
                except Exception as error:
                    report["status"] = "failed"
                    report["cleanup_error"] = str(error)
            if report["status"] != "passed" and log_path and log_path.exists():
                report["server_log_tail"] = log_path.read_text(encoding="utf-8", errors="replace")[-16000:]
    report["temporary_data_removed"] = not test_root.exists()
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=Path("work/windows-smoke"))
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args(argv)
    executable = arguments.executable.resolve(strict=True)
    require(executable.is_file() and executable.name.casefold() == "gravelleserver.exe",
            "Select the packaged GravelleServer.exe companion, not the GUI or an arbitrary command")
    work_root = arguments.work_root.resolve()
    require(work_root != Path(work_root.anchor), "The smoke work root must not be a filesystem root")
    work_root.mkdir(parents=True, exist_ok=True)
    report_path = (arguments.report or work_root / "report.json").resolve()
    report = run_smoke(executable, work_root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
                      "report": str(report_path), "duration_seconds": report["duration_seconds"]},
                     ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

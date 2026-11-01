"""Generated PKCS#12 certificates and real local TLS listener integration."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import http.client
from http.cookies import SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from PIL import Image

from https_settings import (CertificatePasswordVault, HTTPSSettingsError, KEY_NAME,
                            load_context, validate_certificate_path)
from network_settings import reset_security_entry
from server import COOKIE_NAME, Library, ReaderServer


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def scratch():
    root = Path(__file__).resolve().parents[1] / "work" / "https-tests"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def certificate(directory, name="valid", password="", *, expired=False, missing_key=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    leaf = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                           x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    encryption = serialization.BestAvailableEncryption(password.encode("utf-8")) if password else serialization.NoEncryption()
    bundle = pkcs12.serialize_key_and_certificates(b"fixture", None if missing_key else key,
                                                  leaf, None, encryption)
    path = directory / (name + ".pfx")
    path.write_bytes(bundle)
    trust = directory / (name + ".pem")
    trust.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    return path, trust


class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = scratch(); cls.root = Path(cls.temp.name)
        cls.clear, cls.trust = certificate(cls.root)
        cls.password = "  测试证书 Password  "
        cls.locked, _ = certificate(cls.root, "locked", cls.password)
        cls.expired, _ = certificate(cls.root, "expired", expired=True)
        cls.missing, _ = certificate(cls.root, "missing", missing_key=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_valid_empty_and_unicode_password_keep_tls12_minimum(self):
        self.assertEqual(load_context(str(self.clear), "").minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertIsInstance(load_context(str(self.locked), self.password), ssl.SSLContext)
        for password in ("wrong", self.password.strip(), ""):
            with self.subTest(password=password), self.assertRaises(HTTPSSettingsError):
                load_context(str(self.locked), password)

    def test_bad_expired_missing_key_and_bounded_file(self):
        for path in (self.expired, self.missing):
            with self.subTest(path=path.name), self.assertRaises(HTTPSSettingsError):
                load_context(str(path), "")
        oversized = self.root / "oversized.pfx"
        with oversized.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        for value in (str(oversized), str(self.root / "absent.pfx"), str(self.root)):
            with self.subTest(value=value), self.assertRaises(HTTPSSettingsError):
                load_context(value, "")
        for path in ("relative.pfx", "https://example.com/cert.pfx", str(self.clear.with_suffix('.pem'))):
            with self.assertRaises(HTTPSSettingsError):
                validate_certificate_path(path)

    def test_private_pem_files_are_restricted_and_removed(self):
        original = ssl.SSLContext.load_cert_chain
        observed = []
        def inspect(context, certificate, key):
            paths = (Path(certificate), Path(key))
            self.assertTrue(all(path.exists() for path in paths))
            if os.name != "nt":
                self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in paths))
            observed.extend(paths)
            return original(context, certificate, key)
        with patch.object(ssl.SSLContext, "load_cert_chain", inspect):
            load_context(str(self.clear), "")
        self.assertTrue(observed)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in observed))

    def test_vault_encrypts_and_detects_missing_or_damaged_key(self):
        with scratch() as folder:
            vault = CertificatePasswordVault(folder)
            encrypted = vault.seal(self.password)
            self.assertNotIn(self.password, encrypted)
            self.assertEqual(vault.reveal(encrypted), self.password)
            self.assertEqual(vault.seal(""), "")
            key = Path(folder) / KEY_NAME
            if os.name != "nt":
                self.assertEqual(key.stat().st_mode & 0o077, 0)
            key.write_bytes(b"wrong")
            with self.assertRaises(HTTPSSettingsError):
                vault.reveal(encrypted)


class HTTPSHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = scratch(); cls.root = Path(cls.temp.name)
        media = cls.root / "media"; (media / "Book").mkdir(parents=True)
        Image.new("RGB", (30, 40), "pink").save(media / "Book" / "1.jpg")
        cls.library = Library(media, cls.root / "data", "", 0)
        cls.library.transcoding.auto_start = cls.library.image_search.auto_start = False
        cls.library.register({"username": "https-admin", "password": "test-https-password"}, "fixture")
        cls.admin = COOKIE_NAME + "=" + cls.library.make_session(1)
        cls.library.save_library({"name": "TLS fixture", "path": str(media), "media_type": "photo"})
        deadline = time.monotonic() + 10
        while cls.library.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        cls.book = cls.library.listing()["books"][0]
        cls.cert, trust = certificate(cls.root, "tls")
        cls.secret = "  证书 secret  "
        cls.locked, _ = certificate(cls.root, "protected", cls.secret)
        cls.client_context = ssl.create_default_context(cafile=str(trust))

    @classmethod
    def tearDownClass(cls):
        cls.library.close(); cls.temp.cleanup()

    def setUp(self):
        self.library.network.persist(self.library.network.prepare({
            "security_entry_enabled": False, "security_entry_code": "", "share_port_enabled": False,
            "public_share_url": "", "connection_mode": "http", "certificate_path": "", "certificate_password": ""}))
        self.share_port, self.https_port = free_port(), free_port()
        self.server = ReaderServer(("127.0.0.1", 0), self.library, runtime="source",
                                   share_port=self.share_port, https_port=self.https_port)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def request(self, path, body=None, *, tls=False, port=None, cookie=None, server=None):
        owner = server or self.server
        port = port or (owner.https_port if tls else owner.server_port)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        client = (http.client.HTTPSConnection("127.0.0.1", port, context=self.client_context, timeout=4)
                  if tls else http.client.HTTPConnection("127.0.0.1", port, timeout=4))
        try:
            client.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, headers)
            response = client.getresponse(); data = response.read()
            value = json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data
            return response.status, value, dict(response.headers)
        finally:
            client.close()

    def configure(self, body):
        return self.request("/api/network-settings", body, cookie=self.admin)

    def enable(self, **extra):
        status, snapshot, _ = self.configure({"connection_mode": "https", "certificate_path": str(self.cert), **extra})
        self.assertEqual(status, 200, snapshot)
        self.assertTrue(snapshot["https_listening"])
        return snapshot

    def test_real_verified_tls_health_login_cookie_and_old_http(self):
        snapshot = self.enable()
        self.assertEqual(snapshot["share_scheme"], "https")
        self.assertEqual(self.request("/api/health")[1]["https_listening"], True)
        self.assertEqual(self.request("/", tls=True)[0], 200)
        status, _, headers = self.request("/api/login", {"username": "https-admin", "password": "test-https-password"}, tls=True)
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/api/access", tls=True, cookie=self.admin)[0], 200)

    def test_private_password_omission_clear_and_legacy_post(self):
        status, snapshot, _ = self.configure({"connection_mode": "https", "certificate_path": str(self.locked),
                                             "certificate_password": self.secret})
        self.assertEqual(status, 200, snapshot)
        self.assertTrue(snapshot["certificate_password_set"])
        self.assertNotIn("certificate_password", snapshot)
        self.assertNotIn("certificate_password_ciphertext", snapshot)
        raw = self.library.network.current()
        self.assertNotIn(self.secret, json.dumps(raw, ensure_ascii=False))
        status, snapshot, _ = self.configure({"public_share_url": "https://share.example"})
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["connection_mode"], "https")
        self.assertTrue(snapshot["certificate_password_set"])
        before = self.server.network_snapshot()
        self.assertEqual(self.configure({"certificate_password": "wrong"})[0], 400)
        self.assertEqual(self.server.network_snapshot(), before)
        status, cleared, _ = self.configure({"connection_mode": "http", "certificate_password": ""})
        self.assertEqual(status, 200, cleared)
        self.assertFalse(cleared["certificate_password_set"])
        self.assertFalse(cleared["https_listening"])

    def test_share_tls_keeps_management_isolated_and_secure_cookie(self):
        _, created, _ = self.request("/api/shares", {"book_id": self.book["id"], "days": 1}, cookie=self.admin)
        token = created["share"]["token"]; path = "/api/share/" + token
        self.enable(share_port_enabled=True)
        for route in ("/", "/api/session", "/api/network-settings", "/api/health"):
            self.assertEqual(self.request(route, tls=True, port=self.share_port, cookie=self.admin)[0], 404)
        self.assertEqual(self.request("/share/" + token, tls=True)[0], 404)
        self.assertEqual(self.request("/share/" + token)[0], 404)
        self.assertEqual(self.request("/share/" + token, tls=True, port=self.share_port)[0], 200)
        status, _, headers = self.request(path + "/unlock", {"password": created["password"]}, tls=True, port=self.share_port)
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_existing_share_listener_switches_http_tls_http_without_rebinding(self):
        self.configure({"share_port_enabled": True})
        listener = self.server.share_server
        self.assertEqual(self.request("/share-reader.js", port=self.share_port)[0], 200)
        self.enable()
        self.assertIs(self.server.share_server, listener)
        self.assertEqual(self.request("/share-reader.js", tls=True, port=self.share_port)[0], 200)
        self.assertEqual(self.configure({"connection_mode": "proxy"})[0], 200)
        self.assertIs(self.server.share_server, listener)
        self.assertEqual(self.request("/share-reader.js", port=self.share_port)[0], 200)
        self.assertIsNone(self.server.https_server)

    def test_tls_port_conflict_and_bad_certificate_leave_all_old_fields(self):
        before = self.server.network_snapshot()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.https_port)); occupied.listen()
            status, _, _ = self.configure({"connection_mode": "https", "certificate_path": str(self.cert),
                                          "share_port_enabled": True, "security_entry_enabled": True,
                                          "security_entry_code": "test_tls_entry123"})
        self.assertEqual(status, 409)
        self.assertEqual(self.server.network_snapshot(), before)
        self.assertEqual(self.configure({"connection_mode": "https", "certificate_path": str(self.root / "missing.pfx")})[0], 400)
        self.assertEqual(self.server.network_snapshot(), before)
        self.assertEqual(self.request("/")[0], 200)

    def test_persistence_failure_disposes_prepared_tls_listeners(self):
        before = self.server.network_snapshot()
        with patch.object(self.library.network, "persist", side_effect=sqlite3.OperationalError("fixture")):
            self.assertEqual(self.configure({"connection_mode": "https", "certificate_path": str(self.cert),
                                             "share_port_enabled": True})[0], 500)
        self.assertEqual(self.server.network_snapshot(), before)
        for port in (self.https_port, self.share_port):
            with socket.socket() as available:
                available.bind(("127.0.0.1", port))

    def test_security_entry_cookie_is_secure_on_direct_tls(self):
        self.enable(security_entry_enabled=True, security_entry_code="tls_Entry123")
        status, _, headers = self.request("/tls_Entry123", tls=True)
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_tls_mode_without_separate_port_never_serves_share_over_http(self):
        _, created, _ = self.request("/api/shares", {"book_id": self.book["id"], "days": 1}, cookie=self.admin)
        self.enable()
        route = "/share/" + created["share"]["token"]
        self.assertEqual(self.request(route)[0], 404)
        self.assertEqual(self.request(route, tls=True)[0], 200)
        self.assertEqual(self.configure({"connection_mode": "http"})[0], 200)
        self.assertEqual(self.request(route)[0], 200)

    def test_plaintext_keepalive_accepted_before_tls_switch_cannot_read_shares(self):
        self.configure({"share_port_enabled": True})
        client = http.client.HTTPConnection("127.0.0.1", self.share_port, timeout=3)
        try:
            client.request("GET", "/share-reader.js")
            response = client.getresponse(); response.read()
            self.assertEqual(response.status, 200)
            self.enable()
            client.request("GET", "/share-reader.js")
            response = client.getresponse(); response.read()
            self.assertEqual(response.status, 404)
        finally:
            client.close()

    def test_shutdown_releases_both_secondary_ports(self):
        self.enable(share_port_enabled=True)
        self.server.shutdown()
        for port in (self.https_port, self.share_port):
            with socket.socket() as available:
                available.bind(("127.0.0.1", port))


class HTTPSPersistenceTests(unittest.TestCase):
    def test_legacy_settings_migrate_and_offline_recovery_preserves_tls(self):
        with scratch() as folder:
            root = Path(folder); (root / "media").mkdir()
            library = Library(root / "media", root / "data", "", 0)
            initial = library.network.current()
            library.close()
            for key in ("connection_mode", "certificate_path", "certificate_password_ciphertext"):
                initial.pop(key)
            with closing(sqlite3.connect(root / "data/library.sqlite3")) as database, database:
                database.execute("UPDATE network_settings SET settings=?", (json.dumps(initial),))
            library = Library(root / "media", root / "data", "", 0)
            self.assertEqual(library.network.current()["connection_mode"], "http")
            path, _ = certificate(root, password="secret pass")
            value = library.network.prepare({"connection_mode": "https", "certificate_path": str(path),
                                             "certificate_password": "secret pass", "share_port_enabled": True,
                                             "security_entry_enabled": True, "security_entry_code": "old_Entry123"})
            library.network.persist(value); library.close()
            reset_security_entry(root / "data")
            library = Library(root / "media", root / "data", "", 0)
            try:
                current = library.network.current()
                self.assertFalse(current["security_entry_enabled"])
                for key in ("connection_mode", "certificate_path", "certificate_password_ciphertext", "share_port_enabled"):
                    self.assertEqual(current[key], value[key])
                self.assertEqual(library.network.certificate_password(), "secret pass")
            finally:
                library.close()

    def test_startup_bad_certificate_preserves_http_management_and_share_isolation(self):
        with scratch() as folder:
            root = Path(folder); (root / "media").mkdir()
            library = Library(root / "media", root / "data", "", 0)
            value = library.network.prepare({"connection_mode": "https", "certificate_path": str(root / "missing.pfx"),
                                             "share_port_enabled": True})
            library.network.persist(value)
            server = ReaderServer(("127.0.0.1", 0), library, share_port=free_port(), https_port=free_port(), runtime="source")
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            thread.start()
            try:
                snapshot = server.network_snapshot()
                self.assertFalse(snapshot["https_listening"])
                self.assertFalse(snapshot["share_listening"])
                self.assertTrue(snapshot["https_error"])
                self.assertEqual(snapshot["connection_mode"], "https")
                self.assertEqual(snapshot["share_scheme"], "https")
                self.assertTrue(snapshot["share_port_enabled"])
                for route, expected in (("/", 200), ("/api/health", 200), ("/share/" + "x" * 43, 404)):
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        client.request("GET", route)
                        response = client.getresponse(); response.read()
                        self.assertEqual(response.status, expected)
                    finally:
                        client.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(3); library.close()


if __name__ == "__main__":
    unittest.main()

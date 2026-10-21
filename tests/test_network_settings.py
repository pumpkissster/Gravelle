"""Actual HTTP listener boundaries and offline entry recovery."""
from contextlib import closing
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from network_settings import (ENTRY_COOKIE, NetworkSettingsError, SHARE_STATIC,
                              read_security_entry_path, reset_security_entry,
                              validate_entry_code, validate_public_share_url)
from server import COOKIE_NAME, VERSION, Library, ReaderServer


def temporary_directory():
    scratch = Path(__file__).resolve().parents[1] / "work" / "network-tests"
    scratch.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=scratch)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NetworkHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = temporary_directory()
        cls.root = Path(cls.temp.name)
        media = cls.root / "media"
        (media / "Share work").mkdir(parents=True)
        Image.new("RGB", (40, 60), "pink").save(media / "Share work" / "1.png")
        cls.library = Library(media, cls.root / "data", "", 0)
        cls.library.transcoding.auto_start = cls.library.image_search.auto_start = False
        cls.library.register({"username": "network-admin", "password": "network-test-password"}, "fixture")
        child = cls.library.save_user({"username": "network-child", "password": "network-child-password", "library_ids": []})["user"]
        cls.admin = f"{COOKIE_NAME}={cls.library.make_session(1)}"
        cls.child = f"{COOKIE_NAME}={cls.library.make_session(child['id'])}"
        cls.library.save_library({"name": "Network fixture", "path": str(media), "media_type": "photo"})
        deadline = time.monotonic() + 10
        while cls.library.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        cls.book = cls.library.listing()["books"][0]

    @classmethod
    def tearDownClass(cls):
        cls.library.close()
        cls.temp.cleanup()

    def setUp(self):
        # Persist defaults between independently running disposable listeners.
        value = self.library.network.prepare({"security_entry_enabled": False, "security_entry_code": "",
                                              "share_port_enabled": False, "public_share_url": ""})
        self.library.network.persist(value)
        self.share_port = free_port()
        self.server = ReaderServer(("127.0.0.1", 0), self.library, share_port=self.share_port, runtime="source")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.entry_cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, *, port=None, cookie=None, method=None, headers=None):
        metadata = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            metadata["Cookie"] = cookie
        metadata.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port or self.server.server_port, timeout=3)
        try:
            connection.request(method or ("POST" if body is not None else "GET"), path,
                               json.dumps(body) if body is not None else None, metadata)
            response = connection.getresponse()
            raw = response.read()
            payload = json.loads(raw) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, payload, dict(response.headers)
        finally:
            connection.close()

    def configure(self, body):
        status, data, headers = self.request("/api/network-settings", body, cookie=self.admin + ("; " + self.entry_cookie if self.entry_cookie else ""))
        if status == 200:
            cookie = SimpleCookie()
            cookie.load(headers["Set-Cookie"])
            self.entry_cookie = f"{ENTRY_COOKIE}={cookie[ENTRY_COOKIE].value}"
        return status, data, headers

    def test_defaults_are_compatible_and_settings_are_admin_only(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/api/network-settings")[0], 401)
        self.assertEqual(self.request("/api/network-settings", cookie=self.child)[0], 403)
        self.assertEqual(self.request("/api/network-settings", {}, cookie=self.child)[0], 403)
        status, value, _ = self.request("/api/network-settings", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertFalse(value["security_entry_enabled"])
        self.assertFalse(value["share_port_enabled"])
        self.assertEqual(value["security_entry_path"], "/")
        self.assertNotIn("entry_epoch", value)
        self.assertEqual(value["runtime"], "source")

    def test_entry_gates_page_assets_login_and_existing_account_cookie(self):
        status, value, headers = self.configure({"security_entry_enabled": True, "security_entry_code": "secret_Entry-123"})
        self.assertEqual(status, 200, value)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        for path in ("/", "/index.html", "/app.js", "/api/session", "/api/library", "/api/network-settings"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=self.admin)[0], 404)
        self.assertEqual(self.request("/api/login", {"username": "network-admin", "password": "network-test-password"})[0], 404)
        self.assertEqual(self.request("/api/health")[1], {"status": "ok", "version": VERSION})
        status, html, headers = self.request("/secret_Entry-123")
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", html)
        self.assertEqual(headers["Cache-Control"], "no-store")
        cookie = SimpleCookie(); cookie.load(headers["Set-Cookie"])
        entry = f"{ENTRY_COOKIE}={cookie[ENTRY_COOKIE].value}"
        self.assertEqual(self.request("/api/session", cookie=entry)[1]["authenticated"], False)
        self.assertEqual(self.request("/api/library", cookie=entry)[0], 401)
        self.assertEqual(self.request("/api/library", cookie=entry + "; " + self.admin)[0], 200)
        self.assertEqual(self.request("/app.js", cookie=entry)[0], 200)
        for path in ("//secret_Entry-123", "/%73ecret_Entry-123", "/secret_Entry-123/", "/secret_Entry-123?x=1"):
            self.assertEqual(self.request(path)[0], 404)

    def test_entry_rotation_revokes_old_capabilities_but_save_keeps_current_admin(self):
        self.assertEqual(self.configure({"security_entry_enabled": True, "security_entry_code": "entry_One123"})[0], 200)
        old = self.entry_cookie
        self.assertEqual(self.configure({"security_entry_code": "entry_Two456"})[0], 200)
        self.assertEqual(self.request("/api/session", cookie=old)[0], 404)
        self.assertEqual(self.request("/entry_One123")[0], 404)
        self.assertEqual(self.request("/entry_Two456")[0], 200)
        self.assertEqual(self.request("/api/network-settings", cookie=self.admin + "; " + self.entry_cookie)[0], 200)
        self.assertEqual(self.configure({"security_entry_enabled": False})[0], 200)
        self.assertEqual(self.request("/")[0], 200)

    def test_entry_cookie_forgery_and_expiry_never_grant_access(self):
        self.configure({"security_entry_enabled": True, "security_entry_code": "entry_One123"})
        token = self.entry_cookie.partition("=")[2]
        for fake in ("", token[:-1] + ("0" if token[-1] != "0" else "1"), token.replace(token.split(".")[1], "1"), "x" * 500):
            self.assertEqual(self.request("/api/session", cookie=f"{ENTRY_COOKIE}={fake}")[0], 404)

    def test_lan_address_has_entry_path_and_logs_do_not_leak_it(self):
        self.configure({"security_entry_enabled": True, "security_entry_code": "do_not_log_123"})
        with self.assertLogs("sumi", level="INFO") as logs:
            self.request("/do_not_log_123")
        self.assertNotIn("do_not_log_123", " ".join(logs.output))
        status, data, _ = self.request("/api/access", cookie=self.admin + "; " + self.entry_cookie,
                                      headers={"Host": "192.168.1.100:8080"})
        self.assertEqual(status, 200)
        self.assertEqual(data["lan_url"], "http://192.168.1.100:8080/do_not_log_123")

    def test_independent_listener_accepts_only_exact_share_reader_resources(self):
        status, data, _ = self.configure({"share_port_enabled": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["share_listening"])
        self.assertEqual(SHARE_STATIC, {"/share-reader.js", "/share-reader.css", "/share-crypto.js",
                                      "/styles.css", "/reader-model.js", "/vendor/noble-gcm.js",
                                      "/gravelle-icon.png", "/gravelle-wordmark.svg"})
        for path in SHARE_STATIC:
            self.assertEqual(self.request(path, port=self.share_port)[0], 200, path)
            self.assertEqual(self.request(path + "?x=1", port=self.share_port)[0], 404, path)
            self.assertEqual(self.request("/%" + format(ord(path[1]), "02x") + path[2:], port=self.share_port)[0], 404, path)
        for path in ("/", "/index.html", "/app.js", "/api/session", "/api/library", "/api/health", "/api/network-settings",
                     "/network-settings-ui.js", "/reader-end.js", "/reader-end.css", "/page-router.js",
                     "/share-reader.js?x=1", "/%73hare-reader.js", "//share-reader.js", "/share/../index.html"):
            self.assertEqual(self.request(path, port=self.share_port, cookie=self.admin)[0], 404, path)
        for method in ("OPTIONS", "PUT", "DELETE", "PATCH", "TRACE", "CONNECT"):
            self.assertEqual(self.request("/", port=self.share_port, method=method)[0], 404, method)
        self.assertEqual(self.request("/api/login", {"username": "network-admin", "password": "network-test-password"}, port=self.share_port)[0], 404)

    def test_shared_reading_moves_to_independent_port_and_revocation_survives(self):
        status, share, _ = self.request("/api/shares", {"book_id": self.book["id"], "days": 1}, cookie=self.admin)
        self.assertEqual(status, 201, share)
        token = share["share"]["token"]
        path = "/api/share/" + token
        self.configure({"security_entry_enabled": True, "security_entry_code": "entry_One123"})
        self.assertEqual(self.request("/share/" + token)[0], 200)
        self.assertEqual(self.request(path)[0], 200)
        self.configure({"share_port_enabled": True})
        for route in ("/share/" + token, path, path + "/manifest", path + "/pages/0"):
            self.assertEqual(self.request(route, cookie=self.admin + "; " + self.entry_cookie)[0], 404)
        self.assertEqual(self.request(path + "/unlock", {"password": share["password"]})[0], 404)
        status, unlocked, headers = self.request(path + "/unlock", {"password": share["password"]}, port=self.share_port)
        self.assertEqual(status, 200, unlocked)
        cookie = SimpleCookie(); cookie.load(headers["Set-Cookie"])
        guest = "gravelle_share=" + cookie["gravelle_share"].value
        self.assertEqual(self.request(path + "/pages/0", port=self.share_port, cookie=guest)[0], 200)
        self.assertEqual(self.request(path + "/manifest", port=self.share_port, cookie=guest, method="HEAD")[0], 200)
        self.assertEqual(self.request(path + "/unlock", {"password": share["password"]}, port=self.share_port,
                                      headers={"Origin": "https://evil.example"})[0], 403)
        status, _, _ = self.request("/api/shares/" + share["share"]["id"] + "/close", {}, cookie=self.admin + "; " + self.entry_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(path + "/manifest", port=self.share_port, cookie=guest)[0], 410)
        self.assertEqual(self.request(path + "/pages/0", port=self.share_port, cookie=guest)[0], 410)
        self.configure({"share_port_enabled": False})
        self.assertEqual(self.request(path)[0], 410)
        with socket.socket() as sock:
            sock.settimeout(.2)
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", self.share_port)), 0)

    def test_bind_failure_rolls_back_all_fields_and_keeps_main_access(self):
        before = self.server.network_snapshot()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.share_port)); sock.listen()
            status, _, _ = self.configure({"share_port_enabled": True, "security_entry_enabled": True,
                                           "security_entry_code": "entry_One123", "public_share_url": "https://share.example:8443"})
        self.assertEqual(status, 409)
        self.assertEqual(self.server.network_snapshot(), before)
        self.assertEqual(self.request("/")[0], 200)

    def test_persistence_failure_closes_prepared_listener_and_keeps_old_state(self):
        before = self.server.network_snapshot()
        with patch.object(self.library.network, "persist", side_effect=sqlite3.OperationalError("fixture")):
            self.assertEqual(self.configure({"share_port_enabled": True})[0], 500)
        self.assertEqual(self.server.network_snapshot(), before)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.share_port))

    def test_no_web_port_edits_or_unsafe_origin(self):
        for body in ({"share_port": 8989}, {"runtime": "source"}, {"security_entry_enabled": 1},
                     {"share_port_enabled": "true"}, {"security_entry_enabled": True},
                     {"security_entry_code": "/entry123"}, {"public_share_url": "https://user:pass@example.com"}):
            self.assertEqual(self.configure(body)[0], 400, body)
        value = self.configure({"public_share_url": "https://share.example:8443/"})[1]
        self.assertEqual(value["public_share_url"], "https://share.example:8443")

    def test_shutdown_releases_independent_port(self):
        self.configure({"share_port_enabled": True})
        self.server.shutdown()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.share_port))

    def test_expired_share_rejects_old_guest_cookie_and_image_url(self):
        _, created, _ = self.request("/api/shares", {"book_id": self.book["id"], "days": 1}, cookie=self.admin)
        self.configure({"share_port_enabled": True})
        path = "/api/share/" + created["share"]["token"]
        _, _, headers = self.request(path + "/unlock", {"password": created["password"]}, port=self.share_port)
        cookie = SimpleCookie(); cookie.load(headers["Set-Cookie"])
        guest = "gravelle_share=" + cookie["gravelle_share"].value
        with closing(self.library.connect()) as connection, connection:
            connection.execute("UPDATE shares SET expires_at=? WHERE id=?", (time.time() - 1, created["share"]["id"]))
        for route in (path, path + "/manifest", path + "/pages/0"):
            self.assertEqual(self.request(route, port=self.share_port, cookie=guest)[0], 410)

    def test_startup_port_conflict_keeps_main_shares_closed_and_is_recoverable(self):
        self.library.network.persist(self.library.network.prepare({"share_port_enabled": True}))
        with socket.socket() as occupied:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(("127.0.0.1", self.share_port)); occupied.listen()
            with self.assertLogs("sumi", level="ERROR"):
                other = ReaderServer(("127.0.0.1", 0), self.library, share_port=self.share_port, runtime="source")
            try:
                snapshot = other.network_snapshot()
                self.assertTrue(snapshot["share_port_enabled"])
                self.assertFalse(snapshot["share_listening"])
                self.assertTrue(snapshot["share_error"])
                other.configure_network({"share_port_enabled": False})
                self.assertFalse(other.network_snapshot()["share_port_enabled"])
            finally:
                other.server_close()

    def test_listener_restarts_from_persisted_configuration_and_shares_library(self):
        self.configure({"share_port_enabled": True})
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        restarted = ReaderServer(("127.0.0.1", 0), self.library, share_port=self.share_port, runtime="source")
        try:
            self.assertTrue(restarted.network_snapshot()["share_listening"])
            self.assertIs(restarted.share_server.library, self.library)
            self.assertIs(restarted.share_server.library.task_scheduler, self.library.task_scheduler)
            self.assertEqual(self.request("/api/session", port=self.share_port, cookie=self.admin)[0], 404)
        finally:
            restarted.server_close()

    def test_runtime_cannot_override_docker_container_share_port(self):
        container = ReaderServer(("127.0.0.1", 0), self.library, share_port=12345, runtime="docker")
        try:
            self.assertEqual(container.network_snapshot()["share_port"], 8081)
            self.assertEqual(container.network_snapshot()["runtime"], "docker")
        finally:
            container.server_close()

    def test_failed_backup_never_disables_existing_entry(self):
        self.configure({"security_entry_enabled": True, "security_entry_code": "backup_Entry123"})
        before = self.library.network.current()
        # The recovery operation may not run while this library holds its lock.
        with self.assertRaises(NetworkSettingsError):
            reset_security_entry(self.library.data)
        self.assertEqual(self.library.network.current(), before)


class NetworkRecoveryTests(unittest.TestCase):
    def test_offline_reset_requires_stopped_service_and_preserves_accounts(self):
        with temporary_directory() as temporary:
            root = Path(temporary); (root / "media").mkdir()
            app = Library(root / "media", root / "data", "", 0)
            try:
                app.network.persist(app.network.prepare({"security_entry_enabled": True, "security_entry_code": "recover_Entry123",
                                                        "public_share_url": "https://share.example"}))
                token = app.network.issue_cookie()
                self.assertEqual(read_security_entry_path(root / "data"), "/recover_Entry123")
                with self.assertRaises(NetworkSettingsError):
                    reset_security_entry(root / "data")
                with closing(app.connect()) as connection:
                    original_users = connection.execute("SELECT * FROM users").fetchall()
            finally:
                app.close()
            result = reset_security_entry(root / "data")
            self.assertTrue(result["disabled"])
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertEqual(read_security_entry_path(root / "data"), "/")
            with closing(sqlite3.connect(result["backup"])) as connection:
                saved = json.loads(connection.execute("SELECT settings FROM network_settings").fetchone()[0])
                self.assertTrue(saved["security_entry_enabled"])
            new = Library(root / "media", root / "data", "", 0)
            try:
                self.assertFalse(new.network.valid_cookie(token))
                self.assertEqual(new.network.current()["public_share_url"], "https://share.example")
                with closing(new.connect()) as connection:
                    self.assertEqual(connection.execute("SELECT * FROM users").fetchall(), original_users)
            finally:
                new.close()

    def test_helper_never_creates_missing_data_or_migrates_legacy_database(self):
        with temporary_directory() as temporary:
            data = Path(temporary) / "missing"
            self.assertEqual(read_security_entry_path(data), "/")
            self.assertFalse(data.exists())
            data.mkdir()
            with closing(sqlite3.connect(data / "library.sqlite3")) as connection, connection:
                connection.execute("CREATE TABLE legacy(id INTEGER)")
            before = (data / "library.sqlite3").read_bytes()
            self.assertEqual(read_security_entry_path(data), "/")
            self.assertEqual((data / "library.sqlite3").read_bytes(), before)

    def test_malformed_configuration_fails_closed_but_can_be_recovered_offline(self):
        with temporary_directory() as temporary:
            data = Path(temporary)
            with closing(sqlite3.connect(data / "library.sqlite3")) as connection, connection:
                connection.execute("CREATE TABLE network_settings(id INTEGER PRIMARY KEY,settings TEXT)")
                connection.execute("INSERT INTO network_settings VALUES(1,?)", ('{"broken":true}',))
            with self.assertRaises(NetworkSettingsError):
                read_security_entry_path(data)
            self.assertTrue(reset_security_entry(data)["changed"])
            self.assertEqual(read_security_entry_path(data), "/")

    def test_validates_ascii_codes_and_origin_without_network_requests(self):
        for code in ("", "abc", "中文入口码123", "with space", "entry/123", "entry%123", "x" * 65, None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_entry_code(code)
        for url in ("javascript:alert(1)", "//share.example", "https://u@share.example", "https://share.example/path",
                    "https://share.example?", "https://share.example#", "https://share.example:0", "https://share.example:",
                    "https://share.example:65536", "https://bad host", "https://share.example\n", "https://share.example/%2f"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_public_share_url(url)
        for url in ("https://share.example:8443", "http://192.0.2.10:7879", "https://[::1]:8081", ""):
            self.assertEqual(validate_public_share_url(url), url)

    def test_failed_sqlite_backup_leaves_protection_unchanged(self):
        with temporary_directory() as temporary:
            root = Path(temporary); (root / "media").mkdir()
            app = Library(root / "media", root / "data", "", 0)
            app.network.persist(app.network.prepare({"security_entry_enabled": True, "security_entry_code": "backup_Entry123"}))
            app.close()
            with patch("network_settings.os.fsync", side_effect=OSError("fixture")):
                with self.assertRaises(NetworkSettingsError):
                    reset_security_entry(root / "data")
            self.assertEqual(read_security_entry_path(root / "data"), "/backup_Entry123")

    def test_missing_existing_settings_row_fails_closed_on_service_start(self):
        with temporary_directory() as temporary:
            root = Path(temporary); (root / "media").mkdir()
            app = Library(root / "media", root / "data", "", 0)
            app.close()
            with closing(sqlite3.connect(root / "data" / "library.sqlite3")) as connection, connection:
                connection.execute("DELETE FROM network_settings")
            with self.assertRaises(NetworkSettingsError):
                Library(root / "media", root / "data", "", 0)
            self.assertTrue(reset_security_entry(root / "data")["disabled"])
            new = Library(root / "media", root / "data", "", 0)
            new.close()

    def test_cli_is_offline_and_uses_explicit_data_directory(self):
        import subprocess
        import sys
        with temporary_directory() as temporary:
            root = Path(temporary); (root / "media").mkdir()
            app = Library(root / "media", root / "data", "", 0)
            app.network.persist(app.network.prepare({"security_entry_enabled": True, "security_entry_code": "cli_Entry123"}))
            app.close()
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "server.py"),
                                     "--reset-security-entry", "--data", str(root / "data")],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(b"cli_Entry123", result.stdout + result.stderr)
            self.assertEqual(read_security_entry_path(root / "data"), "/")

    def test_entry_only_corruption_preserves_independent_sharing_and_public_origin(self):
        for field, broken in (("security_entry_code", "bad/entry"), ("entry_epoch", "broken"),
                              ("security_entry_enabled", "true")):
            with self.subTest(field=field), temporary_directory() as temporary:
                root = Path(temporary); (root / "media").mkdir()
                app = Library(root / "media", root / "data", "", 0)
                value = app.network.prepare({"security_entry_enabled": True, "security_entry_code": "recover_Entry123",
                                             "share_port_enabled": True, "public_share_url": "https://share.example:8443"})
                app.close()
                value[field] = broken
                with closing(sqlite3.connect(root / "data" / "library.sqlite3")) as connection, connection:
                    connection.execute("UPDATE network_settings SET settings=?", (json.dumps(value),))
                result = reset_security_entry(root / "data")
                self.assertEqual(result["warnings"], [])
                new = Library(root / "media", root / "data", "", 0)
                try:
                    self.assertFalse(new.network.current()["security_entry_enabled"])
                    self.assertEqual(new.network.current()["security_entry_code"], "")
                    self.assertTrue(new.network.current()["share_port_enabled"])
                    self.assertEqual(new.network.current()["public_share_url"], "https://share.example:8443")
                finally:
                    new.close()
                with closing(sqlite3.connect(result["backup"])) as connection:
                    saved = json.loads(connection.execute("SELECT settings FROM network_settings").fetchone()[0])
                    self.assertEqual(saved[field], broken)

    def test_corrupt_sharing_policy_or_json_recovers_to_isolation_not_main_port(self):
        cases = ({"share_port_enabled": "false", "public_share_url": "https://share.example:8443"},
                 {"share_port_enabled": 0, "public_share_url": "https://share.example:8443"},
                 {"public_share_url": "https://share.example:8443"},
                 {"share_port_enabled": True, "public_share_url": "https://user:password@share.example"},
                 "{not valid json", None)
        for document in cases:
            with self.subTest(document=document), temporary_directory() as temporary:
                root = Path(temporary); (root / "media").mkdir()
                app = Library(root / "media", root / "data", "", 0)
                app.close()
                raw = document if isinstance(document, str) else json.dumps(document)
                with closing(sqlite3.connect(root / "data" / "library.sqlite3")) as connection, connection:
                    connection.execute("UPDATE network_settings SET settings=?", (raw,))
                result = reset_security_entry(root / "data")
                self.assertTrue(result["warnings"])
                new = Library(root / "media", root / "data", "", 0)
                try:
                    self.assertTrue(new.network.current()["share_port_enabled"])
                    expected = document.get("public_share_url", "") if isinstance(document, dict) else ""
                    if "@" in expected:
                        expected = ""
                    self.assertEqual(new.network.current()["public_share_url"], expected)
                finally:
                    new.close()


if __name__ == "__main__":
    unittest.main()

"""Launcher configuration, update trust boundaries and private server lifecycle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.core import (LauncherConfig, LauncherError, ReleaseConfig, SingleInstanceLock,
                          check_port, check_update, checksum_for, download_update,
                          lan_addresses, load_config, load_release_config, network_diagnostics,
                          save_config, validate_download_url, verify_installer, version_key)
from desktop.runtime import ServerController
from desktop.launcher import InstallerLaunchError, launch_verified_installer


class ConfigurationTests(unittest.TestCase):
    def test_atomic_roundtrip_unicode_directory_and_port(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photos = root / "写真"
            photos.mkdir()
            config = LauncherConfig(str(photos), 18080, True)
            save_config(root / "launcher.json", config)
            self.assertEqual(load_config(root / "launcher.json"), config)
            self.assertFalse(list(root.glob("launcher-*.tmp")))

    def test_rejects_relative_missing_paths_and_invalid_ports(self):
        for path, port in (("relative", 8080), (str(Path.cwd()), 0), (str(Path.cwd()), True),
                           (str(Path.cwd()), 65536), (str(Path.cwd()), "8080")):
            with self.subTest(path=path, port=port), self.assertRaises(LauncherError):
                LauncherConfig(path, port).validate()

    def test_invalid_config_does_not_silently_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launcher.json"
            path.write_text('{"port": "8080"}', encoding="utf-8")
            with self.assertRaises(LauncherError):
                load_config(path)
            self.assertEqual(json.loads(path.read_text())["port"], "8080")

    def test_missing_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(load_config(Path(folder) / "missing.json").port, 8080)

    def test_single_instance_lock_releases_and_can_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launcher.lock"
            first, second = SingleInstanceLock(path), SingleInstanceLock(path)
            try:
                self.assertTrue(first.acquire())
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.release()
                self.assertTrue(second.acquire())
            finally:
                first.release()
                second.release()


class UpdateTests(unittest.TestCase):
    def test_semver_stable_and_numeric_prerelease_order(self):
        for older, newer in (("1.9.0", "1.10.0"), ("1.0.0-rc.2", "1.0.0-rc.10"),
                             ("1.0.0-rc.10", "1.0.0"), ("v1.0.0", "v2.0.0")):
            self.assertLess(version_key(older), version_key(newer))
        self.assertEqual(version_key("1.0.0+abc"), version_key("1.0.0+def"))
        with self.assertRaises(LauncherError):
            version_key("../../setup.exe")

    def test_unconfigured_update_is_explicit(self):
        result = check_update(None, "1.8.2")
        self.assertFalse(result["configured"])
        self.assertFalse(result["available"])
        self.assertIn("尚未配置", result["message"])

    def test_installation_release_configuration_cannot_use_arbitrary_url(self):
        for name in ("https://evil.example/release", "name/repo/extra", "name/../repo"):
            with self.subTest(name=name), self.assertRaises(LauncherError):
                ReleaseConfig(name).validate()
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            root = Path(folder)
            self.assertIsNone(load_release_config(root))
            (root / "desktop").mkdir()
            (root / "desktop" / "release.json").write_text('{"repository":"pumpkissster/gravelle"}')
            self.assertEqual(load_release_config(root).repository, "pumpkissster/gravelle")

    def test_api_html_url_never_controls_navigation(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://api.github.com/repos/pumpkissster/gravelle/releases/latest"
            def read(self, limit):
                return b'{"tag_name":"v2.0.0","html_url":"https://evil.example/setup.exe"}'
        with patch("desktop.core.urlopen", return_value=Response()):
            result = check_update(ReleaseConfig("pumpkissster/gravelle"), "1.8.2")
        self.assertTrue(result["available"])
        self.assertEqual(result["url"], "https://github.com/pumpkissster/gravelle/releases")

    def test_redirect_and_download_trust(self):
        validate_download_url("https://release-assets.githubusercontent.com/a?signature=123")
        for url in ("http://github.com/a", "https://github.com.evil.example/a", "https://evil.example/a",
                    "https://user@github.com/a", "https://github.com:444/a"):
            with self.subTest(url=url), self.assertRaises(LauncherError):
                validate_download_url(url)

    def test_mislabelled_prerelease_and_nonstable_tags_are_rejected(self):
        class Response:
            def __init__(self, tag): self.tag = tag
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://api.github.com/repos/pumpkissster/gravelle/releases/latest"
            def read(self, limit):
                return json.dumps({"tag_name": self.tag, "prerelease": False, "draft": False}).encode()
        for tag in ("v1.9.0-rc.1", "v01.9.0", "v1.09.0", "v1.9.00", "V1.9.0", "v1.9.0+build", "v1000000000.0.0"):
            with self.subTest(tag=tag), patch("desktop.core.urlopen", return_value=Response(tag)):
                result = check_update(ReleaseConfig("pumpkissster/gravelle"), "1.8.2")
                self.assertFalse(result["available"])
                self.assertNotIn("tag", result)

    def test_checksum_match_is_exact_and_unique(self):
        digest = "a" * 64
        self.assertEqual(checksum_for(digest + "  setup.exe\n", "setup.exe"), digest)
        with self.assertRaises(LauncherError):
            checksum_for(digest + "  ../setup.exe\n", "setup.exe")
        with self.assertRaises(LauncherError):
            checksum_for((digest + "  setup.exe\n") * 2, "setup.exe")

    def test_download_verifies_checksum_before_atomic_publish(self):
        installer = b"test installation program"
        digest = hashlib.sha256(installer).hexdigest()
        filename = "Gravelle-Setup-2.0.0-x64.exe"
        def fake_download(url, maximum, destination=None):
            if destination is None:
                return (digest + "  " + filename).encode()
            destination.write(installer)
        with tempfile.TemporaryDirectory() as folder, patch("desktop.core._download", side_effect=fake_download):
            result = download_update(ReleaseConfig("pumpkissster/gravelle"), {"tag": "v2.0.0"}, Path(folder))
            self.assertTrue(verify_installer(Path(result["path"]), result["sha256"]))
            self.assertFalse(list(Path(folder).glob("*.part")))

    def test_bad_download_removes_temporary_file(self):
        def fake_download(url, maximum, destination=None):
            if destination is None:
                return (("a" * 64) + "  Gravelle-Setup-2.0.0-x64.exe").encode()
            destination.write(b"corrupt")
        with tempfile.TemporaryDirectory() as folder, patch("desktop.core._download", side_effect=fake_download):
            with self.assertRaises(LauncherError):
                download_update(ReleaseConfig("pumpkissster/gravelle"), {"tag": "v2.0.0"}, Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])


class NetworkTests(unittest.TestCase):
    def test_filters_loopback_linklocal_and_duplicate_addresses(self):
        with patch("server._lan_ipv4_addresses", return_value=["127.0.0.1", "169.254.1.2", "192.168.1.2", "192.168.1.2", "invalid"]):
            self.assertEqual(lan_addresses(), ["192.168.1.2"])

    def test_local_connectivity_never_claims_firewall_is_open(self):
        with patch("desktop.core.lan_addresses", return_value=["192.168.1.2"]), \
                patch("desktop.core.socket.create_connection"):
            result = network_diagnostics(LauncherConfig(str(Path.cwd()), 18080, True), running=True)
        self.assertFalse(result["firewall_verified"])
        self.assertIn("无法确认入站防火墙", result["message"])
        self.assertEqual(result["lan_urls"], ["http://192.168.1.2:18080/"])

    def test_port_conflict_does_not_stop_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            config = LauncherConfig(str(Path.cwd()), listener.getsockname()[1])
            self.assertFalse(check_port(config)["available"])
            with tempfile.TemporaryDirectory() as folder:
                controller = ServerController(Path(folder) / "data", Path(folder) / "server.log")
                with self.assertRaises(LauncherError):
                    controller.start(config)
                controller.stop()
            with socket.create_connection(listener.getsockname(), timeout=1):
                pass


class LifecycleTests(unittest.TestCase):
    def test_local_health_ignores_environment_http_proxy(self):
        from desktop.runtime import ProxyHandler
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photos = root / "photos"
            photos.mkdir()
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
            controller = ServerController(root / "data", root / "server.log")
            proxy_environment = {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                                 "HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                                 "NO_PROXY": "", "no_proxy": ""}
            try:
                with patch.dict("os.environ", proxy_environment), \
                        patch("desktop.runtime.ProxyHandler", wraps=ProxyHandler) as proxy_handler:
                    self.assertEqual(controller.start(LauncherConfig(str(photos), port)), f"http://127.0.0.1:{port}/")
                    proxy_handler.assert_called_once_with({})
                self.assertTrue(controller.running)
            finally:
                controller.stop()

    def test_frozen_uses_console_companion_instead_of_gui_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            companion = root / "GravelleServer.exe"
            companion.touch()
            controller = ServerController(root / "data", root / "server.log")
            with patch("desktop.runtime.sys.frozen", True, create=True), \
                    patch("desktop.runtime.sys.executable", str(root / "Gravelle.exe")):
                command = controller.command(LauncherConfig(str(root), 18080), "private-token")
            self.assertEqual(command[0], str(companion))
            self.assertIn("--serve", command)
            self.assertIn("private-token", command)

    def test_real_private_child_health_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photos = root / "photos"
            photos.mkdir()
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
            config = LauncherConfig(str(photos), port)
            controller = ServerController(root / "data", root / "server.log")
            try:
                url = controller.start(config)
                self.assertTrue(controller.running)
                with urlopen(url + "api/health", timeout=2) as response:
                    self.assertEqual(json.loads(response.read())["status"], "ok")
                process = controller.process
                controller.stop()
                self.assertEqual(process.returncode, 0)
                self.assertFalse(controller.running)
                self.assertTrue((root / "data" / "library.sqlite3").exists())
                self.assertTrue(check_port(config)["available"])
            finally:
                controller.stop()


    def test_parent_pipe_eof_shuts_down_private_child(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photos = root / "photos"
            photos.mkdir()
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
            controller = ServerController(root / "data", root / "server.log")
            try:
                controller.start(LauncherConfig(str(photos), port))
                process = controller.process
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                controller.stop()


class InstallerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.installer = Path(self.temporary.name) / "Gravelle-Setup.exe"
        self.installer.write_bytes(b"mock verified installer")
        self.digest = hashlib.sha256(self.installer.read_bytes()).hexdigest()

    def test_appmutex_is_released_before_installer_spawn(self):
        from unittest.mock import Mock
        lock = Mock()
        actions = []
        lock.release.side_effect = lambda: actions.append("release")
        def spawn(arguments, shell):
            self.assertEqual(actions, ["release"])
            self.assertEqual(arguments, [str(self.installer)])
            self.assertIs(shell, False)
            actions.append("spawn")
            return "installer-process"
        with patch("desktop.launcher.subprocess.Popen", side_effect=spawn):
            self.assertEqual(launch_verified_installer(self.installer, self.digest, lock), "installer-process")
        self.assertEqual(actions, ["release", "spawn"])
        lock.acquire.assert_not_called()

    def test_failed_spawn_reacquires_instance_lock(self):
        from unittest.mock import Mock
        lock = Mock()
        lock.acquire.return_value = True
        with patch("desktop.launcher.subprocess.Popen", side_effect=OSError("not executable")):
            with self.assertRaises(InstallerLaunchError) as result:
                launch_verified_installer(self.installer, self.digest, lock)
        self.assertTrue(result.exception.lock_recovered)
        lock.release.assert_called_once()
        lock.acquire.assert_called_once()

    def test_failed_spawn_reports_when_another_instance_acquired_lock(self):
        from unittest.mock import Mock
        lock = Mock()
        lock.acquire.return_value = False
        with patch("desktop.launcher.subprocess.Popen", side_effect=OSError("not executable")):
            with self.assertRaises(InstallerLaunchError) as result:
                launch_verified_installer(self.installer, self.digest, lock)
        self.assertFalse(result.exception.lock_recovered)

    def test_checksum_failure_does_not_release_lock_or_spawn(self):
        from unittest.mock import Mock
        lock = Mock()
        self.installer.write_bytes(b"changed after download")
        with patch("desktop.launcher.subprocess.Popen") as spawn:
            with self.assertRaises(LauncherError):
                launch_verified_installer(self.installer, self.digest, lock)
        spawn.assert_not_called()
        lock.release.assert_not_called()

if __name__ == "__main__":
    unittest.main()

"""Desktop network settings preserve service ownership and keep entry codes out of diagnostics."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from desktop.core import (LauncherConfig, LauncherError, load_config, network_diagnostics, reader_url,
                          save_config, security_entry_recovery_warnings)
from desktop.runtime import ServerController
from desktop.macos_launcher import LauncherState, text
from desktop.smoke_windows import (Client, network_recovery_smoke, network_settings_smoke,
                                   ports_released, random_ports, sample_png)


class DesktopNetworkSettingsTests(unittest.TestCase):
    def test_old_json_and_positional_arguments_default_share_port_and_new_json_roundtrips(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "launcher.json"
            path.write_text(json.dumps({"library_path": str(root), "port": 18080, "lan_enabled": True}), encoding="utf8")
            self.assertEqual(load_config(path).share_port, 8081)
            old = LauncherConfig(str(root), 18080, True, (str(root),))
            self.assertEqual(old.share_port, 8081)
            config = LauncherConfig(str(root), 18080, True, (str(root),), 18081)
            save_config(path, config)
            self.assertEqual(load_config(path), config)

    def test_share_port_is_bounded_and_different_from_main_port(self):
        for value in (0, 65536, True, False, "8081", 8080):
            with self.subTest(value=value), self.assertRaises(LauncherError):
                LauncherConfig(share_port=value).validate(False)
        for value in (1, 8081, 65535):
            self.assertEqual(LauncherConfig(share_port=value).validate(False).share_port, value)

    def test_legacy_main_port_8081_keeps_working_with_an_omitted_share_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "launcher.json"
            path.write_text(json.dumps({"library_path": str(root), "port": 8081}), encoding="utf-8")
            config = load_config(path)
            self.assertEqual((config.port, config.share_port), (8081, 8082))
            self.assertEqual(LauncherConfig(str(root), 8081).validate().share_port, 8082)
            with self.assertRaises(LauncherError):
                LauncherConfig(str(root), 8081, share_port=8081).validate()
            save_config(path, config)
            self.assertEqual(load_config(path), config)

    def test_private_serve_preserves_legacy_main_port_without_a_share_flag(self):
        from desktop.launcher import main as windows_main
        from desktop.macos_entry import main as mac_main
        root = Path.cwd()
        arguments = ["--serve", "--library", str(root), "--data", str(root / "unused-data"), "--port", "8081"]
        for entry, target in ((windows_main, "desktop.launcher.serve"), (mac_main, "desktop.runtime.serve")):
            with self.subTest(platform=entry.__module__), patch(target, return_value=0) as serve:
                self.assertEqual(entry(arguments), 0)
                config = serve.call_args.args[0]
                self.assertEqual((config.port, config.share_port), (8081, 8082))

    def test_child_command_includes_explicit_share_port_and_never_entry_code(self):
        root = Path.cwd()
        controller = ServerController(root / "unused-data", root / "unused-log", ["fixture-server"])
        command = controller.command(LauncherConfig(str(root), share_port=18081), "private-instance")
        self.assertEqual(command[command.index("--share-port") + 1], "18081")
        self.assertNotIn("--security-entry", command)

    def test_browser_address_reads_current_entry_and_diagnostic_export_defaults_to_root(self):
        root = Path.cwd()
        controller = ServerController(root / "unused-data", root / "unused-log")
        controller.config = LauncherConfig(str(root), 18080, True)
        with patch("network_settings.read_security_entry_path", side_effect=["/first-entry", "/replacement", "/"]):
            self.assertEqual(controller.reader_url, "http://127.0.0.1:18080/first-entry")
            self.assertEqual(controller.reader_url, "http://127.0.0.1:18080/replacement")
            self.assertEqual(controller.reader_url, "http://127.0.0.1:18080/")
        with patch("network_settings.read_security_entry_path") as read, \
                patch("desktop.core.lan_addresses", return_value=["192.168.1.2"]), \
                patch("desktop.core.check_port", return_value={"available": True, "message": "available"}):
            result = network_diagnostics(controller.config)
            read.assert_not_called()
            self.assertEqual(result["local_url"], "http://127.0.0.1:18080/")
            self.assertEqual(result["lan_urls"], ["http://192.168.1.2:18080/"])
        self.assertEqual(reader_url(controller.config), "http://127.0.0.1:18080/")

    def test_offline_recovery_stops_only_owned_controller_before_helper_and_preserves_failure(self):
        root = Path.cwd()
        controller = ServerController(root / "unused-data", root / "unused-log")
        order = []
        with patch.object(controller, "stop", side_effect=lambda: order.append("stop")), \
                patch("network_settings.reset_security_entry", side_effect=lambda path: order.append(path) or {"disabled": True, "changed": True, "backup": "backup"}):
            result = controller.reset_security_entry()
        self.assertEqual(order, ["stop", controller.data_path])
        self.assertTrue(result["disabled"])
        with patch.object(controller, "stop"), \
                patch("network_settings.reset_security_entry", side_effect=RuntimeError("Stop the existing reader first")):
            with self.assertRaisesRegex(LauncherError, "Stop the existing reader first"):
                controller.reset_security_entry()

    def test_recovery_cli_is_headless_and_requires_explicit_data_for_both_platforms(self):
        from desktop.launcher import main as windows_main
        from desktop.macos_entry import main as mac_main
        for entry in (windows_main, mac_main):
            with self.subTest(platform=entry.__module__), patch("sys.stderr", StringIO()):
                with self.assertRaises(SystemExit):
                    entry(["--reset-security-entry"])
            with patch("network_settings.reset_security_entry", return_value={"disabled": True}) as reset, \
                    patch("sys.stdout", StringIO()) as output:
                self.assertEqual(entry(["--reset-security-entry", "--data", str(Path.cwd() / "unused-data")]), 0)
                reset.assert_called_once()
                self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_windows_and_mac_private_serve_accept_custom_share_port(self):
        from desktop.launcher import main as windows_main
        from desktop.macos_entry import main as mac_main
        root = Path.cwd()
        arguments = ["--serve", "--library", str(root), "--data", str(root / "unused-data"),
                     "--port", "18080", "--share-port", "18081"]
        for entry, target in ((windows_main, "desktop.launcher.serve"), (mac_main, "desktop.runtime.serve")):
            with self.subTest(platform=entry.__module__), patch(target, return_value=0) as serve:
                self.assertEqual(entry(arguments), 0)
                self.assertEqual(serve.call_args.args[0].share_port, 18081)

    def test_recovery_warnings_are_localized_and_cli_preserves_optional_notice(self):
        from desktop.runtime import reset_security_entry_cli
        result = {"disabled": True, "warnings": ["已保持独立分享端口，请核对网络设置。", "公网分享地址已清空。"],
                  "warning_codes": ["share_isolation_preserved", "public_share_url_cleared"]}
        self.assertEqual(security_entry_recovery_warnings(result), result["warnings"])
        english = security_entry_recovery_warnings(result, "en")
        self.assertEqual(len(english), 2)
        self.assertIn("will not fall back", english[0])
        self.assertIn("has been cleared", english[1])
        self.assertEqual(security_entry_recovery_warnings({}), [])
        self.assertEqual(security_entry_recovery_warnings({"warnings": None}), [])
        unknown = security_entry_recovery_warnings({"warnings": ["untranslated setting detail"], "warning_codes": ["future_code"]}, "en")
        self.assertEqual(len(unknown), 1)
        self.assertNotIn("untranslated setting detail", unknown[0])
        with patch("network_settings.reset_security_entry", return_value=result), patch("sys.stdout", StringIO()) as output:
            self.assertEqual(reset_security_entry_cli(Path.cwd() / "unused-data"), 0)
        self.assertEqual(json.loads(output.getvalue())["warnings"], result["warnings"])

    def test_mac_state_persists_share_port_and_validates_bad_typed_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "launcher.json"
            save_config(path, LauncherConfig(str(root), share_port=19081))
            controller = MagicMock()
            state = LauncherState(root, path, "0.9.0", controller)
            self.assertEqual(state.share_port, "19081")
            state.start_reader(state.configuration())
            self.assertEqual(load_config(path).share_port, 19081)
            for value in ("0", "65536", "8080", "１９０８１", True, "abc"):
                state.share_port = value
                with self.subTest(value=value), self.assertRaises(LauncherError):
                    state.configuration()
        self.assertEqual(text("share_port", "en"), "Share Port")
        self.assertIn("Restart", text("reset_entry_done", "en"))

    def test_real_owned_service_opens_current_entry_recovers_offline_and_releases_both_ports(self):
        from network_settings import read_security_entry_path
        from server import Library
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            photos, data = root / "photos", root / "data"
            photos.mkdir()
            with socket.socket() as main_socket, socket.socket() as share_socket:
                main_socket.bind(("127.0.0.1", 0))
                share_socket.bind(("127.0.0.1", 0))
                main_port, share_port = main_socket.getsockname()[1], share_socket.getsockname()[1]
            library = Library(photos, data, "", 0)
            try:
                library.network.persist(library.network.prepare({
                    "security_entry_enabled": True, "security_entry_code": "isolated-test-entry",
                    "share_port_enabled": True,
                }))
            finally:
                library.close()
            config = LauncherConfig(str(photos), main_port, share_port=share_port)
            controller = ServerController(data, root / "server.log")
            client = build_opener(ProxyHandler({}))
            def status(url):
                try:
                    response = client.open(url, timeout=3)
                except HTTPError as error:
                    response = error
                with response:
                    response.read()
                    return response.status
            try:
                self.assertEqual(controller.start(config), f"http://127.0.0.1:{main_port}/isolated-test-entry")
                self.assertEqual(status(config.local_url + "api/health"), 200)
                self.assertEqual(status(config.local_url), 404)
                self.assertEqual(status(controller.reader_url), 200)
                self.assertEqual(status(f"http://127.0.0.1:{share_port}/share-reader.js"), 200)
                for route in ("/", "/api/session", "/api/health", "/isolated-test-entry"):
                    with self.subTest(share_route=route):
                        self.assertEqual(status(f"http://127.0.0.1:{share_port}{route}"), 404)
                recovery = controller.reset_security_entry()
                self.assertFalse(controller.running)
                self.assertTrue(Path(recovery["backup"]).is_file())
                self.assertEqual(read_security_entry_path(data), "/")
                for port in (main_port, share_port):
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", port))
                self.assertEqual(controller.start(config), config.local_url)
                self.assertEqual(status(config.local_url), 200)
                self.assertNotIn("isolated-test-entry", (root / "server.log").read_text(encoding="utf8"))
            finally:
                controller.stop()
            for port in (main_port, share_port):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))


class FrozenNetworkSmokeDriverTests(unittest.TestCase):
    """Check that a packaged smoke failure cannot silently become a pass.

    The HTTP fixture is the real source service. Deliberate wire faults below
    simulate missing frozen files or an accidentally broadened listener, rather
    than mirroring the application's path and policy implementation.
    """
    def setUp(self):
        from server import Library, ReaderServer
        self.temporary = tempfile.TemporaryDirectory(prefix='gravelle-network-smoke-driver-')
        self.root = Path(self.temporary.name).resolve()
        self.media, self.data = self.root / 'media', self.root / 'data'
        (self.media / 'Generated work').mkdir(parents=True)
        self.image = sample_png()
        (self.media / 'Generated work/1.png').write_bytes(self.image)
        self.library = Library(self.media, self.data, '', 0)
        self.library.transcoding.auto_start = self.library.image_search.auto_start = False
        main, sharing = random_ports(2)
        self.config = LauncherConfig(str(self.media), main, share_port=sharing)
        self.server = ReaderServer(('127.0.0.1', main), self.library,
                                   share_port=sharing, runtime='desktop')
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.url = self.config.local_url.rstrip('/')
        self.client = Client(self.url)
        self.assertEqual(self.client.request('/api/register', {
            'username': 'smoke-driver', 'password': 'disposable-driver-password'})[0], 201)
        self.library.save_library({'name': 'Generated fixture', 'path': str(self.media), 'media_type': 'photo'})
        deadline = time.monotonic() + 10
        while self.library.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.book = self.library.listing()['books'][0]['id']
        self.stopped = False

    def stop(self):
        if not self.stopped:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(3)
            self.library.close()
            self.stopped = True

    def tearDown(self):
        self.stop()
        self.temporary.cleanup()

    def probe(self):
        return network_settings_smoke(self.client, self.url, self.config, self.book, self.image)

    def test_driver_accepts_real_boundaries_and_recovery_without_reporting_capabilities(self):
        result = self.probe()
        private_code = self.library.network.current()['security_entry_code']
        self.assertTrue(all(result.values()))
        self.assertNotIn(private_code, json.dumps(result))
        self.assertFalse(ports_released(self.config))
        self.stop()
        self.assertTrue(ports_released(self.config))
        # Run the exact CLI mode using source Python on this test host. Real
        # release smoke passes the frozen executable with the same arguments.
        actual_run = subprocess.run
        fake_executable = self.root / 'not-executed-frozen-fixture'
        invoked = []
        def source_cli(command, **options):
            self.assertEqual(command[0], str(fake_executable))
            self.assertEqual(command[1:], ['--reset-security-entry', '--data', str(self.data)])
            invoked.append(command)
            return actual_run([sys.executable, str(Path(__file__).resolve().parents[1] / 'desktop/launcher.py'),
                               *command[1:]], **options)
        with patch('desktop.smoke_windows.subprocess.run', side_effect=source_cli):
            restored = network_recovery_smoke(fake_executable, self.data)
        self.assertEqual(len(invoked), 1)
        self.assertTrue(all(restored.values()))
        self.assertNotIn(private_code, json.dumps(restored))

    def test_driver_rejects_missing_isolated_reader_model_dependency(self):
        original = Client.request
        def missing(client, path, *args, **kwargs):
            if client.url.endswith(':' + str(self.config.share_port)) and path == '/reader-model.js':
                return 404, {}, 'application/json'
            return original(client, path, *args, **kwargs)
        with patch.object(Client, 'request', new=missing), self.assertRaisesRegex(
                AssertionError, 'isolated share reader resource is missing: /reader-model.js'):
            self.probe()

    def test_driver_rejects_exposed_login_on_the_share_listener(self):
        original = Client.request
        def leaked(client, path, *args, **kwargs):
            if client.url.endswith(':' + str(self.config.share_port)) and path == '/api/login':
                return 401, {}, 'application/json'
            return original(client, path, *args, **kwargs)
        with patch.object(Client, 'request', new=leaked), self.assertRaisesRegex(
                AssertionError, 'share listener exposed account login'):
            self.probe()

    def test_driver_rejects_entry_cookie_that_grants_account_access(self):
        original = Client.request
        def privileged(client, path, *args, **kwargs):
            status, value, mime = original(client, path, *args, **kwargs)
            if path == '/api/network-settings' and status == 401 and any(
                    cookie.name == 'gravelle_entry' for cookie in client.cookies):
                return 200, {}, 'application/json'
            return status, value, mime
        with patch.object(Client, 'request', new=privileged), self.assertRaisesRegex(
                AssertionError, 'entry cookie bypassed account authentication'):
            self.probe()


if __name__ == "__main__":
    unittest.main()

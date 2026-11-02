"""Desktop HTTPS contracts; no native macOS or launcher GUI is started."""
from __future__ import annotations

from io import BytesIO, StringIO
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.request import ProxyHandler

from desktop.core import (LauncherConfig, LauncherError, load_config, local_connection_status,
                          network_diagnostics, reader_url, save_config)
from desktop.runtime import ServerController, serve
from desktop.macos_launcher import LauncherState, text


class DesktopHTTPSConfigTests(unittest.TestCase):
    def test_legacy_json_and_positionals_preserve_ports_and_new_port_roundtrips(self):
        with tempfile.TemporaryDirectory(prefix="gravelle-desktop-https-") as temporary:
            root = Path(temporary).resolve()
            path = root / "launcher.json"
            old = {"library_path": str(root), "port": 18080, "share_port": 18081}
            path.write_text(json.dumps(old), encoding="utf8")
            self.assertEqual(load_config(path).https_port, 8443)
            positional = LauncherConfig(str(root), 18080, True, (str(root),), 18081)
            self.assertEqual((positional.port, positional.share_port, positional.https_port), (18080, 18081, 8443))
            current = LauncherConfig(str(root), 18080, True, (str(root),), 18081, 18443)
            save_config(path, current)
            self.assertEqual(load_config(path), current)
            self.assertEqual(json.loads(path.read_text(encoding="utf8"))["https_port"], 18443)

    def test_only_an_omitted_https_port_migrates_around_existing_8443_ports(self):
        for main, share, expected in ((8443, 8081, 8444), (8080, 8443, 8444), (8443, 8444, 8445)):
            with self.subTest(main=main, share=share):
                self.assertEqual(LauncherConfig(port=main, share_port=share).validate(False).https_port, expected)
                with self.assertRaises(LauncherError):
                    LauncherConfig(port=main, share_port=share, https_port=8443).validate(False)

    def test_https_port_rejects_invalid_types_bounds_and_cross_port_collisions(self):
        for value in (0, -1, 65536, True, False, "8443", 8443.0, 8080, 8081):
            with self.subTest(value=value), self.assertRaises(LauncherError):
                LauncherConfig(https_port=value).validate(False)
        for value in (1, 8443, 65535):
            with self.subTest(value=value):
                self.assertEqual(LauncherConfig(https_port=value).validate(False).https_port, value)

    def test_private_children_pass_https_port_without_putting_credentials_on_command_line(self):
        from desktop.launcher import main as windows_main
        from desktop.macos_entry import main as mac_main
        root = Path.cwd()
        config = LauncherConfig(str(root), https_port=18443)
        controller = ServerController(root / "unused-https-data", root / "unused-https-log", ["fixture-child"])
        command = controller.command(config, "private-instance")
        self.assertEqual(command[command.index("--https-port") + 1], "18443")
        self.assertEqual(command[-2:], ["--instance", "private-instance"])
        self.assertFalse(any("certificate" in part or "password" in part for part in command))
        arguments = ["--serve", "--library", str(root), "--data", str(controller.data_path),
                     "--port", "18080", "--share-port", "18081", "--https-port", "18443"]
        for entry, target in ((windows_main, "desktop.launcher.serve"), (mac_main, "desktop.runtime.serve")):
            with self.subTest(platform=entry.__module__), patch(target, return_value=0) as private_serve:
                self.assertEqual(entry(arguments), 0)
                actual = private_serve.call_args.args[0]
                self.assertEqual((actual.port, actual.share_port, actual.https_port), (18080, 18081, 18443))

    def test_private_serve_passes_port_to_server_and_preserves_original_ready_handshake(self):
        with tempfile.TemporaryDirectory(prefix="gravelle-desktop-https-serve-") as temporary:
            root = Path(temporary).resolve()
            config = LauncherConfig(str(root), 18080, https_port=18443)
            with patch("server.Library") as library, patch("server.ReaderServer") as server, \
                    patch("desktop.runtime.threading.Thread"), patch("sys.stdout", StringIO()) as output:
                server.return_value.server_address = ("127.0.0.1", 18080)
                self.assertEqual(serve(config, root / "data", "private-instance"), 0)
                self.assertEqual(server.call_args.kwargs["https_port"], 18443)
                self.assertEqual(json.loads(output.getvalue()), {"ready": True, "instance": "private-instance", "port": 18080})
                library.return_value.close.assert_called_once()
                server.return_value.server_close.assert_called_once()

    def test_mac_state_validates_and_persists_https_port(self):
        with tempfile.TemporaryDirectory(prefix="gravelle-desktop-https-state-") as temporary:
            root = Path(temporary).resolve()
            path = root / "launcher.json"
            save_config(path, LauncherConfig(str(root), https_port=18443))
            state = LauncherState(root, path, "0.9.2", MagicMock())
            self.assertEqual(state.https_port, "18443")
            state.start_reader(state.configuration())
            self.assertEqual(load_config(path).https_port, 18443)
            for value in ("0", "65536", "8080", "8081", "１８４４３", True, "abc"):
                state.https_port = value
                with self.subTest(value=value), self.assertRaises(LauncherError):
                    state.configuration()
        self.assertEqual(text("https_port", "en"), "HTTPS Port")

    def test_port_controls_cannot_change_an_active_or_starting_service_on_either_platform(self):
        from desktop.launcher import LauncherWindow
        from desktop import macos_launcher
        source = Path(macos_launcher.__file__).read_text(encoding="utf8")
        tree = ast.parse(source)
        delegate = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "GravelleMacDelegate")
        method = next(node for node in delegate.body if isinstance(node, ast.FunctionDef) and node.name == "set_busy")
        method.decorator_list = []
        state = SimpleNamespace(controller=SimpleNamespace(running=False), roots=[])
        namespace = {"state": state}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(macos_launcher.__file__), "exec"), namespace)
        for running, busy in ((False, False), (False, True), (True, False), (True, True)):
            enabled = not running and not busy
            with self.subTest(running=running, busy=busy):
                state.controller.running = running
                mac = MagicMock(folder_controls=[])
                namespace["set_busy"](mac, busy)
                for control in (mac.port, mac.share_port, mac.https_port):
                    control.setEnabled_.assert_called_once_with(enabled)
                windows = MagicMock(folder_widgets=[MagicMock(), MagicMock(), MagicMock()], library_values=["root"])
                windows.controller.running = running
                LauncherWindow._set_busy(windows, busy)
                for control in (windows.port_entry, windows.share_port_entry, windows.https_port_entry):
                    control.configure.assert_called_once_with(state="normal" if enabled else "disabled")


class DesktopHTTPSStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gravelle-desktop-https-status-")
        self.addCleanup(temporary.cleanup)
        root = str(Path(temporary.name).resolve())
        self.config = LauncherConfig(library_path=root, library_paths=(root,),
                                     port=18080, https_port=18443)
        self.fallback = {"connection_mode": "http", "https_port": 18443, "https_listening": False}

    def health(self, payload, status=200):
        response = BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf8"))
        response.status = status
        client = MagicMock()
        client.open.return_value = response
        return client

    def test_health_is_bounded_loopback_http_without_environment_proxy_or_redirects(self):
        client = self.health({"status": "ok", "connection_mode": "https", "https_port": 18443,
                              "https_listening": True, "certificate_path": "secret", "certificate_password": "secret"})
        with patch("desktop.core.build_opener", return_value=client) as opener:
            self.assertEqual(local_connection_status(self.config), {"connection_mode": "https", "https_port": 18443, "https_listening": True})
        client.open.assert_called_once_with("http://127.0.0.1:18080/api/health", timeout=.5)
        self.assertIsInstance(opener.call_args.args[0], ProxyHandler)
        self.assertEqual(opener.call_args.args[0].proxies, {})
        self.assertIsNone(opener.call_args.args[1].redirect_request(None, None, 302, "", {}, "https://outside.invalid/"))

    def test_unusable_health_never_claims_https_and_never_exposes_raw_errors(self):
        valid = {"status": "ok", "connection_mode": "https", "https_port": 18443, "https_listening": True}
        values = (b"not-json", b" " * 16385, [], {**valid, "status": "bad"},
                  {**valid, "https_port": 8443}, {**valid, "https_port": "18443"},
                  {**valid, "https_port": True}, {**valid, "connection_mode": "other"},
                  {**valid, "connection_mode": []})
        for payload in values:
            with self.subTest(payload=str(payload)[:100]), patch("desktop.core.build_opener", return_value=self.health(payload)):
                self.assertEqual(local_connection_status(self.config), self.fallback)
        with patch("desktop.core.build_opener", return_value=self.health(valid, 503)):
            self.assertEqual(local_connection_status(self.config), self.fallback)
        with patch("desktop.core.build_opener", side_effect=URLError("secret certificate password")):
            self.assertEqual(local_connection_status(self.config), self.fallback)
        for mode, listening in (("http", True), ("proxy", True), ("https", "true"), ("https", 1), ("https", False)):
            with self.subTest(mode=mode, listening=listening), patch("desktop.core.build_opener", return_value=self.health({**valid, "connection_mode": mode, "https_listening": listening})):
                self.assertFalse(local_connection_status(self.config)["https_listening"])

    def test_browser_stays_http_for_local_administration_and_ignores_tls_identity(self):
        root = Path.cwd()
        controller = ServerController(root / "unused-https-data", root / "unused-https-log")
        controller.config = self.config
        controller.process = SimpleNamespace(poll=lambda: None)
        with patch("network_settings.read_security_entry_path", return_value="/current-entry"), \
                patch("desktop.core.local_connection_status") as health:
            self.assertEqual(controller.reader_url, "http://127.0.0.1:18080/current-entry")
            health.assert_not_called()
            self.assertEqual(reader_url(self.config, controller.data_path, https_ready=True), "https://127.0.0.1:18443/current-entry")

    def test_diagnostics_explain_listener_separately_and_keep_default_http_recovery(self):
        for ready in (False, True):
            with self.subTest(ready=ready), patch("desktop.core.local_connection_status", return_value={"connection_mode": "https", "https_port": 18443, "https_listening": ready}), \
                    patch("desktop.core.socket.create_connection"), patch("network_settings.read_security_entry_path") as read:
                result = network_diagnostics(self.config, running=True)
                read.assert_not_called()
                self.assertEqual(result["local_url"], "http://127.0.0.1:18080/")
                self.assertEqual(result["https_url"], "https://127.0.0.1:18443/" if ready else "")
                self.assertEqual(result["https_listening"], ready)
                self.assertIn("正在监听" if ready else "恢复地址", result["message"])
                self.assertNotIn("certificate", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

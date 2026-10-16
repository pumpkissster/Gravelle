"""macOS choices and decoder guards simulated without touching a real library.

These tests run on Windows/Linux as well as macOS. They verify contracts and
owned child-process behavior; they do not claim native AppKit or bundle QA.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.core import LauncherConfig, LauncherError, ReleaseConfig, download_update, load_release_config
from desktop.platforms import mac_architecture, macos_update_asset, user_directory
from desktop.runtime import ServerController
from image_search import ImageSearchManager
import server
from server import ReaderError
from transcoding import DEFAULT_RULES, TranscodingManager, _DarwinMemoryGuard, _restrict_worker, _worker_exit_message


class MacOSRuntimeContracts(unittest.TestCase):
    def test_macos_data_stays_in_user_application_support(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "Mac 用户"
            result = user_directory("darwin", home, {"LOCALAPPDATA": "ignored", "XDG_DATA_HOME": "ignored"})
            self.assertEqual(result, home / "Library" / "Application Support" / "Gravelle")
            self.assertFalse(result.exists())

    def test_windows_and_linux_defaults_are_independent_of_macos(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(user_directory("win32", home, {}), home / "AppData" / "Local" / "Gravelle")
            self.assertEqual(user_directory("linux", home, {}), home / ".local" / "share" / "Gravelle")
            self.assertEqual(user_directory("linux", home, {"XDG_DATA_HOME": "relative"}),
                             home / ".local" / "share" / "Gravelle")

    def test_mac_architecture_assets_are_explicit_and_never_windows_executables(self):
        for name in ("arm64", "aarch64", "ARM64"):
            self.assertEqual(mac_architecture(name), "arm64")
            self.assertEqual(macos_update_asset(name), "Gravelle-macOS-{version}-arm64.dmg")
        for name in ("x86_64", "AMD64", "x64"):
            self.assertEqual(mac_architecture(name), "x64")
            self.assertEqual(macos_update_asset(name), "Gravelle-macOS-{version}-x64.dmg")
        for name in ("i386", "ppc", "../../bad", "", 64):
            with self.subTest(name=name), self.assertRaises(ValueError):
                mac_architecture(name)

    def test_installed_config_selects_current_mac_architecture(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            (root / "desktop").mkdir()
            (root / "desktop" / "release.json").write_text(json.dumps({
                "repository": "pumpkissster/Gravelle", "asset_name": "Windows-{version}.exe"}), encoding="utf-8")
            for machine, expected in (("arm64", "arm64"), ("x86_64", "x64")):
                with patch("desktop.core.sys.platform", "darwin"), \
                        patch("desktop.platforms.host_platform.machine", return_value=machine):
                    config = load_release_config(root)
                self.assertEqual(config.platform, "macos")
                self.assertEqual(config.asset_name, "Gravelle-macOS-{version}-" + expected + ".dmg")

    def test_macos_release_rejects_executable_or_untrusted_configuration(self):
        for asset, platform in (("Gravelle.exe", "macos"), ("Gravelle.dmg", "windows"),
                                ("Gravelle.dmg", "other"), ("../Gravelle.dmg", "macos")):
            with self.subTest(asset=asset, platform=platform), self.assertRaises(LauncherError):
                ReleaseConfig("pumpkissster/Gravelle", asset_name=asset, platform=platform).validate()
        with self.assertRaises(LauncherError):
            ReleaseConfig("https://evil.example/Gravelle", asset_name="Gravelle.dmg", platform="macos").validate()

    def test_mac_download_uses_the_exact_architecture_and_checksum(self):
        image = b"disposable disk-image fixture, never mounted or executed"
        digest = hashlib.sha256(image).hexdigest()
        filename = "Gravelle-macOS-0.9.0-arm64.dmg"
        urls = []
        def download(url, maximum, destination=None):
            urls.append(url)
            if destination is None:
                return (digest + "  " + filename + "\n").encode()
            destination.write(image)
        with tempfile.TemporaryDirectory() as temporary, patch("desktop.core._download", side_effect=download):
            config = ReleaseConfig("pumpkissster/Gravelle", macos_update_asset("arm64"), platform="macos")
            result = download_update(config, {"tag": "v0.9.0"}, Path(temporary))
            self.assertEqual(Path(result["path"]).read_bytes(), image)
            self.assertEqual(Path(result["path"]).name, filename)
            self.assertEqual(result["sha256"], digest)
            self.assertFalse(list(Path(temporary).glob("*.part")))
        self.assertEqual(urls, ["https://github.com/pumpkissster/Gravelle/releases/download/v0.9.0/SHA256SUMS.txt",
                                "https://github.com/pumpkissster/Gravelle/releases/download/v0.9.0/" + filename])

    def test_frozen_mac_service_reenters_same_bundle_without_windows_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            second = root / "写真 二"
            second.mkdir()
            executable = str(root / "Gravelle 测试.app" / "Contents" / "MacOS" / "Gravelle")
            config = LauncherConfig(library_paths=[str(root), str(second)], port=19543)
            controller = ServerController(root / "data", root / "server.log")
            with patch("desktop.runtime.sys.platform", "darwin"), \
                    patch("desktop.runtime.sys.frozen", True, create=True), \
                    patch("desktop.runtime.sys.executable", executable):
                command = controller.command(config, "fixture-instance")
            self.assertEqual(command[0], executable)
            self.assertEqual(command[1], "--serve")
            self.assertEqual(command.count("--library"), 2)
            self.assertIn(str(second.resolve()), command)
            self.assertFalse(any("GravelleServer.exe" in value for value in command))
            self.assertEqual(command[-2:], ["--instance", "fixture-instance"])

    def test_unfrozen_mac_service_uses_headless_entry_and_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = LauncherConfig(str(root))
            with patch("desktop.runtime.sys.platform", "darwin"), \
                    patch("desktop.runtime.sys.frozen", False, create=True):
                command = ServerController(root / "data", root / "log").command(config, "instance")
                self.assertEqual(Path(command[1]).name, "macos_entry.py")
                custom = ServerController(root / "data", root / "log", command_prefix=["custom-fixture"])
                self.assertEqual(custom.command(config, "instance")[0], "custom-fixture")


class MacOSNetworkContracts(unittest.TestCase):
    def test_default_route_commands_are_fixed_local_and_bounded(self):
        results = [SimpleNamespace(returncode=0, stdout="   route to: default\n interface: en0\n"),
                   SimpleNamespace(returncode=0, stdout="192.168.2.9\n")]
        with patch("subprocess.run", side_effect=results) as run:
            self.assertEqual(server._macos_default_ipv4(), ["192.168.2.9"])
        self.assertEqual(run.call_args_list[0].args[0], ["/sbin/route", "-n", "get", "default"])
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/sbin/ipconfig", "getifaddr", "en0"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 2)
            self.assertFalse(call.kwargs.get("shell", False))
            self.assertEqual(call.kwargs["env"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})

    def test_malformed_route_interface_is_never_executed(self):
        for value in ("en0;touch", "../en0", "en0\ninterface: en1", "en0 --evil"):
            result = SimpleNamespace(returncode=0, stdout="interface: " + value + "\n")
            with self.subTest(value=value), patch("subprocess.run", return_value=result) as run:
                self.assertEqual(server._macos_default_ipv4(), [])
                self.assertEqual(run.call_count, 1)

    def test_missing_route_timeout_and_nonprivate_addresses_are_empty(self):
        for error in (FileNotFoundError("fixture"), subprocess.TimeoutExpired("fixture", 2)):
            with self.subTest(error=type(error)), patch("subprocess.run", side_effect=error):
                self.assertEqual(server._macos_default_ipv4(), [])
        for address in ("127.0.0.1", "169.254.1.2", "8.8.8.8", "::1", "fc00::1", "not-an-address"):
            results = [SimpleNamespace(returncode=0, stdout="interface: en0\n"),
                       SimpleNamespace(returncode=0, stdout=address)]
            with self.subTest(address=address), patch("subprocess.run", side_effect=results):
                self.assertEqual(server._macos_default_ipv4(), [])

    def test_darwin_dispatch_does_not_attempt_linux_proc_routes(self):
        with patch("server.sys.platform", "darwin"), \
                patch("server._macos_default_ipv4", return_value=["10.0.1.5"]) as mac, \
                patch("server._linux_default_ipv4", side_effect=AssertionError("Linux route was used")):
            self.assertEqual(server._lan_ipv4_addresses(), ["10.0.1.5"])
            mac.assert_called_once_with()


class MacOSDecoderBudgetContracts(unittest.TestCase):
    def test_guard_uses_bytes_keeps_under_budget_worker_and_closes_thread(self):
        exits = []
        with patch.object(_DarwinMemoryGuard, "interval", .001):
            guard = _DarwinMemoryGuard(1024, measure=lambda: 1000, exit_process=exits.append)
            try:
                self.assertEqual(guard._read_bytes(), 1000)
                self.assertFalse(guard.stopped.wait(.01))
                self.assertEqual(exits, [])
            finally:
                guard.close()
        self.assertFalse(guard.thread.is_alive())

    def test_guard_refuses_invalid_or_already_exhausted_budget_before_decode(self):
        exits = []
        for value in (0, -1, True, "1024"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _DarwinMemoryGuard(value, measure=lambda: 1, exit_process=exits.append)
        with self.assertRaises(MemoryError):
            _DarwinMemoryGuard(1024, measure=lambda: 1025, exit_process=exits.append)
        self.assertEqual(exits, [])

    def test_sampler_exits_only_owned_worker_when_resident_budget_is_exceeded(self):
        values = iter((1000, 1025))
        finished = threading.Event()
        exits = []
        def exit_process(code):
            exits.append(code)
            finished.set()
        with patch.object(_DarwinMemoryGuard, "interval", .001):
            guard = _DarwinMemoryGuard(1024, measure=lambda: next(values), exit_process=exit_process)
            try:
                self.assertTrue(finished.wait(1))
                self.assertEqual(exits, [86])
            finally:
                guard.close()

    def test_sampler_fails_closed_if_measurement_becomes_unavailable(self):
        calls = 0
        exits = []
        finished = threading.Event()
        def measure():
            nonlocal calls
            calls += 1
            if calls == 1:
                return 1
            raise OSError("disposable measurement failure")
        def exit_process(code):
            exits.append(code)
            finished.set()
        with patch.object(_DarwinMemoryGuard, "interval", .001):
            guard = _DarwinMemoryGuard(1024, measure=measure, exit_process=exit_process)
            try:
                self.assertTrue(finished.wait(1))
                self.assertEqual(exits, [87])
            finally:
                guard.close()

    def test_darwin_selects_sampler_linux_retains_address_space_hard_limit(self):
        import transcoding
        with patch("transcoding.sys.platform", "darwin"), patch("transcoding._DarwinMemoryGuard") as guard:
            self.assertIs(_restrict_worker(2048), guard.return_value)
            guard.assert_called_once_with(2048)
        resource = SimpleNamespace(RLIMIT_AS=9, setrlimit=MagicMock())
        with patch("transcoding.sys.platform", "linux"), \
                patch("transcoding.os", SimpleNamespace(name="posix")), \
                patch.dict(sys.modules, {"resource": resource}), patch("transcoding._DarwinMemoryGuard") as guard:
            self.assertIsNone(transcoding._restrict_worker(2048))
            resource.setrlimit.assert_called_once_with(9, (2048, 2048))
            guard.assert_not_called()

    def test_windows_job_object_limit_remains_separate(self):
        kernel = MagicMock()
        kernel.CreateJobObjectW.return_value = 123
        kernel.SetInformationJobObject.return_value = 1
        kernel.AssignProcessToJobObject.return_value = 1
        with patch("transcoding.sys.platform", "win32"), \
                patch("transcoding.os", SimpleNamespace(name="nt")), \
                patch("ctypes.WinDLL", return_value=kernel, create=True), \
                patch("transcoding._DarwinMemoryGuard") as guard:
            self.assertEqual(_restrict_worker(2048), 123)
            kernel.AssignProcessToJobObject.assert_called_once()
            guard.assert_not_called()

    def test_real_disposable_child_guard_exit_does_not_terminate_test_parent(self):
        code = ("import time; from transcoding import _DarwinMemoryGuard; "
                "values=iter((1,1025)); guard=_DarwinMemoryGuard(1024,measure=lambda:next(values)); "
                "time.sleep(2); raise SystemExit(99)")
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 86, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")

    def test_index_query_and_reusable_pipe_map_only_new_guard_exit_codes(self):
        manager = object.__new__(ImageSearchManager)
        manager._error = ReaderError
        manager._current = lambda generation, connection: True
        for code in (86, 87):
            expected = _worker_exit_message(code)
            self.assertIsNotNone(expected)
            with self.subTest(code=code), self.assertRaises(ReaderError) as caught:
                manager._parse_worker_result(b"", returncode=code)
            self.assertEqual(str(caught.exception), expected)
            responses = Queue()
            responses.put(None)
            process = MagicMock()
            process.poll.return_value = None
            process.wait.return_value = code
            session = SimpleNamespace(responses=responses, process=process)
            with self.assertRaises(ReaderError) as caught:
                manager._receive_index(session, time.monotonic() + 1, 1, None)
            self.assertEqual(str(caught.exception), expected)
            process.wait.assert_called_once_with(timeout=.2)
        for code in (None, 0, 1, -9, 255):
            self.assertIsNone(_worker_exit_message(code))

    def test_optimization_parent_reports_guard_exit_and_removes_partial_job(self):
        source = b"disposable source fixture, never written to a real library"
        @contextmanager
        def open_source(job, key):
            with BytesIO(source) as stream:
                yield stream, "fixture-version"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for code in (86, 87):
                manager = object.__new__(TranscodingManager)
                connection = MagicMock()
                connection.execute.return_value.fetchone.return_value = (0,)
                manager.library = SimpleNamespace(connect=lambda: connection)
                manager._error = ReaderError
                manager._storage_directory = lambda book_id: root
                manager._open_source = open_source
                manager._job_current = lambda book_id, generation: True
                manager.lock = threading.RLock()
                manager.processes = set()
                process = MagicMock()
                process.returncode = code
                process.poll.return_value = code
                process.wait.return_value = code
                rules = {**DEFAULT_RULES, "min_free_space_mb": 0, "storage_limit_mb": 0}
                job = {"id": "fixture-book", "generation": 1, "inspect_error": "", "page_key": "fixture.png",
                       "source_version": "fixture-version", "desired_profile": "fixture-profile"}
                with self.subTest(code=code), patch("transcoding.subprocess.Popen", return_value=process), \
                        self.assertRaises(ReaderError) as caught:
                    manager._convert(job, rules)
                self.assertEqual(str(caught.exception), _worker_exit_message(code))
                self.assertEqual(manager.processes, set())
                self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

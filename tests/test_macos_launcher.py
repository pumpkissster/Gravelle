"""Portable contracts for the native macOS launcher; no fake rendering claims."""
from __future__ import annotations

import hashlib
import ast
import importlib
import io
import json
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from desktop.core import LauncherConfig, LauncherError
from desktop.macos_entry import main, validate_smoke_paths
from desktop.macos_launcher import (LauncherState, normalise_roots, preferred_language, text,
                                    verified_disk_image, window_size)


class OwnedController:
    def __init__(self):
        self.running, self.config, self.process = False, None, None
        self.starts = []

    def start(self, config):
        self.starts.append(config)
        self.config, self.running = config, True
        return config.local_url


def termination_contract():
    """Execute the shipped lifecycle methods with a portable AppKit host.

    Only event routing is mocked; this does not exercise native rendering or
    claim that a macOS run loop was started on another operating system.
    """
    from desktop import macos_launcher
    source = ast.parse(Path(macos_launcher.__file__).read_text(encoding="utf-8"))
    delegate = next(node for node in ast.walk(source)
                    if isinstance(node, ast.ClassDef) and node.name == "GravelleMacDelegate")
    selected = {"quit_", "wake_for_worker", "begin_exit", "poll_", "applicationShouldTerminate_"}
    methods = [node for node in delegate.body if isinstance(node, ast.FunctionDef) and node.name in selected]
    for method in methods:
        method.decorator_list = []
    state = types.SimpleNamespace(events=queue.Queue(), worker=MagicMock(),
                                  controller=types.SimpleNamespace(stop=MagicMock(), process=None, running=False))
    app = MagicMock()
    namespace = {"queue": queue, "state": state, "app": app, "tr": lambda key, **values: key,
                 "A": types.SimpleNamespace(NSTerminateLater=2, NSTerminateCancel=0,
                                             NSModalPanelRunLoopMode="NSModalPanelRunLoopMode"),
                 "F": types.SimpleNamespace(NSRunLoopCommonModes="NSRunLoopCommonModes")}
    module = ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[]))
    exec(compile(module, str(macos_launcher.__file__), "exec"), namespace)
    subject = types.SimpleNamespace(busy=False, quitting=False, exit_started=False, updating=False,
                                    status=MagicMock(), update_button=MagicMock(), restore_=MagicMock(),
                                    openBrowser_=MagicMock(), window=MagicMock(), address=MagicMock(),
                                    performSelectorOnMainThread_withObject_waitUntilDone_modes_=MagicMock())
    subject.set_busy = lambda busy: setattr(subject, "busy", busy)
    for name in selected:
        setattr(subject, name, types.MethodType(namespace[name], subject))
    return subject, state, app


class MacLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gravelle-macos-contract-")
        self.root = Path(self.temporary.name).resolve()
        self.photos = self.root / "写真"
        self.photos.mkdir()
        self.storage = self.root / "isolated-launcher"
        self.config = self.storage / "launcher.json"

    def tearDown(self):
        self.temporary.cleanup()

    def state(self, controller=None):
        result = LauncherState(self.storage, self.config, "0.8.6", controller)
        result.roots = [str(self.photos)]
        return result

    def test_imports_do_not_load_native_gui_or_create_a_window(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import desktop.macos_entry; import desktop.macos_launcher; print(','.join(n for n in ('AppKit','Foundation','Quartz','objc','tkinter') if n in sys.modules))"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_language_follows_first_system_preference(self):
        for languages, expected in ((["zh-Hans-CN", "en"], "zh"), (["zh_Hant_TW"], "zh"),
                                    (["en-GB", "zh"], "en"), ([], "en"), ([None, "fr"], "en")):
            with self.subTest(languages=languages):
                self.assertEqual(preferred_language(languages), expected)
        self.assertEqual(text("version", "en", version="0.8.6"), "Version 0.8.6")
        self.assertEqual(text("check_update", "zh"), "检查更新")

    def test_window_dimensions_use_points_and_fit_small_displays(self):
        self.assertEqual(window_size(3840, 2160), (720, 740))
        self.assertEqual(window_size(1280, 800), (720, 740))
        self.assertEqual(window_size(640, 480), (600, 420))

    def test_multiple_roots_are_canonical_and_deduplicated(self):
        second = self.root / "另一目录"
        second.mkdir()
        values = normalise_roots([str(self.photos), str(self.photos / ".." / "写真"), str(second)])
        self.assertEqual(values, (str(self.photos), str(second)))
        state = self.state()
        state.add_roots([str(second), str(self.photos)])
        self.assertEqual(state.roots, list(values))
        state.remove_root(0)
        state.remove_root(0)
        self.assertEqual(state.roots, [str(second)])

    def test_invalid_addition_is_atomic_and_does_not_replace_valid_roots(self):
        state = self.state()
        with self.assertRaises(LauncherError):
            state.add_roots([str(self.root / "missing")])
        self.assertEqual(state.roots, [str(self.photos)])
        with self.assertRaises(LauncherError):
            normalise_roots([str(self.root / str(index)) for index in range(21)])

    def test_native_folder_replacement_validates_before_changing_state(self):
        state = self.state()
        with self.assertRaises(LauncherError):
            state.replace_root(0, "relative/path")
        self.assertEqual(state.roots, [str(self.photos)])

    def test_invalid_port_is_rejected_before_service_start(self):
        state = self.state()
        for value in ("0", "65536", "12.5", "-1", "８０８０", "8080x", "", True):
            state.port = value
            with self.subTest(value=value), self.assertRaises(LauncherError):
                state.configuration()
        state.port = " 18081 "
        state.lan_enabled = True
        config = state.configuration()
        self.assertEqual(config.port, 18081)
        self.assertEqual(config.host, "0.0.0.0")

    def test_start_persists_roots_and_uses_only_owned_controller(self):
        owned = OwnedController()
        state = self.state(owned)
        state.port = "18089"
        url = state.start_reader(state.configuration())
        self.assertEqual(url, "http://127.0.0.1:18089/")
        self.assertEqual(len(owned.starts), 1)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["library_paths"], [str(self.photos)])
        self.assertEqual(saved["port"], 18089)
        restored = LauncherState(self.storage, self.config, "0.8.6", owned)
        self.assertEqual(restored.roots, state.roots)
        self.assertEqual(restored.port, "18089")

    def test_worker_reports_failure_without_touching_native_controls(self):
        state = self.state()
        def failed():
            raise LauncherError("generated failure")
        state.worker("test", failed)
        self.assertEqual(state.events.get(timeout=2), ("test", False, "generated failure"))

    def test_worker_notifies_after_queuing_success_and_failure(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                state = self.state()
                delivered, woke = [], threading.Event()
                def completed():
                    delivered.append(state.events.get_nowait())
                    woke.set()
                def action():
                    if not succeeds:
                        raise LauncherError("generated failure")
                    return "generated result"
                state.worker_completed = completed
                state.worker("test", action)
                self.assertTrue(woke.wait(2), "A completion must wake the termination run loop")
                self.assertEqual(delivered, [("test", succeeds, "generated result" if succeeds else "generated failure")])

    def test_worker_completion_targets_termination_and_normal_run_loop_modes(self):
        subject, _, _ = termination_contract()
        subject.wake_for_worker()
        subject.performSelectorOnMainThread_withObject_waitUntilDone_modes_.assert_called_once_with(
            "poll:", None, False, ["NSModalPanelRunLoopMode", "NSRunLoopCommonModes"])

    def test_menu_quit_uses_application_termination_path(self):
        subject, _, app = termination_contract()
        subject.quit_(None)
        app.terminate_.assert_called_once_with(None)

    def test_idle_quit_waits_for_owned_stop_completion_and_runs_only_once(self):
        subject, state, app = termination_contract()
        self.assertEqual(subject.applicationShouldTerminate_(app), 2)
        self.assertEqual(subject.applicationShouldTerminate_(app), 2)
        self.assertTrue(subject.quitting)
        self.assertTrue(subject.busy)
        state.worker.assert_called_once_with("exit", state.controller.stop)
        state.controller.stop.assert_not_called()
        app.replyToApplicationShouldTerminate_.assert_not_called()
        state.events.put(("exit", True, None))
        subject.poll_(None)
        app.replyToApplicationShouldTerminate_.assert_called_once_with(True)

    def test_busy_quit_is_retained_until_foreground_operation_completes(self):
        for operation in ("start", "stop", "reset", "diagnose", "smoke-start"):
            for succeeds in (True, False):
                with self.subTest(operation=operation, succeeds=succeeds):
                    subject, state, app = termination_contract()
                    subject.busy, subject.updating = True, True
                    self.assertEqual(subject.applicationShouldTerminate_(app), 2)
                    state.worker.assert_not_called()
                    subject.status.setStringValue_.assert_called_with("quitting_after_operation")
                    state.events.put(("update", True, {"available": False}))
                    state.events.put(("background-diagnose", True, {}))
                    subject.poll_(None)
                    state.worker.assert_not_called()
                    state.events.put((operation, succeeds, "generated result"))
                    subject.poll_(None)
                    state.worker.assert_called_once_with("exit", state.controller.stop)
                    self.assertTrue(subject.busy)
                    self.assertTrue(subject.quitting)
                    subject.openBrowser_.assert_not_called()
                    subject.window.orderOut_.assert_not_called()
                    app.replyToApplicationShouldTerminate_.assert_not_called()

    def test_shutdown_failure_cancels_pending_termination_and_can_be_retried(self):
        subject, state, app = termination_contract()
        subject.applicationShouldTerminate_(app)
        state.events.put(("exit", False, "generated shutdown failure"))
        subject.poll_(None)
        self.assertFalse(subject.quitting)
        self.assertFalse(subject.exit_started)
        self.assertFalse(subject.busy)
        subject.status.setStringValue_.assert_called_with("generated shutdown failure")
        app.replyToApplicationShouldTerminate_.assert_called_once_with(False)
        self.assertEqual(subject.applicationShouldTerminate_(app), 2)
        self.assertEqual(state.worker.call_count, 2)

    def test_corrupt_config_is_visible_and_does_not_overwrite_it(self):
        self.storage.mkdir()
        self.config.write_text("{broken", encoding="utf-8")
        state = LauncherState(self.storage, self.config, "0.8.6", OwnedController())
        self.assertTrue(state.configuration_error)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{broken")

    def test_verified_dmg_rechecks_bytes_and_contained_path(self):
        updates = self.storage / "updates"
        updates.mkdir(parents=True)
        disk_image = updates / "Gravelle-macOS-0.8.7-arm64.dmg"
        payload = b"generated test asset, never mounted"
        disk_image.write_bytes(payload)
        downloaded = {"path": str(disk_image), "sha256": hashlib.sha256(payload).hexdigest()}
        self.assertEqual(verified_disk_image(downloaded, updates), disk_image)
        disk_image.write_bytes(payload + b"tampered")
        with self.assertRaises(LauncherError):
            verified_disk_image(downloaded, updates)
        disk_image.write_bytes(payload)
        with self.assertRaises(LauncherError):
            verified_disk_image(downloaded, self.root)
        with self.assertRaises(LauncherError):
            verified_disk_image({"path": str(disk_image), "sha256": "invalid"}, updates)

    def test_update_requires_discovered_trusted_release(self):
        state = self.state()
        with patch("desktop.macos_launcher.download_update") as download:
            with self.assertRaises(LauncherError):
                state.download_release()
            download.assert_not_called()

    def test_worker_modes_dispatch_before_cocoa_or_instance_lock(self):
        for mode, module in (("--transcode-worker", "transcoding"), ("--image-search-worker", "image_search")):
            worker = MagicMock(return_value=7)
            fake = types.SimpleNamespace(worker_main=worker)
            with self.subTest(mode=mode), patch.dict(sys.modules, {module: fake}), patch("desktop.macos_entry.restore_private_stdio") as restore:
                self.assertEqual(main([mode, "--generated-argument"]), 7)
                restore.assert_called_once_with()
                worker.assert_called_once_with(["--generated-argument"])

    def test_private_serve_passes_all_roots_and_instance(self):
        serve = MagicMock(return_value=0)
        runtime = types.SimpleNamespace(serve=serve)
        second = self.root / "Second"
        with patch.dict(sys.modules, {"desktop.runtime": runtime}), patch("desktop.macos_entry.restore_private_stdio"):
            self.assertEqual(main(["--serve", "--library", str(self.photos), "--library", str(second),
                                   "--data", str(self.storage), "--port", "18091", "--instance", "nonce"]), 0)
        config, data, instance = serve.call_args.args
        self.assertEqual(config.library_paths, (str(self.photos), str(second)))
        self.assertEqual(data, self.storage)
        self.assertEqual(instance, "nonce")

    def test_gui_smoke_requires_explicit_isolated_paths(self):
        report = self.storage / "gui-report.json"
        real = self.root / "real-account"
        with patch("desktop.core.user_directory", return_value=real):
            self.assertEqual(validate_smoke_paths(self.storage, self.config, report), (self.storage, self.config, report))
            for storage, config, output in ((real, real / "launcher.json", real / "gui.json"),
                                             (self.root, self.root / "launcher.json", self.root / "gui.json"),
                                             (self.storage, self.config, self.root / "outside.json"),
                                             (self.storage, self.config, self.config)):
                with self.subTest(storage=storage, output=output), self.assertRaises(ValueError):
                    validate_smoke_paths(storage, config, output)
        (self.storage / "data").mkdir(parents=True)
        (self.storage / "data" / "library.sqlite3").write_bytes(b"generated sentinel")
        with self.assertRaises(ValueError):
            validate_smoke_paths(self.storage, self.config, report)

    def test_smoke_cli_rejects_missing_paths_without_loading_appkit(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--smoke-launcher"])
        self.assertEqual(caught.exception.code, 2)

    def test_smoke_scenarios_are_forwarded_only_in_explicit_isolated_smoke_mode(self):
        for scenario in ("idle", "running", "hidden"):
            with self.subTest(scenario=scenario), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["--smoke-scenario", scenario])
            self.assertEqual(caught.exception.code, 2)
            report = self.storage / "gui-report.json"
            with self.subTest(scenario=scenario), patch("sys.platform", "darwin"), patch("desktop.macos_launcher.run_gui", return_value=0) as gui:
                self.assertEqual(main(["--smoke-launcher", "--data", str(self.storage), "--config", str(self.config),
                                       "--report", str(report), "--smoke-scenario", scenario]), 0)
                self.assertEqual(gui.call_args.kwargs, {"smoke_report": report, "smoke_scenario": scenario})

    @unittest.skipIf(sys.platform == "darwin", "Requires a non-macOS host to verify honest unsupported behavior")
    def test_gui_smoke_on_windows_does_not_claim_native_validation(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--smoke-launcher", "--data", str(self.storage), "--config", str(self.config),
                  "--report", str(self.storage / "gui-report.json")])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.storage / "gui-report.json").exists())


if __name__ == "__main__":
    unittest.main()

"""Portable contracts for the native macOS launcher; no fake rendering claims."""
from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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

    @unittest.skipIf(sys.platform == "darwin", "Requires a non-macOS host to verify honest unsupported behavior")
    def test_gui_smoke_on_windows_does_not_claim_native_validation(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--smoke-launcher", "--data", str(self.storage), "--config", str(self.config),
                  "--report", str(self.storage / "gui-report.json")])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.storage / "gui-report.json").exists())


if __name__ == "__main__":
    unittest.main()

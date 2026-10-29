"""Pinned offline face-detector resources survive each distribution layout."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import avatar_face_crop

COMMIT = "f12e12798e8314f7c074a6656816c048dcc95b7a"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
LICENSE_SHA256 = "c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958"
RESOURCES = {
    "models/yunet/face_detection_yunet_2023mar.onnx",
    "models/yunet/LICENSE",
    "models/yunet/provenance.json",
    "models/yunet/README.md",
}


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AvatarFacePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gravelle-avatar-package-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_bundled_bytes_match_official_lfs_pin_and_complete_mit_notice(self):
        directory = ROOT / "models" / "yunet"
        metadata = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
        model = (directory / "face_detection_yunet_2023mar.onnx").read_bytes()
        notice = (directory / "LICENSE").read_bytes()
        self.assertEqual(len(model), 232589)
        self.assertFalse(model.startswith(b"version https://git-lfs"))
        self.assertEqual(hashlib.sha256(model).hexdigest(), MODEL_SHA256)
        self.assertEqual(metadata["sha256"], MODEL_SHA256)
        self.assertEqual(metadata["size_bytes"], len(model))
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual(metadata["repository"], "https://github.com/opencv/opencv_zoo")
        self.assertIn("/" + COMMIT + "/", metadata["source_url"])
        self.assertTrue(metadata["source_url"].startswith("https://media.githubusercontent.com/media/opencv/opencv_zoo/"))
        self.assertEqual(metadata["license"], "MIT")
        self.assertEqual(hashlib.sha256(notice).hexdigest(), LICENSE_SHA256)
        self.assertEqual(metadata["license_sha256"], LICENSE_SHA256)
        self.assertIn(b"Copyright (c) 2020 Shiqi Yu <shiqi.yu@gmail.com>", notice)
        self.assertIn(b"THE SOFTWARE IS PROVIDED \"AS IS\"", notice)
        self.assertFalse(metadata["runtime_downloads"])
        self.assertEqual({path.name for path in directory.iterdir()},
                         {Path(name).name for name in RESOURCES})

    def test_real_source_export_preserves_model_and_notice_bytes_without_qa_samples(self):
        exporter = load_script("gravelle_avatar_source_export", "packaging/package_source.py")
        names = exporter.source_files()
        self.assertTrue(RESOURCES <= set(names))
        for name in ("avatar_face_crop.py", "tests/test_avatar_face_crop.py",
                     "tests/test_avatar_face_worker.py", "tests/test_avatar_face_packaging.py",
                     "tests/test_model_avatar_regeneration.py", "tests/test_model_avatar_ui.js"):
            self.assertIn(name, names)
        self.assertFalse(any(name.startswith("work/") or "lena" in name.lower() for name in names))
        archive_path = self.root / "resources.zip"
        exporter.archive(archive_path, sorted(RESOURCES), "gravelle-reader/")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {"gravelle-reader/" + name for name in RESOURCES})
            for name in RESOURCES:
                self.assertEqual(archive.read("gravelle-reader/" + name), (ROOT / name).read_bytes())

    def test_docker_context_copies_the_actual_model_with_notice_and_provenance(self):
        checker = load_script("gravelle_avatar_context", "packaging/check_docker_context.py")
        report = checker.validate()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(RESOURCES <= set(report["files"]))
        self.assertIn("avatar_face_crop.py", report["files"])
        self.assertIn("models/yunet", report["copy_sources"])
        self.assertTrue(report["private_data_excluded"])

    def _freeze_configuration(self, filename):
        class Captured(Exception):
            pass

        captured = {}
        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_dynamic_libs = lambda *args, **kwargs: [("pdfium-native", "pypdfium2_raw")]
        hooks.collect_data_files = lambda name, **kwargs: [(name + "/version.json", name)]

        def analysis(*args, **kwargs):
            captured.update(kwargs)
            raise Captured

        with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}), patch.dict(
                "os.environ", {"GRAVELLE_BUILD_RESOURCES": str(self.root), "GRAVELLE_MACOS_ARCH": "arm64"}):
            with self.assertRaises(Captured):
                runpy.run_path(str(ROOT / "desktop" / filename), init_globals={
                    "SPECPATH": str(ROOT / "desktop"), "Analysis": analysis,
                })
        return captured

    def test_windows_and_macos_specs_keep_model_directory_and_lazy_worker_module(self):
        for filename in ("gravelle.spec", "gravelle-macos.spec"):
            with self.subTest(spec=filename):
                config = self._freeze_configuration(filename)
                self.assertIn("avatar_face_crop", config["hiddenimports"])
                entries = [(Path(source), destination) for source, destination in config["datas"]
                           if destination == "models/yunet"]
                self.assertEqual(entries, [(ROOT / "models" / "yunet", "models/yunet")])
                source, destination = entries[0]
                bundle_root = self.root / filename
                shutil.copytree(source, bundle_root / destination)
                # PyInstaller resolves modules relative to its data root, rather
                # than the launch directory or the administrator's data folder.
                with patch.object(avatar_face_crop, "__file__", str(bundle_root / "avatar_face_crop.py")), \
                        patch("socket.create_connection", side_effect=AssertionError("offline resource lookup")), \
                        patch("urllib.request.urlopen", side_effect=AssertionError("offline resource lookup")):
                    self.assertEqual(avatar_face_crop._verified_model(),
                                     (bundle_root / "models" / "yunet" / avatar_face_crop.MODEL_NAME).read_bytes())

    def test_offline_source_lookup_ignores_working_directory_and_rejects_altered_model(self):
        with patch("pathlib.Path.cwd", return_value=self.root), \
                patch("urllib.request.urlopen", side_effect=AssertionError("no runtime downloads")):
            self.assertEqual(avatar_face_crop._verified_model(),
                             (ROOT / "models" / "yunet" / avatar_face_crop.MODEL_NAME).read_bytes())
        directory = self.root / "models" / "yunet"
        shutil.copytree(ROOT / "models" / "yunet", directory)
        model_path = directory / avatar_face_crop.MODEL_NAME
        data = bytearray(model_path.read_bytes())
        data[-1] ^= 1
        model_path.write_bytes(data)
        metadata = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
        metadata["sha256"] = hashlib.sha256(data).hexdigest()
        (directory / "provenance.json").write_text(json.dumps(metadata), encoding="utf-8")
        with patch.object(avatar_face_crop, "__file__", str(self.root / "avatar_face_crop.py")):
            with self.assertRaises(ValueError):
                avatar_face_crop._verified_model()

    def test_desktop_notices_describe_bundled_avatar_only_detector(self):
        windows = (ROOT / "desktop" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        macos = (ROOT / "desktop" / "build_macos.py").read_text(encoding="utf-8")
        for notice in (windows, macos):
            self.assertIn("YuNet face detector (MIT)", notice)
            self.assertIn("copyright (c) 2020 Shiqi Yu", notice)
            self.assertIn("models/yunet/", notice)
            self.assertIn("avatar cropping", notice)
            self.assertIn("never downloaded at runtime", notice)
        self.assertNotIn("No GPU runtime or downloaded model is included", macos)


if __name__ == "__main__":
    unittest.main()

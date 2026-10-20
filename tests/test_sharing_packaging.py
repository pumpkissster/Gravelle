"""Pinned offline share crypto, notices and distribution boundaries."""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop import share_packaging

ROOT = Path(__file__).resolve().parents[1]


class SharePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fake_distributions(self):
        result = {}
        for name, names in (("cryptography", ("LICENSE", "LICENSE.APACHE", "LICENSE.BSD", "component/NOTICE.thirdparty")),
                            ("cffi", ("LICENSE",)), ("pycparser", ("LICENSE",))):
            base = self.root / name
            files = []
            for filename in names:
                relative = Path(name + "-fake.dist-info/licenses") / filename
                source = base / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes((filename + "\nComplete generated license fixture.\n").encode() * 10)
                files.append(relative)
            files += [Path("../outside.txt"), Path(name + "/__init__.py")]
            result[name] = SimpleNamespace(version=share_packaging.crypto_version() if name == "cryptography" else "fixture",
                                           files=files, locate_file=lambda entry, prefix=base: prefix / entry)
        return result

    def test_complete_crypto_notices_keep_both_license_alternatives_and_all_components(self):
        distributions = self.fake_distributions()
        with patch.object(share_packaging.importlib.metadata, "distribution", side_effect=distributions.__getitem__):
            copied = share_packaging.collect_share_licenses(self.root / "output")
        self.assertEqual(len(copied), 7)
        self.assertTrue({"LICENSE.APACHE", "LICENSE.BSD", "NOTICE.thirdparty"} <= {path.name for path in copied})
        for path in copied:
            if path.name.startswith("OpenSSL-"):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), share_packaging.OPENSSL_LICENSE_SHA256)
            else:
                package = path.relative_to(self.root / "output").parts[0]
                original = distributions[package].locate_file(path.relative_to(self.root / "output" / package))
                self.assertEqual(path.read_bytes(), original.read_bytes())

    def test_incomplete_notice_or_unreviewed_wheel_stops_packaging(self):
        for missing in ("LICENSE.APACHE", "LICENSE.BSD"):
            distributions = self.fake_distributions()
            distributions["cryptography"].files = [item for item in distributions["cryptography"].files if item.name != missing]
            with self.subTest(missing=missing), patch.object(share_packaging.importlib.metadata, "distribution", side_effect=distributions.__getitem__):
                with self.assertRaisesRegex(RuntimeError, "license notices are missing"):
                    share_packaging.collect_share_licenses(self.root / "output")
        distributions = self.fake_distributions()
        distributions["cryptography"].version = "unreviewed"
        with patch.object(share_packaging.importlib.metadata, "distribution", side_effect=distributions.__getitem__):
            with self.assertRaisesRegex(RuntimeError, "pinned version"):
                share_packaging.collect_share_licenses(self.root / "output")

    def test_installed_dependency_and_full_notices_are_available(self):
        self.assertEqual(importlib.metadata.version("cryptography"), share_packaging.crypto_version())
        copied = share_packaging.collect_share_licenses(self.root / "notices")
        self.assertTrue({"LICENSE.APACHE", "LICENSE.BSD"} <= {path.name for path in copied})
        from share_crypto import encrypt, generate_key
        self.assertTrue(encrypt(b"license-test", "image/png", generate_key()).startswith(b"GSH1"))

    def test_intel_mac_uses_last_universal2_wheel_without_downgrading_other_systems(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn('cryptography==50.0.2; sys_platform != "darwin" or platform_machine != "x86_64"', requirements)
        self.assertIn('cryptography==48.0.1; sys_platform == "darwin" and platform_machine == "x86_64"', requirements)
        for system, machine, expected in (("darwin", "x86_64", "48.0.1"), ("darwin", "arm64", "50.0.2"),
                                          ("win32", "AMD64", "50.0.2"), ("linux", "x86_64", "50.0.2"), ("linux", "aarch64", "50.0.2")):
            with self.subTest(system=system, machine=machine), patch.object(share_packaging.sys, "platform", system), patch.object(share_packaging.platform, "machine", return_value=machine):
                self.assertEqual(share_packaging.crypto_version(), expected)

    def test_browser_bundle_and_unmodified_upstream_sources_have_reviewed_hashes(self):
        builder = runpy.run_path(str(ROOT / "packaging/build_share_crypto.py"))
        provenance = builder["verify_inputs"]()
        self.assertEqual(provenance["version"], "2.4.0")
        self.assertEqual(provenance["build_tool"]["version"], "0.28.2")
        bundle = (ROOT / "static/vendor/noble-gcm.js").read_bytes()
        self.assertEqual(hashlib.sha256(bundle).hexdigest(), provenance["bundle_sha256"])
        self.assertEqual((ROOT / "static/vendor/noble-ciphers-LICENSE.txt").read_bytes(),
                         (ROOT / "packaging/vendor/noble-ciphers-2.4.0/LICENSE").read_bytes())
        self.assertLess(len(bundle), 20000)
        self.assertNotIn(b"https://cdn", bundle)
        page = (ROOT / "static/share.html").read_text(encoding="utf-8")
        self.assertLess(page.index("/vendor/noble-gcm.js"), page.index("/share-crypto.js"))
        self.assertLess(page.index("/share-crypto.js"), page.index("/share-reader.js"))

    def test_source_export_and_docker_context_include_share_resources_without_private_data(self):
        exporter = runpy.run_path(str(ROOT / "packaging/package_source.py"))
        source = set(exporter["source_files"]())
        required = {"sharing.py", "share_crypto.py", "share_passwords.py", "static/sharing-ui.js", "static/sharing-ui.css", "static/share.html",
                    "static/share-reader.js", "static/share-reader.css", "static/share-crypto.js", "static/vendor/noble-gcm.js",
                    "static/vendor/noble-ciphers-LICENSE.txt", "docs/sharing.md", "desktop/share_packaging.py",
                    "tests/test_sharing.py", "tests/test_sharing_api.py", "tests/test_sharing_ui.js", "tests/test_share_crypto.py",
                    "tests/test_share_crypto_ui.js", "tests/test_sharing_packaging.py", "tests/share_crypto_vectors.json"}
        self.assertFalse(required - source, required - source)
        self.assertFalse({"docs/next-version.md", "VERIFICATION.md"} & source)
        checker = runpy.run_path(str(ROOT / "packaging/check_docker_context.py"))
        context = checker["validate"](ROOT)
        self.assertEqual(context["status"], "passed")
        self.assertTrue({"sharing.py", "share_crypto.py", "share_passwords.py", "static/share.html", "static/vendor/noble-gcm.js",
                         "desktop/licenses/OpenSSL-4.0.2-LICENSE.txt"} <= set(context["files"]))
        self.assertFalse(any(name.startswith("packaging/vendor/") for name in context["files"]))

    def test_desktop_specs_collect_native_crypto_and_notices(self):
        for filename in ("gravelle.spec", "gravelle-macos.spec"):
            tree = ast.parse((ROOT / "desktop" / filename).read_text(encoding="utf-8"))
            analysis = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == "Analysis")
            options = {keyword.arg: keyword.value for keyword in analysis.keywords}
            self.assertTrue({"sharing", "share_crypto", "share_passwords", "cryptography.hazmat.primitives.ciphers.aead"}
                            <= set(ast.literal_eval(options["hiddenimports"])))
        for filename in ("build_windows.ps1", "build_macos.py"):
            source = (ROOT / "desktop" / filename).read_text(encoding="utf-8")
            self.assertIn("from desktop.share_packaging import collect_share_licenses", source)
            self.assertIn("collect_share_licenses(target)", source)
        self.assertIn("from cryptography.hazmat.primitives.ciphers.aead import AESGCM", (ROOT / "start_windows.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

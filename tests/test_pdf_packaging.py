"""Portable PDF distribution contracts; no Mac binaries are fabricated here."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from desktop import build_macos, macos_entry, pdf_packaging

ROOT = Path(__file__).resolve().parents[1]


class PDFPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fake_distribution(self, *, missing=(), version='5.14.0'):
        entries = []
        for name in sorted(pdf_packaging.REQUIRED_NOTICES | {'future-component.license'}):
            if name in missing:
                continue
            part = 'LICENSES' if name in {'Apache-2.0.txt', 'BSD-3-Clause.txt', 'CC-BY-4.0.txt'} else 'data/platform/BUILD_LICENSES'
            relative = Path('pypdfium2-5.14.0.dist-info/licenses') / part / name
            source = self.root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes((name + '\nFull generated license text.\n').encode() * 10)
            entries.append(relative)
        # Non-license files and traversal entries must not be copied.
        entries += [Path('pypdfium2/__init__.py'), Path('../outside.txt')]
        return types.SimpleNamespace(version=version, files=entries, locate_file=lambda value: self.root / value)

    def test_all_pdfium_component_notices_preserve_contents_and_extensions(self):
        dist = self.fake_distribution()
        with patch.object(pdf_packaging.importlib.metadata, 'distribution', return_value=dist):
            copied = pdf_packaging.collect_pdfium_licenses(self.root / 'output')
        self.assertEqual({path.name for path in copied}, pdf_packaging.REQUIRED_NOTICES | {'future-component.license'})
        self.assertIn('libjpeg_turbo.ijg', {path.name for path in copied})
        for destination in copied:
            source = self.root / destination.relative_to(self.root / 'output' / 'pypdfium2')
            self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertFalse((self.root / 'output' / 'outside.txt').exists())

    def test_rejects_missing_pdfium_or_component_notice_before_copying(self):
        for missing in [('pdfium.txt',), ('libjpeg_turbo.ijg',), ('abseil.txt',)]:
            dist = self.fake_distribution(missing=missing)
            output = self.root / ('output-' + missing[0])
            with self.subTest(missing=missing), patch.object(pdf_packaging.importlib.metadata, 'distribution', return_value=dist):
                with self.assertRaisesRegex(RuntimeError, 'missing'):
                    pdf_packaging.collect_pdfium_licenses(output)
            self.assertFalse(output.exists())

    def test_rejects_license_abbreviation_and_unreviewed_dependency_version(self):
        dist = self.fake_distribution()
        source = self.root / next(path for path in dist.files if path.name == 'pdfium.txt')
        source.write_text('BSD', encoding='utf-8')
        with patch.object(pdf_packaging.importlib.metadata, 'distribution', return_value=dist):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                pdf_packaging.collect_pdfium_licenses(self.root / 'output')
        dist.version = '99.0.0'
        with patch.object(pdf_packaging.importlib.metadata, 'distribution', return_value=dist):
            with self.assertRaisesRegex(RuntimeError, 'reviewed again'):
                pdf_packaging.collect_pdfium_licenses(self.root / 'output')

    def test_installed_wheel_has_complete_licenses_and_native_renderer(self):
        try:
            dist = importlib.metadata.distribution('pypdfium2')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('Install requirements.txt to inspect the real pinned wheel')
        with patch.object(pdf_packaging.importlib.metadata, 'distribution', return_value=dist):
            copied = pdf_packaging.collect_pdfium_licenses(self.root / 'notices')
        self.assertTrue(pdf_packaging.REQUIRED_NOTICES <= {path.name for path in copied})
        import pypdfium2 as pdfium
        # Calls the actual native library, not only import metadata.
        document = pdfium.PdfDocument.new()
        try:
            page = document.new_page(120, 180)
            try:
                self.assertEqual(page.get_size(), (120.0, 180.0))
            finally:
                page.close()
        finally:
            document.close()

    def test_mac_pdf_worker_restores_stdio_before_engine_without_importing_gui(self):
        events = []
        module = types.ModuleType('pdf_reader')
        module.worker_main = lambda arguments: events.append(('worker', arguments)) or 17
        with patch.dict(sys.modules, {'pdf_reader': module}), patch.object(macos_entry, 'restore_private_stdio', side_effect=lambda: events.append('stdio')):
            self.assertEqual(macos_entry.main(['--pdf-worker', '--probe']), 17)
        self.assertEqual(events, ['stdio', ('worker', ['--probe'])])

    def test_windows_private_entry_dispatches_pdf_before_runtime_and_instance_lock(self):
        module = types.ModuleType('pdf_reader')
        calls = []
        module.worker_main = lambda arguments: calls.append(arguments) or 19
        real_import = __import__
        def import_without_launcher_runtime(name, *args, **kwargs):
            if name in {'desktop.core', 'desktop.runtime', 'tkinter'}:
                raise AssertionError('PDF worker imported GUI/runtime before dispatch')
            return real_import(name, *args, **kwargs)
        with patch.dict(sys.modules, {'pdf_reader': module}), patch.object(sys, 'argv', ['launcher.py', '--pdf-worker', '--probe']), patch('builtins.__import__', side_effect=import_without_launcher_runtime):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / 'desktop' / 'launcher.py'), run_name='__main__')
        self.assertEqual(stopped.exception.code, 19)
        self.assertEqual(calls, [['--probe']])

    def test_both_freeze_specs_fail_when_native_pdfium_is_missing(self):
        hooks = types.ModuleType('PyInstaller.utils.hooks')
        hooks.collect_data_files = lambda *args, **kwargs: []
        hooks.collect_dynamic_libs = lambda *args, **kwargs: []
        for name in ('gravelle.spec', 'gravelle-macos.spec'):
            with self.subTest(spec=name), patch.dict(sys.modules, {'PyInstaller.utils.hooks': hooks}):
                with self.assertRaisesRegex(RuntimeError, 'PDFium shared library is missing'):
                    runpy.run_path(str(ROOT / 'desktop' / name))

    def test_both_freeze_specs_fail_when_pdfium_version_resources_are_missing(self):
        hooks = types.ModuleType('PyInstaller.utils.hooks')
        hooks.collect_data_files = lambda *args, **kwargs: []
        hooks.collect_dynamic_libs = lambda *args, **kwargs: [('pdfium.dll', 'pypdfium2_raw')]
        for name in ('gravelle.spec', 'gravelle-macos.spec'):
            with self.subTest(spec=name), patch.dict(sys.modules, {'PyInstaller.utils.hooks': hooks}):
                with self.assertRaisesRegex(RuntimeError, 'PDFium version resources are missing'):
                    runpy.run_path(str(ROOT / 'desktop' / name))

    def test_missing_pdf_notices_block_native_build_and_windows_uses_the_same_collector(self):
        prefix = self.root / 'python'
        prefix.mkdir()
        (prefix / 'LICENSE.txt').write_text('Generated Python license fixture', encoding='utf-8')
        with patch.object(build_macos, 'LICENSE_PACKAGES', ()), patch.object(build_macos.sys, 'base_prefix', str(prefix)), patch('desktop.pdf_packaging.collect_pdfium_licenses', side_effect=RuntimeError('PDFium license notices are missing: pdfium.txt')):
            with self.assertRaisesRegex(build_macos.BuildError, 'pdfium.txt'):
                build_macos.collect_licenses(self.root / 'output')
        windows = (ROOT / 'desktop' / 'build_windows.ps1').read_text(encoding='utf-8')
        self.assertIn('from desktop.pdf_packaging import collect_pdfium_licenses', windows)
        self.assertIn('collect_pdfium_licenses(target)', windows)

    def test_pdf_distribution_inputs_are_allowlisted_and_not_private(self):
        manifest = json.loads((ROOT / 'packaging' / 'source_manifest.json').read_text(encoding='utf-8'))
        for name in ('pdf_reader.py', 'desktop/pdf_packaging.py', 'tests/test_pdf_reader.py',
                     'tests/test_pdf_api.py', 'tests/test_pdf_packaging.py', 'docs/pdf.md'):
            self.assertIn(name, manifest)
        self.assertNotIn('docs/next-version.md', manifest)
        self.assertIn('pypdfium2==5.14.0', (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines())
        self.assertIn('!pdf_reader.py', (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines())
        self.assertIn(' pdf_reader.py ./', (ROOT / 'Dockerfile').read_text(encoding='utf-8'))
        self.assertIn('pypdfium2.PdfDocument.new().close()', (ROOT / 'Dockerfile').read_text(encoding='utf-8'))
        for name in ('gravelle.spec', 'gravelle-macos.spec'):
            spec = (ROOT / 'desktop' / name).read_text(encoding='utf-8')
            for dependency in ("'pdf_reader'", "'pypdfium2'", "'pypdfium2_raw'", "'pypdfium2_cfg'"):
                self.assertIn(dependency, spec)
            self.assertIn("collect_dynamic_libs('pypdfium2_raw')", spec)
            self.assertIn("collect_data_files('pypdfium2_raw', includes=['version.json'])", spec)

    def test_docker_fonts_include_cjk_fallbacks_and_keep_complete_package_notices(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('apt-get install -y --no-install-recommends fontconfig fonts-noto-cjk', dockerfile)
        self.assertIn('fc-cache -f', dockerfile)
        self.assertIn('test -s /usr/share/doc/fonts-noto-cjk/copyright', dockerfile)
        self.assertIn('test -s /usr/share/doc/fontconfig/copyright', dockerfile)
        self.assertIn("('zh-cn','zh-tw','ja','ko')", dockerfile)
        self.assertIn("['fc-match','-f','%{family}'", dockerfile)
        self.assertLess(dockerfile.index('fonts-noto-cjk'), dockerfile.index('USER 1000:1000'))
        self.assertNotIn('rm -rf /usr/share/doc', dockerfile)


if __name__ == '__main__':
    unittest.main()

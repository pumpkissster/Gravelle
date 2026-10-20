"""Portable packaging contract tests; they do not fabricate native artifacts."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from desktop import build_macos


class MacOSPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'server.py').write_text('VERSION = "0.8.6"\n', encoding='utf-8')

    def test_configures_arch_specific_trusted_assets_without_changing_source(self):
        original = (self.root / 'server.py').read_bytes()
        for arch, native in [('arm64', 'arm64'), ('x64', 'x86_64')]:
            config = build_macos.configuration(self.root, 'pumpkissster/Gravelle', arch, 'dist/macos')
            self.assertEqual(config['native_architecture'], native)
            self.assertEqual(config['minimum_macos'], '15.0')
            self.assertEqual(config['release_config']['asset_name'], f'Gravelle-macOS-{{version}}-{arch}.dmg')
            self.assertEqual(config['release_config']['platform'], 'macos')
            self.assertEqual(config['zip'], f'Gravelle-macOS-0.8.6-{arch}.zip')
        self.assertEqual((self.root / 'server.py').read_bytes(), original)
        self.assertFalse((self.root / 'dist').exists())

    def test_rejects_untrusted_repository_architecture_and_outside_output(self):
        for repo, arch, output in [('https://github.com/a/b', 'arm64', 'dist/mac'),
                                   ('owner/repo\nanything', 'arm64', 'dist/mac'),
                                   ('owner/repo', 'universal2', 'dist/mac'),
                                   ('owner/repo', 'arm64', '../outside'),
                                   ('owner/repo', 'arm64', '.')]:
            with self.subTest(repo=repo, arch=arch, output=output):
                with self.assertRaises(build_macos.BuildError):
                    build_macos.configuration(self.root, repo, arch, output)

    def test_never_builds_macos_artifacts_on_another_os(self):
        config = build_macos.configuration(self.root, 'owner/repo', 'arm64', 'dist/mac')
        with patch.object(build_macos.sys, 'platform', 'win32'), patch.object(build_macos, 'run') as run:
            with self.assertRaisesRegex(build_macos.BuildError, 'native Mac'):
                build_macos.build(config)
            run.assert_not_called()
        self.assertFalse((self.root / 'dist').exists())

    def test_rejects_wrong_interpreter_architecture_and_old_os(self):
        with patch.object(build_macos.sys, 'platform', 'darwin'), patch.object(build_macos.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(build_macos.BuildError, 'matching'):
                build_macos.require_native('arm64')
            with patch.object(build_macos.platform, 'mac_ver', return_value=('14.7', (), '')):
                with self.assertRaisesRegex(build_macos.BuildError, '15'):
                    build_macos.require_native('x64')

    def test_stable_version_is_read_with_ast_without_executing_server(self):
        (self.root / 'server.py').write_text('raise RuntimeError("must not execute")\nVERSION = "0.8.6"\n', encoding='utf-8')
        self.assertEqual(build_macos.read_version(self.root), '0.8.6')
        for source in ['VERSION = "0.8.6-beta"', 'VERSION = 1', 'VERSION = "0.8.6"\nVERSION = "0.8.7"']:
            (self.root / 'server.py').write_text(source, encoding='utf-8')
            with self.assertRaises(build_macos.BuildError):
                build_macos.read_version(self.root)

    def test_checksum_contains_exact_named_artifacts_and_bytes(self):
        first = self.root / 'example.zip'
        second = self.root / 'example.dmg'
        first.write_bytes(b'zip fixture')
        second.write_bytes(b'dmg fixture')
        target = self.root / 'SHA256SUMS.txt'
        build_macos.write_checksums([first, second], target)
        self.assertEqual(target.read_text(encoding='ascii'),
                         f'{hashlib.sha256(second.read_bytes()).hexdigest()}  example.dmg\n'
                         f'{hashlib.sha256(first.read_bytes()).hexdigest()}  example.zip\n')

    def test_notarization_requires_explicit_accepted_result(self):
        with patch.object(build_macos, 'run', return_value='{"status":"Invalid"}') as run:
            with self.assertRaisesRegex(build_macos.BuildError, 'not accepted'):
                build_macos.notarize(Path('example.dmg'), 'profile', '/tmp/keychain')
            command = run.call_args.args[0]
            self.assertIn('--keychain-profile', command)
            self.assertNotIn('--password', command)
            self.assertEqual(command[-2:], ['--keychain', '/tmp/keychain'])
        with patch.object(build_macos, 'run', return_value='{"status":"Accepted"}'):
            build_macos.notarize(Path('example.dmg'), 'profile')

    def test_app_zip_uses_ditto_to_preserve_bundle_symlinks_and_modes(self):
        with patch.object(build_macos, 'run') as run:
            build_macos.archive_app(Path('Gravelle.app'), Path('Gravelle.zip'))
            command = run.call_args.args[0]
            self.assertEqual(command[0], '/usr/bin/ditto')
            self.assertIn('--keepParent', command)
            self.assertIn('--sequesterRsrc', command)

    def test_portable_validation_does_not_import_native_gui_or_create_output(self):
        with patch.object(build_macos, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_macos.main(['--validate-config', '--arch', 'arm64']), 0)
        self.assertIn('Gravelle-macOS-0.8.6-arm64.dmg', output.getvalue())
        self.assertFalse((self.root / 'dist').exists())

    def test_spec_preserves_service_workers_and_real_sources(self):
        spec = (build_macos.ROOT / 'desktop' / 'gravelle-macos.spec').read_text(encoding='utf-8')
        ast.parse(spec)
        for name in ('macos_entry.py', 'desktop.platforms', 'transcoding', 'image_search',
                     'library_covers', 'cache_manager', 'password_reset', 'AppKit', 'Foundation'):
            self.assertIn(name, spec)
        self.assertIn('target_arch=architecture', spec)
        self.assertIn("excludes=['resvg_py', 'tkinter']", spec)
        self.assertNotIn('GravelleServer.exe', spec)
        self.assertIn('entitlements_file=', spec)
        self.assertEqual(build_macos.ENTITLEMENTS, {'com.apple.security.cs.allow-jit': True})

    @unittest.skipUnless(importlib.util.find_spec('resvg_py'), 'Build-only resvg-py is not installed')
    def test_real_mac_icon_has_white_rounded_tile_and_preserves_supplied_logo(self):
        from PIL import Image
        source = build_macos.ROOT / 'static' / 'gravelle-icon.png'
        original = source.read_bytes()
        with Image.open(io.BytesIO(original)) as logo:
            colours = logo.convert('RGBA').getcolors(logo.width * logo.height)
            brand_colour = max((count, colour) for count, colour in colours if colour[3] == 255)[1]
        rendered = build_macos.macos_icon_png(source)
        with Image.open(io.BytesIO(rendered)) as icon:
            self.assertEqual(icon.mode, 'RGBA')
            self.assertEqual(icon.size, (1024, 1024))
            # A white tile remains visible in light and dark Docks; its
            # rounded corners and outer margins remain genuinely transparent.
            self.assertEqual(icon.getpixel((0, 0))[3], 0)
            self.assertEqual(icon.getpixel((1023, 1023))[3], 0)
            self.assertEqual(icon.getpixel((110, 110))[3], 0)
            for point in ((150, 512), (874, 512), (512, 150), (512, 874)):
                self.assertEqual(icon.getpixel(point), (255, 255, 255, 255))
            colours = {colour: count for count, colour in icon.getcolors(icon.width * icon.height)}
            self.assertGreater(colours[brand_colour], 10000, 'The original pink mark must remain visible and unchanged in colour')
            self.assertGreater(colours[(255, 255, 255, 255)], 400000)
            # Do not enlarge or clip the mark against the rounded tile.
            pixels = icon.load()
            pink_points = [(x, y) for y in range(1024) for x in range(1024)
                           if pixels[x, y] == brand_colour]
            self.assertGreater(min(x for x, y in pink_points), 200)
            self.assertLess(max(x for x, y in pink_points), 824)
            self.assertGreater(min(y for x, y in pink_points), 200)
            self.assertLess(max(y for x, y in pink_points), 824)
        self.assertEqual(source.read_bytes(), original, 'Mac packaging must never overwrite the web/Windows logo')

    @unittest.skipUnless(importlib.util.find_spec('resvg_py'), 'Build-only resvg-py is not installed')
    def test_resources_encode_white_mac_icon_at_all_icns_sizes_without_changing_wordmark(self):
        from PIL import Image
        static = self.root / 'static'
        static.mkdir()
        for name in ('gravelle-icon.png', 'gravelle-wordmark.svg'):
            shutil.copyfile(build_macos.ROOT / 'static' / name, static / name)
        originals = {path.name: path.read_bytes() for path in static.iterdir()}
        resources = self.root / 'resources'
        config = build_macos.configuration(self.root, 'owner/repo', 'arm64', 'dist/mac')
        with patch.object(build_macos, 'collect_licenses'):
            build_macos.prepare_resources(self.root, resources, config)
        with Image.open(resources / 'Gravelle.icns') as icon:
            self.assertEqual(icon.format, 'ICNS')
            self.assertEqual(icon.size, (1024, 1024))
            self.assertEqual(icon.convert('RGBA').getpixel((150, 512)), (255, 255, 255, 255))
            # Pillow writes a native 16pt @2x slot plus PNG sizes 32..1024.
            self.assertIn((16, 16, 2), icon.info['sizes'])
            for width in (32, 64, 128, 256, 512, 1024):
                self.assertTrue(any(size[0] * size[2] == width for size in icon.info['sizes']))
        with Image.open(resources / 'desktop' / 'assets' / 'gravelle-wordmark.png') as wordmark:
            self.assertEqual(wordmark.width, 600)
            self.assertEqual(wordmark.getextrema()[3], (0, 255))
        self.assertEqual({path.name: path.read_bytes() for path in static.iterdir()}, originals)

    def simulated_native_build(self, *, signing=False, notary_status='Accepted', binary_arch='arm64'):
        """Exercise artifact ordering using explicit generated fixtures, not Mach-O files."""
        (self.root / 'LICENSE').write_text('generated license fixture', encoding='utf-8')
        config = build_macos.configuration(self.root, 'owner/repo', 'arm64', 'dist/mac')
        commands = []
        original_is_file = Path.is_file
        def is_file(path):
            return str(path).replace('\\', '/').startswith('/usr/bin/') or original_is_file(path)
        def prepare(root, resources, config):
            resources.mkdir(parents=True)
        def run(command, **kwargs):
            commands.append(command)
            if '-m' in command and 'PyInstaller' in command:
                dist = Path(command[command.index('--distpath') + 1])
                binary = dist / 'Gravelle.app' / 'Contents' / 'MacOS' / 'Gravelle'
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b'explicit test fixture: not a native executable')
            elif command[0] == '/usr/bin/lipo':
                return binary_arch + '\n'
            elif command[0] == '/usr/bin/ditto':
                if '-c' in command:
                    Path(command[-1]).write_bytes(b'archive fixture')
                else:
                    shutil.copytree(command[-2], command[-1])
            elif command[0] == '/usr/bin/hdiutil':
                Path(command[-1]).write_bytes(b'disk image fixture')
            elif command[:3] == ['xcrun', 'notarytool', 'submit']:
                return json.dumps({'status': notary_status})
            return ''
        with patch.object(build_macos, 'ROOT', self.root), \
                patch.object(build_macos, 'require_native'), \
                patch.object(build_macos, 'prepare_resources', side_effect=prepare), \
                patch.object(Path, 'is_file', is_file), \
                patch.object(Path, 'symlink_to') as symlink, \
                patch.object(build_macos, 'run', side_effect=run):
            report = build_macos.build(config,
                identity='Developer ID Application: Example (1234567890)' if signing else '',
                notary_profile='test-profile' if signing else '')
            symlink.assert_called_once_with('/Applications', target_is_directory=True)
        return report, commands

    def test_signed_archives_are_created_after_stapling_the_app(self):
        report, commands = self.simulated_native_build(signing=True)
        app = Path(report['app'])
        app_staple = next(i for i, cmd in enumerate(commands) if cmd[:3] == ['xcrun', 'stapler', 'staple'] and cmd[-1] == str(app))
        final_zip = next(i for i, cmd in enumerate(commands) if cmd[0] == '/usr/bin/ditto' and cmd[-1].endswith('Gravelle-macOS-0.8.6-arm64.zip'))
        dmg_submission = next(i for i, cmd in enumerate(commands) if cmd[:3] == ['xcrun', 'notarytool', 'submit'] and cmd[3].endswith('.dmg'))
        self.assertLess(app_staple, final_zip)
        self.assertLess(final_zip, dmg_submission)
        self.assertEqual(report['signing'], 'developer_id')
        self.assertTrue(report['notarized'])
        self.assertTrue((self.root / 'dist/mac/SHA256SUMS.txt').is_file())
        # Signature verification runs before notarization and artifacts have
        # deterministic, architecture-specific names in the resulting report.
        self.assertIn(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], commands)
        self.assertEqual(len(report['files']), 2)

    def test_unsigned_build_is_explicitly_ad_hoc_and_arch_failure_blocks_artifacts(self):
        report, commands = self.simulated_native_build()
        self.assertEqual(report['signing'], 'ad_hoc')
        self.assertFalse(report['notarized'])
        self.assertFalse(any('notarytool' in cmd for cmd in commands))
        # A wrong native architecture must fail before packaging, rather than
        # merely labeling an Intel binary as Apple Silicon.
        with self.assertRaisesRegex(build_macos.BuildError, 'architecture'):
            self.simulated_native_build(binary_arch='x86_64')

    def test_validation_workflow_has_no_release_or_repository_write_permissions(self):
        workflow = (build_macos.ROOT / '.github' / 'workflows' / 'macos-build.yml').read_text(encoding='utf-8')
        for value in ('workflow_dispatch:', 'macos-15-intel', 'runner: macos-15', 'contents: read',
                      'smoke_macos.py', 'shasum -a 256 -c', 'actions/upload-artifact@v7'):
            self.assertIn(value, workflow)
        for forbidden in ('contents: write', 'gh release ', 'docker push ', 'push: true', 'on: push', 'pull_request:'):
            self.assertNotIn(forbidden, workflow)
        self.assertIn('certificate.unlink(missing_ok=True)', workflow)
        self.assertIn('delete-keychain', workflow)

    def test_automatic_builds_are_restricted_to_the_macos_preview_branch(self):
        workflow = (build_macos.ROOT / '.github' / 'workflows' / 'macos-build.yml').read_text(encoding='utf-8')
        triggers = workflow.split('on:', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('  push:\n    branches:\n      - dev/macos-preview\n', triggers)
        self.assertEqual(triggers.count('      - '), 1)
        self.assertNotIn('pull_request', triggers)
        self.assertIn('  workflow_dispatch:', triggers)

    def test_headless_gui_is_reported_as_unsupported_without_masking_failures(self):
        workflow = (build_macos.ROOT / '.github' / 'workflows' / 'macos-build.yml').read_text(encoding='utf-8')
        gui = workflow.split('      - name: Smoke test native launcher window and menu bar', 1)[1].split('      - name: Preserve development artifacts', 1)[0]
        self.assertIn('"$gui_status" -eq 77', gui)
        self.assertIn("report.get('status') == 'unsupported'", gui)
        self.assertIn('::warning::', gui)
        self.assertIn('NOT passed native validation', gui)
        self.assertIn('elif [ "$gui_status" -ne 0 ]; then\n            exit "$gui_status"', gui)
        self.assertIn('else\n            printf', gui)

    def collect_core_wheel_fixture(self, *, version='12.2.2', notice='original'):
        """Use real importlib metadata with the official wheel's missing-file layout."""
        dist_info = self.root / f'pyobjc_core-{version}.dist-info'
        dist_info.mkdir(exist_ok=True)
        (dist_info / 'METADATA').write_text(
            f'Metadata-Version: 2.1\nName: pyobjc-core\nVersion: {version}\nLicense: MIT\n\n', encoding='utf-8')
        (dist_info / 'RECORD').write_text(
            f'{dist_info.name}/METADATA,,\n{dist_info.name}/RECORD,,\n', encoding='utf-8')
        distribution = importlib.metadata.PathDistribution(dist_info)
        vendor = self.root / 'desktop/licenses/pyobjc-core-12.2.2-LICENSE.txt'
        vendor.parent.mkdir(parents=True, exist_ok=True)
        if notice == 'original':
            content = (build_macos.ROOT / 'desktop/licenses/pyobjc-core-12.2.2-LICENSE.txt').read_bytes()
            vendor.write_bytes(content)
        elif notice is not None:
            vendor.write_bytes(notice)
        else:
            vendor.unlink(missing_ok=True)
        python_prefix = self.root / 'python'
        python_prefix.mkdir(exist_ok=True)
        (python_prefix / 'LICENSE.txt').write_text('generated Python license fixture', encoding='utf-8')
        target = self.root / 'collected-licenses'
        with patch.object(build_macos, 'ROOT', self.root), \
                patch.object(build_macos, 'LICENSE_PACKAGES', ('pyobjc-core',)), \
                patch.object(build_macos.sys, 'base_prefix', str(python_prefix)), \
                patch.object(build_macos.importlib.metadata, 'distribution', return_value=distribution), \
                patch('desktop.pdf_packaging.collect_pdfium_licenses') as pdf_notices, \
                patch('desktop.share_packaging.collect_share_licenses') as share_notices:
            build_macos.collect_licenses(target)
            pdf_notices.assert_called_once_with(target)
            share_notices.assert_called_once_with(target)
        return target

    def test_core_wheel_missing_license_uses_complete_verified_source_notice(self):
        target = self.collect_core_wheel_fixture()
        content = (target / 'pyobjc-core/License.txt').read_bytes()
        self.assertEqual(len(content), 1249)
        self.assertEqual(hashlib.sha256(content).hexdigest(), build_macos.PYOBJC_CORE_LICENSE_SHA256)
        self.assertIn(b'Copyright 2003-2025 - Ronald Oussoren', content)
        self.assertIn(b'Permission is hereby granted', content)
        self.assertNotEqual(content.strip(), b'MIT')

    def test_changed_core_version_does_not_reuse_an_older_notice(self):
        with self.assertRaisesRegex(build_macos.BuildError, 'No verified license fallback'):
            self.collect_core_wheel_fixture(version='12.2.3')
        self.assertFalse((self.root / 'collected-licenses/pyobjc-core/License.txt').exists())

    def test_corrupted_or_missing_core_notice_still_blocks_the_build(self):
        for content, error in ((b'MIT', 'SHA-256'), (None, 'missing or unreadable')):
            with self.subTest(content=content):
                with self.assertRaisesRegex(build_macos.BuildError, error):
                    self.collect_core_wheel_fixture(notice=content)
        self.assertFalse((self.root / 'collected-licenses/pyobjc-core/License.txt').exists())


if __name__ == '__main__':
    unittest.main()

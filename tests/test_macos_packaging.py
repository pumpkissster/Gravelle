"""Portable packaging contract tests; they do not fabricate native artifacts."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
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


if __name__ == '__main__':
    unittest.main()

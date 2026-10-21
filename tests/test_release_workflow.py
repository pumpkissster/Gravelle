"""Exercise release workflow gates and artifact selection without publishing."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RELEASE = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
MACOS = (ROOT / '.github/workflows/macos-build.yml').read_text(encoding='utf-8')


def job(source, name):
    body = source.split('\n  ' + name + ':\n', 1)[1]
    return re.split(r'\n  [a-z][a-z0-9-]*:\n', body, maxsplit=1)[0]


def python_step(source, name):
    body = source.split('      - name: ' + name + '\n', 1)[1]
    body = re.split(r'\n      - (?:name:|uses:)', body, maxsplit=1)[0]
    script = re.search(r"(?ms)^          python - <<'PY'\n(.*?)^          PY$", body)
    if script is None:
        raise AssertionError('Missing Python validation in ' + name)
    return compile(textwrap.dedent(script.group(1)), '<workflow: ' + name + '>', 'exec')


VERSION_CHECK = python_step(RELEASE, 'Validate source version and publish branch')
MAC_SELECTION = python_step(RELEASE, 'Verify and select both Mac architecture packages')
ASSET_CHECK = python_step(RELEASE, 'Assemble source, deployment assets and exact reviewed description')


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.root)
        self.env = {
            'REQUESTED_VERSION': '', 'PUBLISH': 'false', 'EVENT_NAME': 'push',
            'GITHUB_REF': 'refs/heads/dev/release-0.9.1', 'DEFAULT_BRANCH': 'main',
            'SOURCE_TAG': '', 'GITHUB_OUTPUT': str(self.root / 'github-output'),
            'RELEASE_VERSION': '0.9.1', 'MACOS_REQUIRED': 'true',
            'RUNNER_TEMP': str(self.root / 'runner'),
        }

    def execute(self, code, **environment):
        with patch.dict(os.environ, self.env | environment):
            exec(code, {'__name__': '__workflow_validation__'})

    def version_source(self, version='0.9.1', notes=True):
        (self.root / 'server.py').write_text('VERSION = ' + repr(version) + '\n', encoding='utf-8')
        if notes:
            directory = self.root / 'docs/releases'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (version + '.md')).write_text('Reviewed public description\n', encoding='utf-8')

    def check_version(self, *, tracked_plan='', **environment):
        def git(command, **kwargs):
            return ('a' * 40 + '\n') if command[1] == 'rev-parse' else tracked_plan
        with patch('subprocess.check_output', side_effect=git):
            self.execute(VERSION_CHECK, **environment)
        return dict(line.split('=', 1) for line in (self.root / 'github-output').read_text().splitlines())

    def test_candidate_uses_source_version_and_does_not_require_manual_input(self):
        self.version_source()
        output = self.check_version()
        self.assertEqual(output, {'version': '0.9.1', 'tag': 'v0.9.1',
                                  'source_sha': 'a' * 40, 'macos_required': 'true'})

    def test_manual_version_and_exact_backfill_tag_must_match_source(self):
        self.version_source()
        for values in ({'EVENT_NAME': 'workflow_dispatch', 'REQUESTED_VERSION': '0.8.6'},
                       {'SOURCE_TAG': 'v0.8.6'}, {'SOURCE_TAG': 'main'}):
            with self.subTest(values=values), self.assertRaises(AssertionError):
                self.check_version(**values)

    def test_older_tag_backfill_does_not_require_missing_macos_sources(self):
        self.version_source('0.8.6')
        output = self.check_version(EVENT_NAME='workflow_dispatch', REQUESTED_VERSION='0.8.6',
                                    SOURCE_TAG='v0.8.6', GITHUB_REF='refs/heads/main', PUBLISH='true')
        self.assertEqual(output['macos_required'], 'false')
        self.assertEqual(output['tag'], 'v0.8.6')

    def test_publish_requires_manual_dispatch_on_default_branch(self):
        self.version_source()
        for values in ({'PUBLISH': 'true'},
                       {'EVENT_NAME': 'workflow_dispatch', 'REQUESTED_VERSION': '0.9.1', 'PUBLISH': 'true'}):
            with self.subTest(values=values), self.assertRaises(AssertionError):
                self.check_version(**values)
        output = self.check_version(EVENT_NAME='workflow_dispatch', REQUESTED_VERSION='0.9.1',
                                    GITHUB_REF='refs/heads/main', PUBLISH='true')
        self.assertEqual(output['version'], '0.9.1')

    def test_missing_description_or_tracked_internal_plan_blocks_release(self):
        self.version_source(notes=False)
        with self.assertRaisesRegex(AssertionError, 'description'):
            self.check_version()
        self.version_source()
        with self.assertRaisesRegex(AssertionError, 'Internal development plans'):
            self.check_version(EVENT_NAME='workflow_dispatch', REQUESTED_VERSION='0.9.1',
                               GITHUB_REF='refs/heads/main', PUBLISH='true', tracked_plan='docs/next-version.md\n')

    def test_other_push_branches_are_rejected_and_candidate_cannot_publish(self):
        self.version_source()
        with self.assertRaisesRegex(AssertionError, 'candidate branch'):
            self.check_version(GITHUB_REF='refs/heads/other')
        publish = job(RELEASE, 'publish')
        self.assertIn("if: github.event_name == 'workflow_dispatch' && inputs.publish && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", publish)
        self.assertIn('needs: [test, assemble]', publish)
        before_publish = RELEASE.split('\n  publish:\n', 1)[0]
        self.assertNotIn('contents: write', before_publish)
        self.assertNotIn('push: true', before_publish)
        self.assertNotIn('gh release ', before_publish)

    def mac_artifacts(self):
        destination = self.root / 'dist/release'
        destination.mkdir(parents=True)
        for arch in ('arm64', 'x64'):
            source = self.root / 'runner' / ('macos-' + arch)
            source.mkdir(parents=True)
            lines = []
            for extension in ('dmg', 'zip'):
                name = f'Gravelle-macOS-0.9.1-{arch}.{extension}'
                payload = ('explicit non-native test fixture: ' + name).encode()
                (source / name).write_bytes(payload)
                lines.append(hashlib.sha256(payload).hexdigest() + '  ' + name)
            (source / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
            (source / 'build-report.json').write_text('{"fixture": true}', encoding='ascii')
        return destination

    def test_mac_selection_copies_only_four_verified_architecture_packages(self):
        destination = self.mac_artifacts()
        self.execute(MAC_SELECTION)
        expected = {f'Gravelle-macOS-0.9.1-{arch}.{extension}'
                    for arch in ('arm64', 'x64') for extension in ('dmg', 'zip')}
        self.assertEqual({path.name for path in destination.iterdir()}, expected)

    def test_mac_selection_rejects_tampered_packages(self):
        self.mac_artifacts()
        (self.root / 'runner/macos-arm64/Gravelle-macOS-0.9.1-arm64.dmg').write_bytes(b'tampered fixture')
        with self.assertRaisesRegex(AssertionError, 'checksum mismatch'):
            self.execute(MAC_SELECTION)

    def test_mac_selection_rejects_unexpected_checksum_paths(self):
        destination = self.mac_artifacts()
        checksum = self.root / 'runner/macos-arm64/SHA256SUMS.txt'
        checksum.write_text('0' * 64 + '  ../unexpected.zip\n', encoding='ascii')
        with self.assertRaisesRegex(AssertionError, 'Unexpected Mac checksum'):
            self.execute(MAC_SELECTION)
        self.assertFalse(any(destination.iterdir()))

    def assembled_assets(self, *, macos=True):
        self.version_source()
        destination = self.root / 'dist/release'
        destination.mkdir(parents=True)
        names = {'Gravelle-Setup-0.9.1-x64.exe', 'Gravelle-Windows-0.9.1-x64.zip',
                 'Gravelle-Docker-0.9.1.zip', 'Gravelle-Source-0.9.1.zip'}
        if macos:
            names.update(f'Gravelle-macOS-0.9.1-{arch}.{extension}'
                         for arch in ('arm64', 'x64') for extension in ('dmg', 'zip'))
        lines = []
        for name in sorted(names):
            payload = ('explicit fixture: ' + name).encode()
            (destination / name).write_bytes(payload)
            lines.append(hashlib.sha256(payload).hexdigest() + '  ' + name)
        (destination / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
        (self.root / 'dist/release-notes.md').write_bytes((self.root / 'docs/releases/0.9.1.md').read_bytes())
        return destination

    def test_combined_checksum_validation_includes_mac_disk_images(self):
        self.assembled_assets()
        self.execute(ASSET_CHECK)

    def test_combined_validation_rejects_missing_dmg_and_duplicate_checksums(self):
        destination = self.assembled_assets()
        checksum = destination / 'SHA256SUMS.txt'
        original = checksum.read_text(encoding='ascii')
        without_dmg = '\n'.join(line for line in original.splitlines() if not line.endswith('.dmg')) + '\n'
        checksum.write_text(without_dmg, encoding='ascii')
        with self.assertRaisesRegex(AssertionError, 'every release asset exactly once'):
            self.execute(ASSET_CHECK)
        checksum.write_text(original + original.splitlines()[0] + '\n', encoding='ascii')
        with self.assertRaisesRegex(AssertionError, 'every release asset exactly once'):
            self.execute(ASSET_CHECK)

    def test_release_description_must_match_reviewed_document_byte_for_byte(self):
        self.assembled_assets()
        with (self.root / 'dist/release-notes.md').open('a', encoding='utf-8') as output:
            output.write('Automatically appended marketing text\n')
        with self.assertRaisesRegex(AssertionError, 'exactly match the reviewed'):
            self.execute(ASSET_CHECK)
        self.assertNotIn('1.8.3', RELEASE)
        self.assertNotIn('Record whether the Docker image is published', RELEASE)

    def test_previous_release_asset_layout_is_accepted_without_macos(self):
        self.assembled_assets(macos=False)
        self.execute(ASSET_CHECK, MACOS_REQUIRED='false')

    def test_formal_release_calls_native_validation_for_exact_source(self):
        macos = job(RELEASE, 'macos')
        self.assertIn('uses: ./.github/workflows/macos-build.yml', macos)
        self.assertIn('source_ref: ${{ needs.test.outputs.source_sha }}', macos)
        self.assertIn('sign_and_notarize: false', macos)
        self.assertIn("require_native_gui: ${{ github.event_name == 'workflow_dispatch' && inputs.publish }}", macos)
        self.assertIn('  workflow_call:\n', MACOS)
        self.assertIn('ref: ${{ inputs.source_ref || github.sha }}', MACOS)
        self.assertIn('if [ "$REQUIRE_NATIVE_GUI" = true ]; then', MACOS)
        required_gui = MACOS.split('if [ "$REQUIRE_NATIVE_GUI" = true ]; then', 1)[1].split('            fi', 1)[0]
        self.assertIn('exit 1', required_gui)
        self.assertIn('needs: [test, windows, macos, docker-validate]', job(RELEASE, 'assemble'))


if __name__ == '__main__':
    unittest.main()

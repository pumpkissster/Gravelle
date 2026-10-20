"""Exercise release archive assembly and the shared download checksums."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class ReleaseAssetTests(unittest.TestCase):
    def test_real_archives_and_all_platform_downloads_share_exact_checksums(self):
        with tempfile.TemporaryDirectory(prefix='gravelle-release-assets-') as temporary:
            root = Path(temporary)
            version = '0.9.0'
            source_names = [
                'server.py', 'README.md', 'compose.yaml',
                'compose.optimization.yaml', '.env.example',
                'docs/docker-release.md', 'packaging/source_manifest.json',
            ]
            contents = {
                'server.py': b'VERSION = "0.9.0"\n',
                'README.md': b'Generated release fixture.\n',
                'compose.yaml': b'services: {}\n',
                'compose.optimization.yaml': b'volumes: {}\n',
                '.env.example': b'GRAVELLE_IMAGE=example/gravelle:0.9.0\n',
                'docs/docker-release.md': b'Generated deployment instructions.\n',
                'packaging/source_manifest.json': json.dumps(source_names).encode(),
            }
            for name, content in contents.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            # Present on the source disk, but never allowlisted for export.
            for name in ('docs/next-version.md', 'VERIFICATION.md', '.env'):
                (root / name).write_bytes(b'Private fixture, never distribute.\n')

            output = root / 'dist' / 'release'
            output.mkdir(parents=True)
            downloads = {
                f'Gravelle-Setup-{version}-x64.exe': b'Windows installer fixture',
                f'Gravelle-Windows-{version}-x64.zip': b'Windows portable fixture',
            }
            for architecture in ('arm64', 'x64'):
                downloads[f'Gravelle-macOS-{version}-{architecture}.dmg'] = (
                    b'Mac disk-image fixture: ' + architecture.encode())
                downloads[f'Gravelle-macOS-{version}-{architecture}.zip'] = (
                    b'Mac app-archive fixture: ' + architecture.encode())
            for name, content in downloads.items():
                (output / name).write_bytes(content)
            (output / 'SHA256SUMS.txt').write_text('stale checksum fixture\n', encoding='ascii')

            spec = importlib.util.spec_from_file_location(
                'gravelle_release_asset_exporter', ROOT / 'packaging' / 'package_source.py')
            exporter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(exporter)
            with patch.object(exporter, 'ROOT', root), patch.object(
                    sys, 'argv', ['package_source.py', '--output-dir', str(output), '--version', version]), \
                    contextlib.redirect_stdout(io.StringIO()) as captured:
                exporter.main()

            archive_sources = {
                f'Gravelle-Source-{version}.zip': {
                    'gravelle-reader/' + name: content for name, content in contents.items()},
                f'Gravelle-Docker-{version}.zip': {
                    name: contents[name] for name in (
                        'compose.yaml', 'compose.optimization.yaml',
                        '.env.example', 'docs/docker-release.md')},
            }
            for name, expected_members in archive_sources.items():
                with zipfile.ZipFile(output / name) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(set(archive.namelist()), set(expected_members))
                    self.assertEqual(len(archive.namelist()), len(expected_members))
                    for member, content in expected_members.items():
                        self.assertEqual(archive.read(member), content)

            expected_names = set(downloads) | set(archive_sources)
            lines = (output / 'SHA256SUMS.txt').read_text(encoding='ascii').splitlines()
            checksums = [line.split('  ', 1) for line in lines]
            self.assertEqual(len(checksums), len(expected_names))
            self.assertEqual({name for _, name in checksums}, expected_names)
            for digest, name in checksums:
                self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())
            for name, content in downloads.items():
                self.assertEqual((output / name).read_bytes(), content)
            report = json.loads(captured.getvalue())
            self.assertEqual(report['version'], version)
            self.assertEqual(report['source_files'], len(source_names))
            self.assertEqual(set(report['assets']), expected_names)


if __name__ == '__main__':
    unittest.main()

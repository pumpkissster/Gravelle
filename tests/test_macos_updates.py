"""Trusted platform asset selection and atomic DMG downloads; no real network."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.core import (LauncherError, ReleaseConfig, check_update, download_update,
                          load_release_config)
from desktop.platforms import mac_architecture, macos_update_asset, user_directory


class MacUpdateTests(unittest.TestCase):
    def test_macos_data_is_outside_application_and_ignores_windows_environment(self):
        home = Path(tempfile.gettempdir()) / 'generated-user-home'
        value = user_directory('darwin', home, {'LOCALAPPDATA': 'wrong', 'XDG_DATA_HOME': 'wrong'})
        self.assertEqual(value, home / 'Library' / 'Application Support' / 'Gravelle')
        self.assertEqual(user_directory('win32', home, {'LOCALAPPDATA': str(home / 'AppData')}), home / 'AppData' / 'Gravelle')

    def test_mac_native_architecture_and_suffix_are_explicit(self):
        for value, expected in [('arm64', 'arm64'), ('aarch64', 'arm64'), ('x86_64', 'x64'), ('AMD64', 'x64')]:
            self.assertEqual(mac_architecture(value), expected)
            self.assertEqual(macos_update_asset(value), 'Gravelle-macOS-{version}-' + expected + '.dmg')
        with self.assertRaises(ValueError):
            mac_architecture('unknown')
        with self.assertRaises(LauncherError):
            ReleaseConfig('name/repo', asset_name='wrong.exe', platform='macos').validate()
        with self.assertRaises(LauncherError):
            ReleaseConfig('name/repo', asset_name='wrong.dmg').validate()

    def test_mac_resource_or_environment_always_selects_own_architecture(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {}, clear=True):
            root = Path(folder)
            (root / 'desktop').mkdir()
            (root / 'desktop/release.json').write_text(json.dumps({'repository': 'name/repo', 'asset_name': 'windows.exe'}))
            with patch('desktop.core.sys.platform', 'darwin'), patch('desktop.platforms.host_platform.machine', return_value='arm64'):
                result = load_release_config(root)
                self.assertEqual(result.platform, 'macos')
                self.assertEqual(result.asset_name, 'Gravelle-macOS-{version}-arm64.dmg')
                with patch.dict('os.environ', {'GRAVELLE_RELEASE_REPOSITORY': 'trusted/repo'}):
                    self.assertEqual(load_release_config(root).repository, 'trusted/repo')
                    self.assertEqual(load_release_config(root).asset_name, result.asset_name)

    def test_new_release_requires_matching_mac_asset_and_checksum(self):
        config = ReleaseConfig('name/repo', asset_name=macos_update_asset('arm64'), platform='macos')
        class Response:
            def __init__(self, names): self.names = names
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def geturl(self): return 'https://api.github.com/repos/name/repo/releases/latest'
            def read(self, maximum): return json.dumps({'tag_name': 'v0.8.7', 'assets': [{'name': n} for n in self.names]}).encode()
        for names, available in [([], False), (['Gravelle-macOS-0.8.7-x64.dmg', 'SHA256SUMS.txt'], False),
                                 (['Gravelle-macOS-0.8.7-arm64.dmg'], False),
                                 (['Gravelle-macOS-0.8.7-arm64.dmg', 'SHA256SUMS.txt'], True)]:
            with self.subTest(names=names), patch('desktop.core.urlopen', return_value=Response(names)):
                self.assertEqual(check_update(config, '0.8.6')['available'], available)

    def test_dmg_is_verified_and_published_atomically(self):
        payload = b'generated disk image fixture'
        digest = hashlib.sha256(payload).hexdigest()
        filename = 'Gravelle-macOS-0.8.7-arm64.dmg'
        def fake_download(url, maximum, destination=None):
            self.assertTrue(url.startswith('https://github.com/name/repo/releases/download/v0.8.7/'))
            if url.endswith('SHA256SUMS.txt'): return (digest + '  ' + filename).encode()
            destination.write(payload)
            return len(payload)
        config = ReleaseConfig('name/repo', asset_name=macos_update_asset('arm64'), platform='macos')
        with tempfile.TemporaryDirectory() as folder, patch('desktop.core._download', side_effect=fake_download):
            result = download_update(config, {'tag': 'v0.8.7'}, Path(folder))
            self.assertEqual(Path(result['path']).read_bytes(), payload)
            self.assertEqual(Path(result['path']).suffix, '.dmg')
            self.assertFalse(list(Path(folder).glob('*.part')))

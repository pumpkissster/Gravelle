"""Portable smoke orchestration checks; never claim native AppKit execution."""
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import json

from desktop.macos_entry import validate_smoke_paths
from desktop.smoke_macos import inspect_bundle, main
import desktop.smoke_windows


class MacSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gravelle-macos-smoke-contract-')
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'Gravelle 写真.app'
        self.executable = self.app / 'Contents/MacOS/Gravelle'
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b'fixture only, never executed')
        with (self.app / 'Contents/Info.plist').open('wb') as stream:
            plistlib.dump({'CFBundleExecutable': 'Gravelle',
                          'CFBundleShortVersionString': '0.8.6',
                          'LSMinimumSystemVersion': '15.0'}, stream)

    def tearDown(self):
        self.temporary.cleanup()

    def test_bundle_metadata_must_match_executable(self):
        self.assertEqual(inspect_bundle(self.executable)['version'], '0.8.6')
        with (self.app / 'Contents/Info.plist').open('wb') as stream:
            plistlib.dump({'CFBundleExecutable': 'Other'}, stream)
        with self.assertRaises(ValueError):
            inspect_bundle(self.executable)

    def test_ui_arguments_match_isolated_storage_contract(self):
        invoked = []
        def native_fixture(command, **kwargs):
            storage = Path(command[command.index('--data') + 1])
            config = Path(command[command.index('--config') + 1])
            report = Path(command[command.index('--report') + 1])
            validate_smoke_paths(storage, config, report)
            invoked.append(command)
            report.write_text(json.dumps({'status': 'passed', 'fixture': True}))
            return subprocess.CompletedProcess(command, 0, b'', b'')
        report = self.root / 'reports/macos.json'
        with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                patch('desktop.smoke_windows.run_smoke', return_value={'status': 'passed', 'checks': []}), \
                patch('desktop.smoke_macos.subprocess.run', side_effect=native_fixture):
            result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                           '--report', str(report)])
        self.assertEqual(result, 0)
        self.assertEqual(len(invoked), 1)
        self.assertTrue(json.loads(report.read_text())['native_gui']['fixture'])

    def test_absent_window_session_is_failure_not_gui_pass(self):
        report = self.root / 'report.json'
        with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                patch('desktop.smoke_windows.run_smoke', return_value={'status': 'passed', 'checks': []}), \
                patch('desktop.smoke_macos.subprocess.run', return_value=subprocess.CompletedProcess([], 77, b'', b'No GUI')):
            result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                           '--report', str(report)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(report.read_text())['native_gui']['returncode'], 77)

    def test_non_macos_host_refuses_native_verification(self):
        with patch('desktop.smoke_macos.sys.platform', 'win32'), self.assertRaises(SystemExit) as caught:
            main(['--executable', str(self.executable)])
        self.assertEqual(caught.exception.code, 2)

"""Portable smoke orchestration checks; never claim native AppKit execution."""
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import json

from desktop.macos_entry import validate_smoke_paths
from desktop.smoke_macos import inspect_bundle, main, version_preflight
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
                patch('desktop.smoke_macos.version_preflight', return_value={'status': 'passed'}), \
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
                patch('desktop.smoke_macos.version_preflight', return_value={'status': 'passed'}), \
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

    def test_preflight_records_elapsed_time_and_requires_exact_bundle_version(self):
        child = subprocess.CompletedProcess([], 0, b'0.8.6\n', b'generated diagnostic\n')
        with patch('desktop.smoke_macos.subprocess.run', return_value=child) as run, \
                patch('desktop.smoke_macos.time.monotonic', side_effect=[10.0, 12.125]):
            report = version_preflight(self.executable, '0.8.6')
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['duration_seconds'], 2.125)
        self.assertEqual(report['returncode'], 0)
        self.assertEqual(report['stdout'], '0.8.6\n')
        self.assertEqual(report['stderr_tail'], 'generated diagnostic\n')
        self.assertFalse(report['timed_out'])
        self.assertEqual(run.call_args.args[0], [str(self.executable), '--version'])
        self.assertEqual(run.call_args.kwargs['timeout'], 90)
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_preflight_wrong_version_or_exit_failure_blocks_service_smoke(self):
        for returncode, output in ((0, b'0.8.7\n'), (1, b'0.8.6\n')):
            with self.subTest(returncode=returncode, output=output):
                report_path = self.root / 'preflight-failed.json'
                child = subprocess.CompletedProcess([], returncode, output, b'startup diagnostic')
                with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                        patch('desktop.smoke_macos.subprocess.run', return_value=child), \
                        patch('desktop.smoke_windows.run_smoke') as service:
                    result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                                   '--report', str(report_path)])
                self.assertEqual(result, 1)
                service.assert_not_called()
                report = json.loads(report_path.read_text())
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['checks'], [])
                self.assertEqual(report['preflight']['status'], 'failed')
                self.assertEqual(report['preflight']['returncode'], returncode)
                self.assertEqual(report['preflight']['stderr_tail'], 'startup diagnostic')
                self.assertEqual(report['native_gui']['status'], 'not_run')

    def test_preflight_timeout_is_persisted_and_never_starts_service(self):
        failure = subprocess.TimeoutExpired([str(self.executable), '--version'], 90,
                                            output=b'partial version', stderr=b'partial diagnostic')
        report_path = self.root / 'preflight-timeout.json'
        with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                patch('desktop.smoke_macos.subprocess.run', side_effect=failure), \
                patch('desktop.smoke_macos.time.monotonic', side_effect=[100.0, 190.25]), \
                patch('desktop.smoke_windows.run_smoke') as service:
            result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                           '--report', str(report_path)])
        self.assertEqual(result, 1)
        service.assert_not_called()
        report = json.loads(report_path.read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['preflight']['timed_out'])
        self.assertIsNone(report['preflight']['returncode'])
        self.assertEqual(report['preflight']['duration_seconds'], 90.25)
        self.assertEqual(report['preflight']['stdout'], 'partial version')
        self.assertEqual(report['preflight']['stderr_tail'], 'partial diagnostic')

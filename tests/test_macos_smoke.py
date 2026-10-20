"""Portable smoke orchestration checks; never claim native AppKit execution."""
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import json

from desktop.macos_entry import validate_smoke_paths
from desktop.smoke_macos import inspect_bundle, main, native_resources_released, native_smoke, version_preflight
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

    def native_report(self, scenario):
        return {'status': 'passed', 'scenario': scenario, 'native_event_loop': True,
                'termination_requested': True, 'application_will_terminate': True,
                'checks': {'native_controls': True, 'service_stopped_on_termination': True},
                'service_pid': None if scenario == 'idle' else 123456,
                'service_port': None if scenario == 'idle' else 43210}

    def native_fixture(self, command, **kwargs):
        scenario = command[command.index('--smoke-scenario') + 1]
        report = Path(command[command.index('--report') + 1])
        report.write_text(json.dumps(self.native_report(scenario)))
        return subprocess.CompletedProcess(command, 0, b'', b'')

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
            scenario = command[command.index('--smoke-scenario') + 1]
            report.write_text(json.dumps(self.native_report(scenario)))
            return subprocess.CompletedProcess(command, 0, b'', b'')
        report = self.root / 'reports/macos.json'
        with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                patch('desktop.smoke_macos.version_preflight', return_value={'status': 'passed'}), \
                patch('desktop.smoke_windows.run_smoke', return_value={'status': 'passed', 'checks': []}), \
                patch('desktop.smoke_macos.subprocess.run', side_effect=native_fixture), \
                patch('desktop.smoke_macos.native_resources_released', return_value={
                    'owned_service_exited': True, 'service_port_released': True}):
            result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                           '--report', str(report)])
        self.assertEqual(result, 0)
        self.assertEqual(len(invoked), 3)
        native = json.loads(report.read_text())['native_gui']
        self.assertEqual(native['status'], 'passed')
        self.assertEqual([item['scenario'] for item in native['scenarios']], ['idle', 'running', 'hidden'])
        self.assertEqual(len({command[command.index('--data') + 1] for command in invoked}), 3)

    def test_absent_window_session_is_failure_not_gui_pass(self):
        report = self.root / 'report.json'
        with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                patch('desktop.smoke_macos.version_preflight', return_value={'status': 'passed'}), \
                patch('desktop.smoke_windows.run_smoke', return_value={'status': 'passed', 'checks': []}), \
                patch('desktop.smoke_macos.subprocess.run', return_value=subprocess.CompletedProcess([], 77, b'', b'No GUI')):
            result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                           '--report', str(report)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(report.read_text())['native_gui']['status'], 'failed')
        self.assertEqual(json.loads(report.read_text())['native_gui']['scenarios'][0]['returncode'], 77)

    def test_native_stop_without_real_termination_cannot_pass(self):
        def stop_only(command, **kwargs):
            report = self.native_report('idle')
            report['application_will_terminate'] = False
            Path(command[command.index('--report') + 1]).write_text(json.dumps(report))
            return subprocess.CompletedProcess(command, 0, b'', b'')
        with patch('desktop.smoke_macos.subprocess.run', side_effect=stop_only), \
                patch('desktop.smoke_macos.native_resources_released', return_value={
                    'owned_service_exited': True, 'service_port_released': True}):
            report = native_smoke(self.executable, self.root)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['scenarios'][0]['checks']['application_will_terminate'])

    def test_native_quit_timeout_is_persisted_and_not_reported_pass(self):
        def hangs(command, **kwargs):
            report = self.native_report('idle')
            Path(command[command.index('--report') + 1]).write_text(json.dumps(report))
            raise subprocess.TimeoutExpired(command, 75, stderr=b'quit stalled')
        with patch('desktop.smoke_macos.subprocess.run', side_effect=hangs), \
                patch('desktop.smoke_macos.native_resources_released', return_value={
                    'owned_service_exited': True, 'service_port_released': True}):
            report = native_smoke(self.executable, self.root)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['scenarios'][0]['timed_out'])
        self.assertEqual(report['scenarios'][0]['stderr_tail'], 'quit stalled')
        self.assertFalse(report['scenarios'][0]['checks']['application_process_exited'])

    def test_native_quit_requires_owned_child_and_port_to_be_released(self):
        with patch('desktop.smoke_macos.subprocess.run', side_effect=self.native_fixture), \
                patch('desktop.smoke_macos.native_resources_released', side_effect=[
                    {'owned_service_exited': True, 'service_port_released': True},
                    {'owned_service_exited': False, 'service_port_released': False}]):
            report = native_smoke(self.executable, self.root)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual([item['scenario'] for item in report['scenarios']], ['idle', 'running'])
        self.assertFalse(report['scenarios'][1]['checks']['owned_service_exited'])
        self.assertFalse(report['scenarios'][1]['checks']['service_port_released'])

    def test_running_case_cannot_claim_no_owned_service_was_started(self):
        def no_service(command, **kwargs):
            result = self.native_fixture(command, **kwargs)
            report_path = Path(command[command.index('--report') + 1])
            report = json.loads(report_path.read_text())
            report.update(service_pid=None, service_port=None)
            report_path.write_text(json.dumps(report))
            return result
        with patch('desktop.smoke_macos.subprocess.run', side_effect=no_service), \
                patch('desktop.smoke_macos.native_resources_released', return_value={
                    'owned_service_exited': True, 'service_port_released': True}):
            report = native_smoke(self.executable, self.root)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['scenarios'][1]['checks']['owned_service_identity'])

    def test_failed_native_check_or_mismatched_scenario_cannot_pass(self):
        for field, value in (('scenario', 'other'), ('checks', {'window_hidden_before_quit': False}),
                             ('checks', {'window_hidden_before_quit': 1})):
            with self.subTest(field=field, value=value):
                def invalid(command, **kwargs):
                    report = self.native_report('idle')
                    report[field] = value
                    Path(command[command.index('--report') + 1]).write_text(json.dumps(report))
                    return subprocess.CompletedProcess(command, 0, b'', b'')
                with patch('desktop.smoke_macos.subprocess.run', side_effect=invalid), \
                        patch('desktop.smoke_macos.native_resources_released', return_value={
                            'owned_service_exited': True, 'service_port_released': True}):
                    report = native_smoke(self.executable, self.root)
                self.assertEqual(report['status'], 'failed')

    def test_resource_probe_never_sends_a_termination_signal(self):
        with patch('desktop.smoke_macos.os.kill', side_effect=ProcessLookupError) as kill, \
                patch('desktop.smoke_macos.socket.socket') as socket:
            report = native_resources_released(123456, 43210, timeout=0)
        self.assertEqual(report, {'owned_service_exited': True, 'service_port_released': True})
        kill.assert_called_once_with(123456, 0)
        socket.return_value.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', 43210))

    def test_resource_probe_detects_live_child_or_busy_port(self):
        with patch('desktop.smoke_macos.os.kill'), \
                patch('desktop.smoke_macos.socket.socket') as socket:
            socket.return_value.__enter__.return_value.bind.side_effect = OSError('already in use')
            report = native_resources_released(123456, 43210, timeout=0)
        self.assertEqual(report, {'owned_service_exited': False, 'service_port_released': False})

    def test_gui_only_headless_is_explicitly_unsupported_not_passed(self):
        def headless(command, **kwargs):
            Path(command[command.index('--report') + 1]).write_text(json.dumps({
                'status': 'unsupported', 'reason': 'No graphical session'}))
            return subprocess.CompletedProcess(command, 77, b'', b'')
        report_path = self.root / 'unsupported.json'
        with patch('desktop.smoke_macos.sys.platform', 'darwin'), \
                patch('desktop.smoke_macos.version_preflight', return_value={'status': 'passed'}), \
                patch('desktop.smoke_windows.run_smoke') as service, \
                patch('desktop.smoke_macos.subprocess.run', side_effect=headless):
            result = main(['--executable', str(self.executable), '--work-root', str(self.root / 'work'),
                           '--report', str(report_path), '--gui-only'])
        self.assertEqual(result, 77)
        service.assert_not_called()
        report = json.loads(report_path.read_text())
        self.assertEqual(report['status'], 'unsupported')
        self.assertEqual(report['native_gui']['status'], 'unsupported')
        self.assertEqual(len(report['native_gui']['scenarios']), 1)

    def test_gui_only_cannot_be_combined_with_skip_gui(self):
        with self.assertRaises(SystemExit) as caught:
            main(['--executable', str(self.executable), '--gui-only', '--skip-gui'])
        self.assertEqual(caught.exception.code, 2)

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

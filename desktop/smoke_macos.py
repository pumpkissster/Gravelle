#!/usr/bin/env python3
"""Validate a real macOS bundle with isolated data, then exercise its workers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def inspect_bundle(executable: Path) -> dict:
    executable = executable.resolve(strict=True)
    if (not executable.is_file() or executable.name != 'Gravelle'
            or executable.parent.name != 'MacOS'
            or executable.parent.parent.name != 'Contents'
            or executable.parent.parent.parent.suffix != '.app'):
        raise ValueError('Select Gravelle.app/Contents/MacOS/Gravelle')
    bundle = executable.parent.parent.parent
    with (bundle / 'Contents' / 'Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    if info.get('CFBundleExecutable') != executable.name:
        raise ValueError('Bundle executable metadata does not match')
    return {'bundle': str(bundle), 'executable': str(executable),
            'version': info.get('CFBundleShortVersionString'),
            'minimum_macos': info.get('LSMinimumSystemVersion')}


def version_preflight(executable: Path, expected_version: str) -> dict:
    """Measure first frozen launch before exercising the private service pipe.

    The normal 35-second service smoke is unchanged. A failed or timed-out
    executable probe is diagnostic failure, never a substitute ready signal.
    """
    started = time.monotonic()
    report = {'status': 'failed', 'timeout_seconds': 90, 'returncode': None,
              'timed_out': False, 'stdout': '', 'stderr_tail': ''}
    def decoded(value):
        return value.decode('utf8', errors='replace') if isinstance(value, bytes) else (value or '')
    try:
        if not isinstance(expected_version, str) or not expected_version:
            raise ValueError('Info.plist has no valid application version')
        child = subprocess.run([str(executable), '--version'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        output = decoded(child.stdout)
        report.update(returncode=child.returncode, stdout=output[-4096:],
                      stderr_tail=decoded(child.stderr)[-4096:])
        if child.returncode != 0:
            report['error'] = 'Frozen version probe exited unsuccessfully'
        elif output.strip() != expected_version:
            report['error'] = 'Frozen executable version does not match Info.plist'
        else:
            report['status'] = 'passed'
    except subprocess.TimeoutExpired as error:
        report.update(timed_out=True, stdout=decoded(error.stdout)[-4096:],
                      stderr_tail=decoded(error.stderr)[-4096:],
                      error='Frozen version probe timed out before service validation')
    except (OSError, ValueError) as error:
        report['error'] = str(error)
    finally:
        report['duration_seconds'] = round(time.monotonic() - started, 3)
    return report


NATIVE_SCENARIOS = ('idle', 'running', 'hidden')


def native_resources_released(pid: int | None, port: int | None, timeout: float = 5) -> dict:
    """Read-only checks for the exact child and loopback port recorded by smoke."""
    deadline = time.monotonic() + timeout
    checks = {'owned_service_exited': pid is None, 'service_port_released': port is None}
    while True:
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                checks['owned_service_exited'] = True
            except PermissionError:
                checks['owned_service_exited'] = False
        if port is not None:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(('127.0.0.1', port))
                checks['service_port_released'] = True
            except OSError:
                checks['service_port_released'] = False
        if all(checks.values()) or time.monotonic() >= deadline:
            return checks
        time.sleep(0.1)


def native_smoke(executable: Path, work: Path) -> dict:
    """Run real terminate events in fresh apps; app.stop() cannot satisfy this."""
    scenarios = []
    for scenario in NATIVE_SCENARIOS:
        with tempfile.TemporaryDirectory(prefix=f'gravelle-native-{scenario}-', dir=work) as temporary:
            scratch = Path(temporary)
            report_path = scratch / 'gui-report.json'
            command = [str(executable), '--smoke-launcher', '--smoke-scenario', scenario,
                       '--data', str(scratch), '--config', str(scratch / 'launcher.json'),
                       '--report', str(report_path)]
            item = {'scenario': scenario, 'status': 'failed', 'returncode': None,
                    'timed_out': False, 'timeout_seconds': 75}
            started = time.monotonic()
            try:
                child = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=75)
                item.update(returncode=child.returncode,
                            stderr_tail=child.stderr.decode('utf8', errors='replace')[-2000:])
            except subprocess.TimeoutExpired as error:
                item.update(timed_out=True, error='Native quit did not exit the application process within 75 seconds',
                            stderr_tail=(error.stderr or b'').decode('utf8', errors='replace')[-2000:])
            except OSError as error:
                item['error'] = str(error)
            item['duration_seconds'] = round(time.monotonic() - started, 3)
            try:
                native = json.loads(report_path.read_text(encoding='utf8'))
                if not isinstance(native, dict):
                    raise ValueError('Native report must be an object')
                item['native'] = native
            except (OSError, ValueError):
                native = {}
                item.setdefault('error', 'Native launcher did not write a valid termination report')
            captures = {}
            for name in ('port-focused.png', 'port-unfocused.png'):
                captured = scratch / name
                if captured.is_file() and not captured.is_symlink():
                    destination = work / (scenario + '-' + name)
                    shutil.copyfile(captured, destination)
                    captures[name] = str(destination)
            if captures:
                item['screenshots'] = captures
            if (item['returncode'] == 77 and native.get('status') == 'unsupported'
                    and not item['timed_out']):
                item['status'] = 'unsupported'
                scenarios.append(item)
                break
            process_id, port = native.get('service_pid'), native.get('service_port')
            resources_valid = (process_id is None and port is None if scenario == 'idle' else
                               type(process_id) is int and process_id > 0 and
                               type(port) is int and 1 <= port <= 65535)
            contract = {
                'application_process_exited': item['returncode'] == 0 and not item['timed_out'],
                'native_event_loop': native.get('native_event_loop') is True,
                'requested_scenario': native.get('scenario') == scenario,
                'termination_requested': native.get('termination_requested') is True,
                'application_will_terminate': native.get('application_will_terminate') is True,
                'native_checks_passed': (native.get('status') == 'passed'
                                         and isinstance(native.get('checks'), dict)
                                         and bool(native['checks'])
                                         and all(value is True for value in native['checks'].values())),
                'owned_service_identity': resources_valid,
            }
            if resources_valid:
                contract.update(native_resources_released(process_id, port))
            item['checks'] = contract
            if all(contract.values()):
                item['status'] = 'passed'
            else:
                item.setdefault('error', 'Native quit validation failed: ' +
                                ', '.join(name for name, passed in contract.items() if not passed))
            scenarios.append(item)
            if item['status'] != 'passed':
                break
    status = ('failed' if any(item['status'] == 'failed' for item in scenarios) else
              'unsupported' if any(item['status'] == 'unsupported' for item in scenarios) else
              'passed' if len(scenarios) == len(NATIVE_SCENARIOS) else 'failed')
    return {'status': status, 'scenarios': scenarios}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, default=Path('work/macos-smoke'))
    parser.add_argument('--report', type=Path)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--skip-gui', action='store_true', help='Run service checks only; native UI remains unverified')
    scope.add_argument('--gui-only', action='store_true', help='Run native UI and quit checks with isolated storage')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('Real .app validation must run on macOS; this host cannot verify a macOS application.')
    try:
        bundle = inspect_bundle(args.executable)
        work = args.work_root.resolve()
        if work == Path(work.anchor):
            raise ValueError('Work root must be a dedicated directory')
        work.mkdir(parents=True, exist_ok=True)
        preflight = version_preflight(Path(bundle['executable']), bundle['version'])
        result = {'status': 'failed', 'checks': [], 'error': 'Frozen executable preflight failed'}
        if preflight['status'] == 'passed' and not args.gui_only:
            from desktop.smoke_windows import run_smoke
            result = run_smoke(Path(bundle['executable']), work)
        elif preflight['status'] == 'passed':
            result = {'status': 'passed', 'checks': [], 'service_checks': 'not_run'}
        result.update(platform='macos', bundle=bundle, preflight=preflight,
                      native_gui={'status': 'not_run'})
        if result['status'] == 'passed' and not args.skip_gui:
            result['native_gui'] = native_smoke(Path(bundle['executable']), work)
            if result['native_gui']['status'] != 'passed':
                result['status'] = ('unsupported' if args.gui_only and result['native_gui']['status'] == 'unsupported'
                                    else 'failed')
        report_path = (args.report or work / 'report.json').resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print(json.dumps({'status': result['status'], 'checks': len(result['checks']),
                          'native_gui': result['native_gui'].get('status'), 'report': str(report_path)}))
        return 0 if result['status'] == 'passed' else 77 if result['status'] == 'unsupported' else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate a real macOS bundle with isolated data, then exercise its workers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, default=Path('work/macos-smoke'))
    parser.add_argument('--report', type=Path)
    parser.add_argument('--skip-gui', action='store_true', help='Run service checks only; native UI remains unverified')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('Real .app validation must run on macOS; this host cannot verify a macOS application.')
    try:
        bundle = inspect_bundle(args.executable)
        work = args.work_root.resolve()
        if work == Path(work.anchor):
            raise ValueError('Work root must be a dedicated directory')
        work.mkdir(parents=True, exist_ok=True)
        from desktop.smoke_windows import run_smoke
        result = run_smoke(Path(bundle['executable']), work)
        result['platform'] = 'macos'
        result['bundle'] = bundle
        result['native_gui'] = {'status': 'not_run'}
        if result['status'] == 'passed' and not args.skip_gui:
            with tempfile.TemporaryDirectory(prefix='gravelle-native-ui-', dir=work) as temporary:
                scratch = Path(temporary)
                ui_report = scratch / 'gui-report.json'
                command = [bundle['executable'], '--smoke-launcher', '--data', str(scratch),
                           '--config', str(scratch / 'launcher.json'), '--report', str(ui_report)]
                child = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=30)
                if child.returncode != 0 or not ui_report.is_file():
                    result['native_gui'] = {'status': 'failed', 'returncode': child.returncode,
                                            'message': child.stderr.decode('utf8', errors='replace')[-2000:]}
                    result['status'] = 'failed'
                else:
                    result['native_gui'] = json.loads(ui_report.read_text(encoding='utf8'))
                    if result['native_gui'].get('status') != 'passed':
                        result['status'] = 'failed'
        report_path = (args.report or work / 'report.json').resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print(json.dumps({'status': result['status'], 'checks': len(result['checks']),
                          'native_gui': result['native_gui'].get('status'), 'report': str(report_path)}))
        return 0 if result['status'] == 'passed' else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

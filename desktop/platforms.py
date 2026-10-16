"""Small platform choices shared by native launchers; no GUI imports."""
from __future__ import annotations

import os
from pathlib import Path
import platform as host_platform
import sys


def user_directory(platform=None, home=None, environ=None) -> Path:
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    if platform == 'darwin':
        return home / 'Library' / 'Application Support' / 'Gravelle'
    if platform == 'win32':
        return Path(environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / 'Gravelle'
    base = Path(environ.get('XDG_DATA_HOME') or home / '.local' / 'share')
    if not base.is_absolute():
        base = home / '.local' / 'share'
    return base / 'Gravelle'


def mac_architecture(machine=None) -> str:
    machine = host_platform.machine() if machine is None else machine
    if not isinstance(machine, str):
        raise ValueError('Unsupported macOS architecture')
    name = machine.casefold()
    if name in ('arm64', 'aarch64'):
        return 'arm64'
    if name in ('x86_64', 'amd64', 'x64'):
        return 'x64'
    raise ValueError('Unsupported macOS architecture: ' + machine)


def macos_update_asset(architecture=None) -> str:
    architecture = mac_architecture(architecture)
    return 'Gravelle-macOS-{version}-' + architecture + '.dmg'

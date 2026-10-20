#!/usr/bin/env python3
"""Build a native macOS app, drag-to-Applications DMG and symlink-safe ZIP.

--validate-config is portable and side-effect free. Actual packaging requires
macOS and a Python interpreter with the same architecture as the target.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))
ARCHITECTURES = {'arm64': 'arm64', 'x64': 'x86_64'}
MINIMUM_MACOS = '15.0'
ENTITLEMENTS = {'com.apple.security.cs.allow-jit': True}
LICENSE_PACKAGES = ('Pillow', 'PyInstaller', 'numpy', 'opencv-python-headless',
                    'pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-Quartz')
PYOBJC_CORE_LICENSE_VERSION = '12.2.2'
PYOBJC_CORE_LICENSE_SHA256 = '0ca04b07928d4872b9d9bb22187ca0426dd8bfab08f26eada0999a71dc81aaff'


class BuildError(Exception):
    pass


def project_path(root: Path, value: str | Path) -> Path:
    """Reject root/outside paths, including a symlink into another directory."""
    root = root.resolve()
    target = (root / value).resolve()
    if target == root or root not in target.parents:
        raise BuildError('Build output must stay inside this project, below its root.')
    return target


def read_version(root: Path) -> str:
    tree = ast.parse((root / 'server.py').read_text(encoding='utf-8'))
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets)]
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(r'\d+\.\d+\.\d+', values[0]):
        raise BuildError('server.VERSION must be a stable three-part version.')
    return values[0]


def configuration(root: Path, repository: str, architecture: str, output: str | Path) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+', repository):
        raise BuildError('Repository must be a GitHub owner/repository name.')
    if architecture not in ARCHITECTURES:
        raise BuildError('Choose arm64 or x64.')
    version = read_version(root)
    return {'version': version, 'repository': repository, 'architecture': architecture,
            'native_architecture': ARCHITECTURES[architecture], 'minimum_macos': MINIMUM_MACOS,
            'output': str(project_path(root, output)),
            'dmg': f'Gravelle-macOS-{version}-{architecture}.dmg',
            'zip': f'Gravelle-macOS-{version}-{architecture}.zip',
            'release_config': {'repository': repository, 'platform': 'macos',
                               'asset_name': f'Gravelle-macOS-{{version}}-{architecture}.dmg',
                               'sha256_asset_name': 'SHA256SUMS.txt'}}


def require_native(architecture: str) -> None:
    if sys.platform != 'darwin':
        raise BuildError('macOS packaging requires a native Mac. Use --validate-config for portable validation.')
    if sys.version_info < (3, 12) or sys.maxsize <= 2**32:
        raise BuildError('Use 64-bit Python 3.12 or newer.')
    if platform.machine().lower() != ARCHITECTURES[architecture]:
        raise BuildError('Build with a native Python interpreter matching the target architecture; cross-compilation is not supported.')
    try:
        major = int(platform.mac_ver()[0].split('.')[0])
    except ValueError as error:
        raise BuildError('Cannot determine the macOS version.') from error
    if major < 15:
        raise BuildError('This validation build requires macOS 15 or newer.')


def run(command: list[str], *, env: dict | None = None, capture: bool = False) -> str:
    # Commands are arrays, never shell strings; signing credentials are read
    # only from a previously prepared keychain, never command-line passwords.
    result = subprocess.run(command, check=True, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ''


def copy_pinned_license_fallback(name: str, version: str, target: Path) -> bool:
    """The official core 12.2.2 wheel omits its source distribution's license.

    Do not turn a short SPDX/metadata field into a substitute license or extend
    this exception to later versions without checking their upstream notices.
    """
    if name != 'pyobjc-core':
        return False
    if version != PYOBJC_CORE_LICENSE_VERSION:
        raise BuildError('No verified license fallback for pyobjc-core ' + str(version) + '.')
    source = ROOT / 'desktop' / 'licenses' / 'pyobjc-core-12.2.2-LICENSE.txt'
    try:
        content = source.read_bytes()
    except OSError as error:
        raise BuildError('The pinned pyobjc-core license notice is missing or unreadable.') from error
    if hashlib.sha256(content).hexdigest() != PYOBJC_CORE_LICENSE_SHA256:
        raise BuildError('The pinned pyobjc-core license notice failed SHA-256 verification.')
    destination = target / name / 'License.txt'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return True


def collect_licenses(target: Path) -> None:
    from desktop.pdf_packaging import collect_pdfium_licenses
    from desktop.share_packaging import collect_share_licenses
    target.mkdir(parents=True, exist_ok=True)
    python_candidates = (Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'LICENSE',
                         Path(sys.base_prefix) / 'Resources' / 'LICENSE.txt')
    for candidate in python_candidates:
        if candidate.is_file():
            shutil.copyfile(candidate, target / 'Python-LICENSE.txt')
            break
    else:
        # Some hosted Python layouts keep the license with their stdlib.
        import sysconfig
        candidate = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
        if not candidate.is_file():
            raise BuildError('The Python runtime license is missing; do not distribute an incomplete bundle.')
        shutil.copyfile(candidate, target / 'Python-LICENSE.txt')
    for name in LICENSE_PACKAGES:
        dist = importlib.metadata.distribution(name)
        found = False
        for entry in dist.files or ():
            relative = Path(entry)
            if (any(word in relative.name.lower() for word in ('license', 'copying', 'notice'))
                    and relative.suffix.lower() in ('.txt', '.md', '.rst', '')
                    and not relative.is_absolute() and '..' not in relative.parts):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    destination = target / name / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    found = True
        if not found and not copy_pinned_license_fallback(name, dist.version, target):
            raise BuildError(f'License notices missing for {name}.')
    try:
        collect_pdfium_licenses(target)
        collect_share_licenses(target)
    except (OSError, RuntimeError, importlib.metadata.PackageNotFoundError) as error:
        raise BuildError(str(error)) from error


def macos_icon_png(source: Path) -> bytes:
    """Give only the Mac app icon a white rounded tile and Dock-safe margins."""
    from PIL import Image
    import resvg_py

    original = source.read_bytes()
    with Image.open(io.BytesIO(original)) as image:
        if image.format != 'PNG' or image.mode != 'RGBA':
            raise BuildError('The supplied app logo must be a transparent RGBA PNG.')
        image.verify()
    encoded = base64.b64encode(original).decode('ascii')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">'
           '<rect x="100" y="100" width="824" height="824" rx="185" fill="#fff"/>'
           '<image x="205" y="205" width="614" height="614" preserveAspectRatio="xMidYMid meet" '
           f'href="data:image/png;base64,{encoded}"/>'
           '</svg>')
    png = resvg_py.svg_to_bytes(svg_string=svg, width=1024, height=1024, skip_system_fonts=True)
    with Image.open(io.BytesIO(png)) as image:
        if image.mode != 'RGBA' or image.size != (1024, 1024) or image.getextrema()[3] != (0, 255):
            raise BuildError('The Mac app icon must render as a 1024-pixel RGBA PNG with transparent outer margins.')
    return png


def prepare_resources(root: Path, resources: Path, config: dict) -> None:
    from PIL import Image
    import resvg_py
    assets = resources / 'desktop' / 'assets'
    assets.mkdir(parents=True)
    # PyObjC creates small Objective-C-to-Python callback stubs. Its documented
    # hardened-runtime requirement is allow-jit; no blanket library-validation
    # or executable-page-protection exception is granted.
    (resources / 'entitlements.plist').write_bytes(plistlib.dumps(ENTITLEMENTS))
    (resources / 'release.json').write_text(json.dumps({'repository': config['repository']}, indent=2) + '\n', encoding='utf-8')
    (resources / 'desktop' / 'release.json').write_text(json.dumps(config['release_config'], indent=2) + '\n', encoding='utf-8')
    png = resvg_py.svg_to_bytes(svg_path=str(root / 'static' / 'gravelle-wordmark.svg'), width=600, skip_system_fonts=True)
    with Image.open(io.BytesIO(png)) as image:
        if image.mode != 'RGBA' or image.width != 600 or image.getextrema()[3] != (0, 255):
            raise BuildError('The wordmark must render as a transparent 600-pixel RGBA PNG.')
    (assets / 'gravelle-wordmark.png').write_bytes(png)
    with Image.open(io.BytesIO(macos_icon_png(root / 'static' / 'gravelle-icon.png'))) as image:
        image.save(resources / 'Gravelle.icns', format='ICNS',
                   sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
    collect_licenses(resources / 'licenses')
    (resources / 'THIRD_PARTY_NOTICES.md').write_text(
        '# Gravelle macOS runtime\n\n'
        'This bundle includes Python (PSF license), Pillow (HPND), NumPy (BSD-3-Clause), '
        'OpenCV (Apache-2.0), the opencv-python-headless packaging scripts (MIT), and '
        'PyObjC Cocoa bindings (MIT). Their license files are included in licenses/ and '
        'the bundled runtime directories. PyInstaller bootloader license and distribution exception are included.\n\n'
        'PDF reading uses pypdfium2 (Apache-2.0 or BSD-3-Clause) and PDFium. The complete '
        'PDFium and bundled component notices, including the libjpeg-turbo IJG license, '
        'are preserved in licenses/pypdfium2/.\n\n'
        'Shared photo pages use cryptography (Apache-2.0 or BSD-3-Clause), CFFI/pycparser (MIT), '
        'and the wheel\'s OpenSSL runtime. Full notices are under licenses/cryptography/, '
        'licenses/cffi/, and licenses/pycparser/. The browser fallback uses pinned @noble/ciphers '
        '(MIT); its notice is in static/vendor/noble-ciphers-LICENSE.txt.\n\n'
        'The Libre Caslon Text font includes its SIL Open Font License at static/fonts/OFL.txt. '
        'Gravelle\'s license is included as LICENSE. No GPU runtime or downloaded model is included.\n\n'
        'Build-only resvg-py/resvg renders the supplied SVG; the renderer is excluded from the runtime.\n\n'
        f'Source: https://github.com/{config["repository"]}\n', encoding='utf-8')


def notarize(path: Path, profile: str, keychain: str = '') -> None:
    command = ['xcrun', 'notarytool', 'submit', str(path), '--keychain-profile', profile,
               '--wait', '--timeout', '30m', '--output-format', 'json']
    if keychain:
        command.extend(['--keychain', keychain])
    result = json.loads(run(command, capture=True))
    if result.get('status') != 'Accepted':
        # No complete response dump: service diagnostics can contain identity data.
        raise BuildError('Apple notarization was not accepted; inspect the private notarization log.')


def archive_app(app: Path, target: Path) -> None:
    # ditto preserves bundle symlinks, modes, resource forks and extended attrs.
    run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(target)])


def write_checksums(paths: list[Path], target: Path) -> None:
    lines = []
    for path in sorted(paths, key=lambda value: value.name):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        lines.append(f'{digest.hexdigest()}  {path.name}\n')
    target.write_text(''.join(lines), encoding='ascii')


def build(config: dict, *, identity: str = '', notary_profile: str = '', keychain: str = '') -> dict:
    require_native(config['architecture'])
    if identity and not re.fullmatch(r'Developer ID Application: [^\r\n]+ \([A-Z0-9]{10}\)', identity):
        raise BuildError('Use a Developer ID Application signing identity for distributable macOS builds.')
    if notary_profile and not identity:
        raise BuildError('Notarization requires a Developer ID signing identity.')
    for tool in ('/usr/bin/ditto', '/usr/bin/hdiutil', '/usr/bin/codesign', '/usr/bin/xcrun'):
        if not Path(tool).is_file():
            raise BuildError(f'Required macOS tool missing: {tool}')
    output = Path(config['output'])
    output.mkdir(parents=True, exist_ok=True)
    work = project_path(ROOT, 'work/macos-build')
    work.mkdir(parents=True, exist_ok=True)
    build_root = Path(tempfile.mkdtemp(prefix=config['architecture'] + '-', dir=work))
    resources = build_root / 'resources'
    prepare_resources(ROOT, resources, config)
    environment = os.environ.copy()
    environment.update({'GRAVELLE_BUILD_RESOURCES': str(resources),
                        'GRAVELLE_BUILD_VERSION': config['version'],
                        'GRAVELLE_MACOS_ARCH': config['native_architecture'],
                        'GRAVELLE_CODESIGN_IDENTITY': identity,
                        'MACOSX_DEPLOYMENT_TARGET': MINIMUM_MACOS,
                        'PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR': '1'})
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
         '--distpath', str(build_root / 'dist'), '--workpath', str(build_root / 'pyinstaller'),
         str(ROOT / 'desktop' / 'gravelle-macos.spec')], env=environment)
    app = build_root / 'dist' / 'Gravelle.app'
    binary = app / 'Contents' / 'MacOS' / 'Gravelle'
    if not binary.is_file():
        raise BuildError('PyInstaller did not produce the application executable.')
    native = run(['/usr/bin/lipo', '-archs', str(binary)], capture=True).split()
    if native != [config['native_architecture']]:
        raise BuildError('The resulting executable does not match the requested native architecture.')
    # All resources are already inside the bundle; do not mutate them after signing.
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    if notary_profile:
        submission = build_root / 'notary-app.zip'
        archive_app(app, submission)
        notarize(submission, notary_profile, keychain)
        run(['xcrun', 'stapler', 'staple', str(app)])
        run(['xcrun', 'stapler', 'validate', str(app)])
    final_zip = output / config['zip']
    archive_app(app, build_root / config['zip'])
    stage = build_root / 'dmg-stage'
    stage.mkdir()
    run(['/usr/bin/ditto', str(app), str(stage / 'Gravelle.app')])
    (stage / 'Applications').symlink_to('/Applications', target_is_directory=True)
    shutil.copyfile(ROOT / 'LICENSE', stage / 'LICENSE')
    (stage / 'Install.txt').write_text('Drag Gravelle.app to Applications, then open it from Applications.\n'
                                     '将 Gravelle.app 拖到 Applications，然后从“应用程序”打开。\n', encoding='utf-8')
    final_dmg = output / config['dmg']
    temporary_dmg = build_root / config['dmg']
    run(['/usr/bin/hdiutil', 'create', '-volname', 'Gravelle', '-srcfolder', str(stage),
         '-format', 'UDZO', '-ov', str(temporary_dmg)])
    if identity:
        run(['/usr/bin/codesign', '--force', '--timestamp', '--sign', identity, str(temporary_dmg)])
        run(['/usr/bin/codesign', '--verify', '--strict', str(temporary_dmg)])
    if notary_profile:
        notarize(temporary_dmg, notary_profile, keychain)
        run(['xcrun', 'stapler', 'staple', str(temporary_dmg)])
        run(['xcrun', 'stapler', 'validate', str(temporary_dmg)])
        run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=2', str(app)])
    os.replace(build_root / config['zip'], final_zip)
    os.replace(temporary_dmg, final_dmg)
    write_checksums([final_dmg, final_zip], output / 'SHA256SUMS.txt')
    report = {'version': config['version'], 'architecture': config['architecture'],
              'minimum_macos': MINIMUM_MACOS, 'app': str(app),
              'signing': 'developer_id' if identity else 'ad_hoc', 'notarized': bool(notary_profile),
              'files': [str(final_dmg), str(final_zip)]}
    (output / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default='pumpkissster/Gravelle')
    parser.add_argument('--arch', choices=ARCHITECTURES, default='arm64' if platform.machine() == 'arm64' else 'x64')
    parser.add_argument('--output-dir', default='dist/macos')
    parser.add_argument('--validate-config', action='store_true')
    parser.add_argument('--codesign-identity', default=os.environ.get('GRAVELLE_CODESIGN_IDENTITY', ''))
    parser.add_argument('--notary-profile', default=os.environ.get('GRAVELLE_NOTARY_PROFILE', ''))
    parser.add_argument('--keychain', default=os.environ.get('GRAVELLE_NOTARY_KEYCHAIN', ''))
    args = parser.parse_args(argv)
    try:
        config = configuration(ROOT, args.repository, args.arch, args.output_dir)
        if args.validate_config:
            print(json.dumps(config, ensure_ascii=False, indent=2))
        else:
            report = build(config, identity=args.codesign_identity,
                           notary_profile=args.notary_profile, keychain=args.keychain)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (BuildError, OSError, ValueError, subprocess.CalledProcessError) as error:
        # No environment or full subprocess argument dump, keeping credentials
        # and private keychain paths out of generic build errors.
        message = str(error) if isinstance(error, BuildError) else type(error).__name__
        print('macOS build failed: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

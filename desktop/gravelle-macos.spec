"""Native, onedir macOS bundle; the same executable handles GUI and workers."""
from pathlib import Path
import os

root = Path(SPECPATH).resolve().parent
resources = Path(os.environ['GRAVELLE_BUILD_RESOURCES']).resolve()
architecture = os.environ['GRAVELLE_MACOS_ARCH']
identity = os.environ.get('GRAVELLE_CODESIGN_IDENTITY') or None
analysis = Analysis(
    [str(root / 'desktop' / 'macos_entry.py')],
    pathex=[str(root)], binaries=[],
    datas=[
        (str(root / 'static'), 'static'),
        (str(resources / 'release.json'), '.'),
        (str(resources / 'desktop' / 'release.json'), 'desktop'),
        (str(resources / 'desktop' / 'assets' / 'gravelle-wordmark.png'), 'desktop/assets'),
        (str(root / 'LICENSE'), '.'),
        (str(resources / 'THIRD_PARTY_NOTICES.md'), '.'),
        (str(resources / 'licenses'), 'licenses'),
    ],
    hiddenimports=[
        'server', 'accounts', 'password_reset', 'transcoding', 'image_search',
        'image_search_features', 'task_scheduler', 'library_covers', 'cache_manager',
        'desktop.core', 'desktop.platforms', 'desktop.runtime', 'desktop.macos_launcher',
        'PIL.Image', 'cv2', 'numpy', 'objc', 'AppKit', 'Foundation', 'Quartz', 'PyObjCTools.AppHelper',
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['resvg_py', 'tkinter'], noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name='Gravelle', console=False, argv_emulation=False,
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    target_arch=architecture, codesign_identity=identity,
    entitlements_file=str(resources / 'entitlements.plist'),
)
collected = COLLECT(executable, analysis.binaries, analysis.datas,
                    strip=False, upx=False, name='Gravelle')
bundle = BUNDLE(
    collected, name='Gravelle.app', icon=str(resources / 'Gravelle.icns'),
    bundle_identifier='com.gravelle.launcher',
    version=os.environ['GRAVELLE_BUILD_VERSION'],
    info_plist={
        'CFBundleDisplayName': 'Gravelle',
        'CFBundleShortVersionString': os.environ['GRAVELLE_BUILD_VERSION'],
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '15.0',
        'NSLocalNetworkUsageDescription': 'Gravelle shares your photo libraries with devices on your local network when you enable LAN access.',
    },
)

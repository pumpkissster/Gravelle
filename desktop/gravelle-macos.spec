"""Native, onedir macOS bundle; the same executable handles GUI and workers."""
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

pdfium_binaries = collect_dynamic_libs('pypdfium2_raw')
if not pdfium_binaries:
    raise RuntimeError('The bundled PDFium shared library is missing; install the pinned pypdfium2 wheel.')
pdfium_data = (collect_data_files('pypdfium2', includes=['version.json']) +
               collect_data_files('pypdfium2_raw', includes=['version.json']))
if {destination for _, destination in pdfium_data} != {'pypdfium2', 'pypdfium2_raw'}:
    raise RuntimeError('PDFium version resources are missing; install the pinned pypdfium2 wheel.')

root = Path(SPECPATH).resolve().parent
resources = Path(os.environ['GRAVELLE_BUILD_RESOURCES']).resolve()
architecture = os.environ['GRAVELLE_MACOS_ARCH']
identity = os.environ.get('GRAVELLE_CODESIGN_IDENTITY') or None
analysis = Analysis(
    [str(root / 'desktop' / 'macos_entry.py')],
    pathex=[str(root)], binaries=pdfium_binaries,
    datas=[
        (str(root / 'static'), 'static'),
        (str(resources / 'release.json'), '.'),
        (str(resources / 'desktop' / 'release.json'), 'desktop'),
        (str(resources / 'desktop' / 'assets' / 'gravelle-wordmark.png'), 'desktop/assets'),
        (str(root / 'LICENSE'), '.'),
        (str(resources / 'THIRD_PARTY_NOTICES.md'), '.'),
        (str(resources / 'licenses'), 'licenses'),
    ] + pdfium_data,
    hiddenimports=[
        'server', 'accounts', 'password_reset', 'transcoding', 'image_search',
        'image_search_features', 'task_scheduler', 'library_covers', 'cache_manager',
        'epub_reader', 'pdf_reader', 'pypdfium2', 'pypdfium2_raw', 'pypdfium2_cfg',
        'sharing', 'share_crypto', 'share_passwords', 'cryptography.hazmat.primitives.ciphers.aead',
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

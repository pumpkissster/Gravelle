# The WPF launcher uses the bundled console companion over redirected pipes.
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
resources = Path(os.environ.get('GRAVELLE_BUILD_RESOURCES', root))
analysis = Analysis(
    [str(root / 'desktop' / 'launcher.py')],
    pathex=[str(root)],
    binaries=pdfium_binaries,
    datas=[
        (str(root / 'static'), 'static'),
        (str(root / 'models' / 'yunet'), 'models/yunet'),
        (str(resources / 'release.json'), '.'),
        (str(resources / 'desktop' / 'release.json'), 'desktop'),
        (str(resources / 'desktop' / 'version.txt'), 'desktop'),
        (str(root / 'desktop' / 'assets' / 'gravelle-wordmark.png'), 'desktop/assets'),
    ] + pdfium_data,
    hiddenimports=['server', 'network_settings', 'https_settings', 'ssl', 'transcoding', 'image_search', 'image_search_features', 'avatar_face_crop', 'task_scheduler', 'library_covers', 'cache_manager', 'page_cache', 'epub_reader', 'pdf_reader', 'pypdfium2', 'pypdfium2_raw', 'pypdfium2_cfg', 'sharing', 'share_crypto', 'share_passwords', 'cryptography.hazmat.primitives.ciphers.aead', 'cryptography.hazmat.primitives.serialization.pkcs12', 'desktop.runtime', 'desktop.platforms', 'PIL.Image', 'cv2', 'numpy',
                   'watchdog.observers.read_directory_changes'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['resvg_py'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
common = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, icon=str(root / 'desktop' / 'gravelle.ico'))
service = EXE(pyz, analysis.scripts, [], name='GravelleServer', console=True, **common)
bundle = COLLECT(service, analysis.binaries, analysis.datas,
                 strip=False, upx=False, name='runtime')


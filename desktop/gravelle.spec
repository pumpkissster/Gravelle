# Two entry points share one runtime: the GUI has no console, while the hidden
# server process retains a real stdin pipe for graceful shutdown.
from pathlib import Path
import os

root = Path(SPECPATH).resolve().parent
resources = Path(os.environ.get('GRAVELLE_BUILD_RESOURCES', root))
analysis = Analysis(
    [str(root / 'desktop' / 'launcher.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / 'static'), 'static'),
        (str(resources / 'release.json'), '.'),
        (str(resources / 'desktop' / 'release.json'), 'desktop'),
        (str(root / 'desktop' / 'assets' / 'gravelle-wordmark.png'), 'desktop/assets'),
    ],
    hiddenimports=['server', 'transcoding', 'image_search', 'image_search_features', 'task_scheduler', 'library_covers', 'cache_manager', 'desktop.runtime', 'desktop.platforms', 'PIL.Image', 'cv2', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['resvg_py'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
common = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, icon=str(root / 'desktop' / 'gravelle.ico'))
launcher = EXE(pyz, analysis.scripts, [], name='Gravelle', console=False,
               manifest=str(root / 'desktop' / 'gravelle.manifest'), **common)
service = EXE(pyz, analysis.scripts, [], name='GravelleServer', console=True, **common)
bundle = COLLECT(launcher, service, analysis.binaries, analysis.datas,
                 strip=False, upx=False, name='Gravelle')

"""Build-only collection of the complete notices from the pinned PDFium wheel.

PDFium's component licenses are individual files (including .ijg files), not
necessarily filenames containing "license". Never replace them with a package
metadata license abbreviation.
"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path
import shutil

PDFIUM_PACKAGE_VERSION = '5.14.0'
REQUIRED_NOTICES = frozenset({
    'Apache-2.0.txt', 'BSD-3-Clause.txt', 'CC-BY-4.0.txt',
    'abseil.txt', 'agg23.txt', 'fast_float.txt', 'freetype.txt', 'icu.txt',
    'lcms.txt', 'libjpeg_turbo.ijg', 'libjpeg_turbo.md', 'libopenjpeg.txt',
    'libpng.txt', 'llvm-libc.txt', 'pdfium-binaries.txt', 'pdfium.txt',
    'simdutf.txt', 'zlib.txt',
})


def collect_pdfium_licenses(target: Path) -> list[Path]:
    """Copy every wheel notice, rejecting an incomplete or replaced wheel."""
    dist = importlib.metadata.distribution('pypdfium2')
    if dist.version != PDFIUM_PACKAGE_VERSION:
        raise RuntimeError('PDFium notices must be reviewed again when the pinned pypdfium2 version changes.')
    notices = []
    for entry in dist.files or ():
        relative = Path(entry)
        if relative.is_absolute() or '..' in relative.parts:
            continue
        if (len(relative.parts) < 3 or not relative.parts[0].endswith('.dist-info')
                or relative.parts[1] != 'licenses'):
            continue
        source = Path(dist.locate_file(entry))
        if source.is_symlink() or not source.is_file() or len(source.read_bytes().strip()) < 80:
            raise RuntimeError('A PDFium license notice is missing or incomplete: ' + relative.name)
        notices.append((source, relative))
    missing = REQUIRED_NOTICES - {relative.name for _, relative in notices}
    if missing:
        raise RuntimeError('PDFium license notices are missing: ' + ', '.join(sorted(missing)))
    destinations = []
    for source, relative in notices:
        destination = target / 'pypdfium2' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destinations.append(destination)
    return destinations

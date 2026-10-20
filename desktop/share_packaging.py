"""Build-only collection of complete cryptography dependency notices."""
from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
OPENSSL_LICENSE_SHA256 = "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a"


def crypto_version() -> str:
    return "48.0.1" if sys.platform == "darwin" and platform.machine() == "x86_64" else "50.0.2"


def collect_share_licenses(target: Path) -> list[Path]:
    """Keep the dual license text, not just its short LICENSE selector file."""
    result = []
    expected = crypto_version()
    for name, required in (("cryptography", {"LICENSE", "LICENSE.APACHE", "LICENSE.BSD"}),
                           ("cffi", {"LICENSE"}), ("pycparser", {"LICENSE"})):
        dist = importlib.metadata.distribution(name)
        if name == "cryptography" and dist.version != expected:
            raise RuntimeError("Sharing crypto notices must be reviewed when its pinned version changes.")
        notices = []
        for entry in dist.files or ():
            relative = Path(entry)
            if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 3
                    or not relative.parts[0].endswith(".dist-info") or relative.parts[1] != "licenses"):
                continue
            source = Path(dist.locate_file(entry))
            if source.is_symlink() or not source.is_file() or len(source.read_bytes().strip()) < 80:
                raise RuntimeError("A sharing crypto license notice is missing or incomplete: " + relative.name)
            notices.append((source, relative))
        missing = required - {path.name for _, path in notices}
        if missing:
            raise RuntimeError("Sharing crypto license notices are missing for " + name + ": " + ", ".join(sorted(missing)))
        for source, relative in notices:
            destination = target / name / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            result.append(destination)
    openssl = "4.0.1" if expected == "48.0.1" else "4.0.2"
    notice = ROOT / "desktop" / "licenses" / ("OpenSSL-" + openssl + "-LICENSE.txt")
    if notice.is_symlink() or hashlib.sha256(notice.read_bytes()).hexdigest() != OPENSSL_LICENSE_SHA256:
        raise RuntimeError("The pinned OpenSSL notice failed SHA-256 verification.")
    destination = target / "cryptography" / notice.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(notice, destination)
    result.append(destination)
    return result

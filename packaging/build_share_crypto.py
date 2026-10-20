"""Rebuild the reviewed local AES-GCM browser fallback without a CDN.

Supply the esbuild 0.28.2 executable. Upstream input modules are committed
unchanged and checked against their reviewed hashes before building.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ["packaging/noble-gcm-entry.js", "--bundle", "--format=iife",
           "--global-name=GravelleNobleGCM", "--minify", "--target=es2020",
           "--legal-comments=inline", "--outfile=static/vendor/noble-gcm.js"]


def verify_inputs() -> dict:
    provenance = json.loads((ROOT / "static/vendor/noble-gcm-provenance.json").read_text(encoding="utf-8"))
    for name, expected in provenance["source_sha256"].items():
        path = ROOT / "packaging/vendor/noble-ciphers-2.4.0" / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Unreviewed or modified noble-ciphers source: " + name)
    return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--esbuild", required=True, help="Path to the official esbuild 0.28.2 executable.")
    arguments = parser.parse_args()
    provenance = verify_inputs()
    executable = str(Path(arguments.esbuild).resolve())
    if subprocess.check_output([executable, "--version"], text=True).strip() != provenance["build_tool"]["version"]:
        raise ValueError("Use the pinned esbuild version to reproduce this bundle.")
    subprocess.run([executable, *COMMAND], cwd=ROOT, check=True)
    actual = hashlib.sha256((ROOT / "static/vendor/noble-gcm.js").read_bytes()).hexdigest()
    if actual != provenance["bundle_sha256"]:
        raise ValueError("The browser crypto output differs from the reviewed bundle.")
    print("Verified the reproducible local AES-GCM browser bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

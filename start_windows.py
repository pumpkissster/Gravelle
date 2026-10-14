"""Run the reader locally on Windows without Docker."""

import argparse
import ipaddress
import os
from pathlib import Path
import runpy
import socket
import sys


def main():
    parser = argparse.ArgumentParser(description="Start Gravelle on Windows.")
    parser.add_argument("--lan", action="store_true", help="Allow access from other devices on your local network.")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        print("Python 3.12 or newer is required. Install it and run this file again.")
        return 1

    project = Path(__file__).resolve().parent
    server = project / "server.py"
    if not server.is_file():
        print("Cannot find server.py. Keep this launcher in the project folder.")
        return 1
    try:
        from PIL import Image  # noqa: F401
        from importlib.metadata import version
        version("numpy")
        version("opencv-python-headless")
    except (ImportError, ModuleNotFoundError):
        print("Install the image processing dependencies, then run this file again:")
        print('"' + sys.executable + '" -m pip install -r "' + str(project / "requirements.txt") + '"')
        return 1

    os.chdir(project)
    os.environ.update({
        "LIBRARY_PATH": str(project),
        "DATA_PATH": str(project / "data-windows"),
        "HOST": "0.0.0.0" if args.lan else "127.0.0.1",
        "PORT": "8080",
        "READER_PASSWORD": "",
        "SCAN_INTERVAL": "300",
    })
    print("Gravelle - Windows local test", flush=True)
    print("Open in your browser: http://127.0.0.1:8080", flush=True)
    if args.lan:
        print("LAN access enabled. Sign in with your existing account.", flush=True)
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
            for address in sorted(addresses):
                ip = ipaddress.ip_address(address)
                if ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
                    print(f"LAN address candidate: http://{address}:8080", flush=True)
        except OSError:
            pass
        print("Use ipconfig to find the IPv4 address of the adapter connected to your router.", flush=True)
    print("Keep this window open. Press Ctrl+C or close it to stop the reader.", flush=True)
    print("Library root:", project, flush=True)
    print("Local data:", project / "data-windows", flush=True)
    print("Create a photo library in the browser and choose your sample folder.", flush=True)
    print(flush=True)
    runpy.run_path(str(server), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Exercise the actual frozen server using disposable data and random ports.

Example: python desktop/smoke_windows.py --executable
dist/windows/portable/Gravelle/GravelleServer.exe --work-root work/windows-smoke
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import json
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.core import LauncherConfig, check_port
from desktop.runtime import ServerController


def sample_png(color=(234, 80, 152)) -> bytes:
    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data +
                struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff))
    # Two-by-two pink/white pixels; stdlib-only so the smoke driver does not
    # depend on any image codecs beyond the application's own bundled Pillow.
    pixels = b"\x00" + bytes(color) + b"\xff\xff\xff\x00\xff\xff\xff" + bytes(color)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack("!2I5B", 2, 2, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def random_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(self, path: str, body=None):
        headers = {"Origin": self.url}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=payload, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(8 * 1024 * 1024)
            content_type = response.headers.get_content_type()
            value = json.loads(raw) if content_type == "application/json" else raw
            return response.status, value, content_type


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_smoke(executable: Path, work_root: Path) -> dict:
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "executable": str(executable), "work_root": str(work_root), "checks": []}
    started = time.monotonic()
    controller = None
    log_path = None

    def passed(name, **details):
        report["checks"].append({"name": name, "status": "passed", **details})

    with tempfile.TemporaryDirectory(prefix="gravelle-frozen-smoke-", dir=work_root) as temporary:
        test_root = Path(temporary).resolve()
        require(test_root.parent == work_root and test_root.name.startswith("gravelle-frozen-smoke-"),
                "The temporary directory escaped the explicitly selected work root")
        media = test_root / "media"
        book_folder = media / "几何样例" / "2026-10-11 几何图形"
        book_folder.mkdir(parents=True)
        image = sample_png()
        source_image = book_folder / "001.png"
        source_image.write_bytes(image)
        source_digest = hashlib.sha256(image).hexdigest()
        second_media = test_root / "第二根目录"
        second_folder = second_media / "几何样例" / "2026-10-11 几何图形"
        second_folder.mkdir(parents=True)
        second_image = sample_png((80, 120, 160))
        second_source = second_folder / "001.png"
        second_source.write_bytes(second_image)
        data = test_root / "data"
        log_path = test_root / "server.log"
        config = LauncherConfig(port=random_port(), library_paths=[str(media), str(second_media)])
        report["port"] = config.port
        controller = ServerController(data, log_path, command_prefix=[str(executable)])
        try:
            url = controller.start(config, timeout=35)
            passed("private_ready_handshake", local_url=url)
            client = Client(url)
            status, health, _ = client.request("/api/health")
            require(status == 200 and health.get("status") == "ok" and health.get("version"),
                    "Frozen health endpoint did not return the reader version")
            report["version"] = health["version"]
            passed("health", version=health["version"])

            status, index, mime = client.request("/index.html")
            require(status == 200 and mime == "text/html" and b'id="check-updates"' in index,
                    "Frozen static/index.html is missing the update-check button")
            require(b"gravelle-wordmark.svg" in index and b"gravelle-icon.png" in index,
                    "Frozen HTML does not reference the supplied branding")
            passed("packaged_html_and_update_button")
            for route, expected_mime, signature in (("/gravelle-wordmark.svg", "image/svg+xml", b"<svg"),
                                                     ("/gravelle-icon.png", "image/png", b"\x89PNG\r\n\x1a\n")):
                status, content, mime = client.request(route)
                require(status == 200 and mime == expected_mime and signature in content[:1024],
                        f"Frozen branding resource failed: {route}")
                passed("packaged_asset", route=route, bytes=len(content))
            status, _, _ = client.request("/api/libraries")
            require(status == 401, "Library data must require authentication")
            passed("unauthenticated_libraries_401")

            credentials = {"username": "frozen-smoke", "password": "disposable-frozen-smoke-only"}
            status, account, _ = client.request("/api/register", credentials)
            require(status == 201 and account.get("authenticated"), "Disposable registration failed")
            status, _, _ = client.request("/api/logout", {})
            require(status == 200, "Disposable logout failed")
            status, account, _ = client.request("/api/login", credentials)
            require(status == 200 and account.get("authenticated"), "Disposable login failed")
            passed("registration_logout_login")

            status, roots, _ = client.request("/api/directories")
            require(status == 200 and roots.get("root_paths") == [str(media), str(second_media)],
                    "Frozen launcher did not pass both allowed roots")
            passed("multiple_allowed_roots")
            status, helper, _ = client.request("/batch-selection.js")
            require(status == 200 and b"GravelleBatchSelection" in helper,
                    "Frozen bundle is missing the batch-selection frontend module")
            passed("packaged_batch_selection_module")
            status, helper, _ = client.request("/page-router.js")
            require(status == 200 and b"GravellePageRouter" in helper,
                    "Frozen bundle is missing the page-router frontend module")
            passed("packaged_page_router_module")

            status, result, _ = client.request("/api/libraries", {
                "name": "冻结包几何样例", "paths": [str(media)], "media_type": "photo",
                "reading_mode": "single", "reading_direction": "ltr", "hide_date_prefix": True})
            require(status == 201 and result.get("library", {}).get("id"), "Disposable photo library creation failed")
            library_id = result["library"]["id"]
            deadline = time.monotonic() + 20
            listing = None
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(0.1)
            require(status == 200 and not listing.get("scanning") and len(listing.get("books", [])) == 1,
                    "Frozen scanner did not catalogue exactly one disposable book")
            book = listing["books"][0]
            require(book.get("page_count") == 1 and book.get("title") == "几何图形",
                    "Photo page counting or date-prefix processing failed")
            status, page, mime = client.request(f"/api/books/{book['id']}/pages/0")
            require(status == 200 and mime == "image/png" and page == image,
                    "Frozen reader did not serve the original geometric sample page")
            status, cover, mime = client.request(f"/api/books/{book['id']}/cover")
            require(status == 200 and mime == "image/jpeg" and cover.startswith(b"\xff\xd8"),
                    "Frozen Pillow did not generate a JPEG cover")
            require(hashlib.sha256(source_image.read_bytes()).hexdigest() == source_digest,
                    "The smoke scan modified its source image")
            passed("photo_library_scan_page_and_cover", books=1, pages=1)

            status, created, _ = client.request("/api/libraries", {
                "name": "冻结包第二根目录", "paths": [str(second_media)], "media_type": "photo"})
            require(status == 201, "Frozen service rejected a library in its second root")
            second_library_id = created["library"]["id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + second_library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(.1)
            require(status == 200 and not listing.get("scanning") and len(listing.get("books", [])) == 1,
                    "Frozen scanner failed in the second root")
            second_book = listing["books"][0]
            require(second_book["id"] != book["id"], "Same-named folders across roots aliased a book ID")
            status, page, _ = client.request(f"/api/books/{second_book['id']}/pages/0")
            require(status == 200 and page == second_image, "Frozen reader served the wrong root's page")
            passed("second_root_scan_and_original_page")

            model_ids = []
            for name in ("冻结演示模特甲", "冻结演示模特乙"):
                status, created, _ = client.request("/api/models", {"name": name})
                require(status == 201 and created.get("model", {}).get("id"), "Frozen model creation failed")
                model_ids.append(created["model"]["id"])
            ids = [book["id"], second_book["id"]]
            status, changed, _ = client.request("/api/books/batch", {
                "action": "models", "ids": ids, "model_ids": model_ids})
            require(status == 200 and changed.get("updated") == ids, "Frozen batch model replacement failed")
            for book_id in ids:
                status, detail, _ = client.request(f"/api/books/{book_id}")
                require(status == 200 and [item["id"] for item in detail["models"]] == model_ids,
                        "Frozen batch model associations were not persisted")
            require(source_image.read_bytes() == image and second_source.read_bytes() == second_image,
                    "Multi-root or model operations changed original files")
            passed("batch_model_replacement_across_roots")

            child = controller.process
            controller.stop()
            require(child.returncode == 0, f"stdin stop returned exit code {child.returncode}")
            require(check_port(config)["available"], "Service port was not released after stop")
            passed("stdin_stop_and_port_release", returncode=child.returncode)
            with closing(sqlite3.connect(data / "library.sqlite3")) as connection:
                require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                        "Disposable SQLite integrity check failed")
                require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                        "Disposable SQLite foreign key check failed")
                require(connection.execute("SELECT count(*) FROM owner_account").fetchone()[0] == 1,
                        "Disposable owner account was not persisted")
            passed("sqlite_integrity_and_persistence")

            controller.start(config, timeout=35)
            child = controller.process
            child.stdin.close()
            require(child.wait(timeout=20) == 0, "EOF on parent pipe did not gracefully stop the frozen server")
            controller.stop()
            require(check_port(config)["available"], "Service port was not released after parent pipe EOF")
            passed("stdin_eof_and_port_release", returncode=child.returncode)
            report["status"] = "passed"
        except Exception as error:
            report["status"] = "failed"
            report["error"] = str(error)
        finally:
            if controller is not None:
                try:
                    controller.stop()
                except Exception as error:
                    report["status"] = "failed"
                    report["cleanup_error"] = str(error)
            if report["status"] != "passed" and log_path and log_path.exists():
                report["server_log_tail"] = log_path.read_text(encoding="utf-8", errors="replace")[-16000:]
    report["temporary_data_removed"] = not test_root.exists()
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=Path("work/windows-smoke"))
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args(argv)
    executable = arguments.executable.resolve(strict=True)
    require(executable.is_file() and executable.name.casefold() == "gravelleserver.exe",
            "Select the packaged GravelleServer.exe companion, not the GUI or an arbitrary command")
    work_root = arguments.work_root.resolve()
    require(work_root != Path(work_root.anchor), "The smoke work root must not be a filesystem root")
    work_root.mkdir(parents=True, exist_ok=True)
    report_path = (arguments.report or work_root / "report.json").resolve()
    report = run_smoke(executable, work_root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
                      "report": str(report_path), "duration_seconds": report["duration_seconds"]},
                     ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
from io import BytesIO
import json
import math
from pathlib import Path
import random
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


def sample_png(color=(234, 80, 152), size=(2, 2)) -> bytes:
    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data +
                struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff))
    # Keep the original tiny-image checks independent of extra image codecs.
    width, height = size
    pixels = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def screenshot_and_crop_fixtures() -> tuple[bytes, bytes, bytes]:
    """A generated source and two queries that need real local-feature matching.

    Use Pillow, already an application dependency, only to make disposable test
    illustrations. Neither query contains a user photograph or downloaded image.
    The smoke driver deliberately does not import the application's search code.
    """
    from PIL import Image, ImageDraw, ImageFont

    randomizer = random.Random(701)
    width, height = 960, 1280
    source = Image.new("RGB", (width, height), (238, 229, 220))
    draw = ImageDraw.Draw(source)
    draw.polygon(((0, 0), (width * .56, 0), (width * .2, height), (0, height)),
                 fill=(45, 65, 91))
    draw.ellipse((width * .36, height * .08, width * 1.2, height * .83),
                 fill=(166, 83, 111))
    font = ImageFont.load_default(size=15)
    for row in range(16):
        for column in range(12):
            x = column * width // 12 + randomizer.randrange(5, 21)
            y = row * height // 16 + randomizer.randrange(5, 21)
            w, h = randomizer.randrange(32, 55), randomizer.randrange(30, 57)
            color = tuple(randomizer.randrange(20, 235) for _ in range(3))
            variant = randomizer.randrange(3)
            if variant == 0:
                draw.rectangle((x, y, x + w, y + h), fill=color, outline="white", width=2)
            elif variant == 1:
                draw.ellipse((x, y, x + w, y + h), fill=color, outline="black", width=3)
            else:
                draw.polygon(((x, y + h), (x + w // 3, y), (x + w, y + h // 3)),
                             fill=color, outline="white", width=2)
            draw.line((x + 4, y + h // 3, x + w - 4, y + h - 4),
                      fill=(250, 247, 235), width=randomizer.randrange(2, 5))
            draw.text((x, y + 5), f"{randomizer.randrange(1000):03}", font=font,
                      fill=(10, 10, 15), stroke_width=1, stroke_fill=(245, 245, 240))
            for _ in range(3):
                dx, dy = x + randomizer.randrange(w), y + randomizer.randrange(h)
                draw.line((dx, dy, dx + randomizer.randrange(5, 17),
                           dy + randomizer.randrange(4, 14)), fill="black", width=2)
    draw.text((32, height - 44), "Generated fixture 701", font=font,
              fill="white", stroke_width=2, stroke_fill="black")

    def encoded(image, format_name, quality=65):
        output = BytesIO()
        image.save(output, format_name, quality=quality)
        return output.getvalue()

    content = source.copy()
    content.thumbnail((660, 880), Image.Resampling.LANCZOS)
    screenshot = Image.new("RGB", (1000, 1120), (28, 27, 31))
    chrome = ImageDraw.Draw(screenshot)
    chrome.rectangle((0, 0, 1000, 42), fill=(244, 240, 243))
    chrome.rounded_rectangle((105, 7, 820, 35), radius=10, fill=(218, 215, 219))
    chrome.text((125, 14), "localhost / generated screenshot", fill=(60, 60, 65))
    chrome.rectangle((0, 42, 1000, 112), fill=(49, 45, 49))
    chrome.text((30, 70), "Fixture reader", fill=(240, 230, 237))
    screenshot.paste(content, ((1000 - content.width) // 2, 136))
    chrome.rounded_rectangle((72, 1054, 928, 1097), radius=14, fill=(72, 65, 72))
    chrome.line((118, 1075, 875, 1075), fill=(234, 80, 152), width=5)

    retained_area = .45
    crop_width, crop_height = round(width * math.sqrt(retained_area)), round(height * math.sqrt(retained_area))
    left, top = round((width - crop_width) * .22), round((height - crop_height) * .72)
    crop = source.crop((left, top, left + crop_width, top + crop_height))
    crop.thumbnail((900, 900), Image.Resampling.LANCZOS)
    return (encoded(source, "PNG"), encoded(screenshot, "JPEG", quality=68),
            encoded(crop, "JPEG", quality=57))


def random_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(self, path: str, body=None, *, image=None):
        headers = {"Origin": self.url}
        payload = None
        if image is not None:
            payload = image
            headers["Content-Type"] = "image/jpeg" if image.startswith(b"\xff\xd8") else "image/png"
        elif body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=payload, headers=headers)
        try:
            response = self.opener.open(request, timeout=65 if path == "/api/image-search/query" else 5)
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

            status, helper, _ = client.request("/transcoding-ui.js")
            require(status == 200 and b"GravelleTranscodingUI" in helper,
                    "Frozen bundle is missing the transcoding frontend module")
            passed("packaged_transcoding_module")
            tiny = sample_png((80, 100, 130), (1, 1))
            tiny_source = book_folder / "002.png"
            tiny_source.write_bytes(tiny)
            status, _, _ = client.request("/api/scan", {"library_id": library_id})
            require(status in (200, 202), "Disposable rescan failed")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(.1)
            require(listing["books"][0]["page_count"] == 2, "Frozen rescan missed the unmatched tiny page")
            status, snapshot, _ = client.request("/api/transcoding")
            require(status == 200 and snapshot["rules"]["enabled"] is False,
                    "Transcoding must stay disabled until the administrator enables it")
            rules = {**snapshot["rules"], "enabled": True, "file_size_mb": 0,
                     "max_source_pixels": 3, "max_source_side": 0,
                     "max_output_side": 512, "min_free_space_mb": 0}
            status, impact, _ = client.request("/api/transcoding/rules/preview", {"rules": rules})
            require(status == 200 and impact["totals"]["newly_matched"] == 2,
                    "Frozen rule preview must count individual matched pages")
            status, snapshot, _ = client.request("/api/transcoding/rules", {
                "rules": rules, "expected_revision": snapshot["revision"]})
            require(status == 200 and snapshot["summary"]["pending_books"] == 2,
                    "Frozen rule save did not create persistent pending records")
            status, reminder, _ = client.request("/api/transcoding/summary")
            require(status == 200 and reminder["summary"]["pending_books"] == 2
                    and len(reminder["pending_book_ids"]) == 2
                    and book["id"] in reminder["pending_book_ids"]
                    and isinstance(reminder["scanning"], bool)
                    and len(reminder["pending_records"]) == 2,
                    "Frozen reminder summary must report consistent pending IDs and count")
            passed("frozen_lightweight_optimization_reminders")
            require(client.request(f"/api/books/{book['id']}")[0] == 404,
                    "Undecided work must be excluded from ordinary detail routes")
            require(client.request(f"/api/transcoding/books/{book['id']}/pages/0")[1] == image,
                    "Administrator preview must retain the untouched source")
            passed("transcoding_rule_preview_and_pending_visibility")
            status, snapshot, _ = client.request("/api/transcoding/actions", {
                "ids": [book["id"]], "action": "transcode"})
            require(status == 200, "Frozen transcoding start failed")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, snapshot, _ = client.request("/api/transcoding")
                record = next(item for item in snapshot["books"] if item["id"] == book["id"])
                if record["status"] in ("ready", "failed", "paused"):
                    break
                time.sleep(.1)
            require(record["status"] == "ready", f"Frozen image worker failed: {record.get('error')}")
            require(record["matched_count"] == record["output_count"] == 1 and record["actual_bytes"] > 0,
                    "Frozen worker must only create the one matched page")
            status, optimized, mime = client.request(f"/api/books/{book['id']}/pages/0")
            require(status == 200 and mime == "image/webp" and optimized != image,
                    "Frozen Pillow/worker did not publish a WebP derivative")
            require(client.request(f"/api/books/{book['id']}/pages/1")[1] == tiny,
                    "Frozen worker changed an unmatched source page")
            require(source_image.read_bytes() == image and tiny_source.read_bytes() == tiny,
                    "Frozen transcoding changed source files")
            passed("frozen_isolated_transcode_worker", matched=1, outputs=record["output_count"],
                   actual_bytes=record["actual_bytes"])
            status, snapshot, _ = client.request("/api/transcoding/actions", {
                "ids": [second_book["id"]], "action": "original"})
            require(status == 200 and any(item["id"] == second_book["id"] and item["status"] == "original"
                                         for item in snapshot["books"]),
                    "Original choice must remain in the management list")
            require(client.request(f"/api/books/{second_book['id']}/pages/0")[1] == second_image,
                    "Frozen original choice did not serve original bytes")
            status, cleared, _ = client.request("/api/transcoding/cleanup", {})
            require(status == 200 and cleared["cleanup"]["removed_files"] == 0,
                    "Cleanup must preserve every active derivative")
            require(client.request(f"/api/books/{book['id']}/pages/0")[1] == optimized,
                    "Cleanup removed the active reading image")
            passed("frozen_original_choice_and_active_cache_protection")

            # The preceding optimization test intentionally matches every page
            # larger than three pixels. Choose original output before adding the
            # detailed source, so rescanning cannot start an extra transcode job.
            status, _, _ = client.request("/api/transcoding/actions", {
                "ids": [book["id"]], "action": "original"})
            require(status == 200, "Frozen original choice failed before adding the detailed page")
            detailed, screenshot_query, cropped_query = screenshot_and_crop_fixtures()
            detailed_source = book_folder / "003.png"
            detailed_source.write_bytes(detailed)
            detailed_digest = hashlib.sha256(detailed).hexdigest()
            status, _, _ = client.request("/api/scan", {"library_id": library_id})
            require(status in (200, 202), "Disposable detailed-page rescan failed")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, listing, _ = client.request("/api/library?library_id=" + library_id)
                if status == 200 and not listing.get("scanning"):
                    break
                time.sleep(.1)
            require(status == 200 and not listing.get("scanning"),
                    "Frozen rescan of the detailed image did not finish")
            status, detail, _ = client.request(f"/api/books/{book['id']}")
            require(status == 200 and detail.get("page_count") == 3,
                    "Frozen rescan missed the detailed third source page")

            status, helper, _ = client.request("/image-search-ui.js")
            require(status == 200 and b"GravelleImageSearchUI" in helper,
                    "Frozen bundle is missing the image-search module")
            require(client.request("/api/image-search/status")[1]["enabled"] is False,
                    "Image search must remain disabled until the administrator enables it")
            require(client.request("/api/image-search/settings", {"enabled": True})[0] == 200,
                    "Frozen image-search enable failed")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, search_status, _ = client.request("/api/image-search/status")
                summary = search_status.get("summary", {})
                if summary.get("total", 0) >= 3 and summary.get("pending") == 0 and not search_status["indexing"]:
                    break
                time.sleep(.1)
            require(summary.get("indexed") == summary.get("total") and summary.get("total", 0) >= 3,
                    f"Frozen image-search index worker failed: {search_status.get('error')}")
            status, matches, _ = client.request("/api/image-search/query", image=image)
            match = next((item for item in matches.get("results", [])
                          if item["book"]["id"] == book["id"] and item["page"] == 0), None)
            require(status == 200 and match and match["match"] == "exact",
                    "Frozen stdin image-query worker did not find the exact source page")
            require("source_root" not in match["book"] and "path" not in match["book"],
                    "Image-search results must not expose source directories")
            status, preview, mime = client.request(f"/api/image-search/thumbnail/{book['id']}/0")
            require(status == 200 and mime == "image/jpeg" and preview.startswith(b"\xff\xd8"),
                    "Frozen search thumbnail did not return a bounded preview")
            require(source_image.read_bytes() == image and tiny_source.read_bytes() == tiny
                    and hashlib.sha256(detailed_source.read_bytes()).hexdigest() == detailed_digest,
                    "Image indexing modified original files")
            passed("frozen_isolated_image_search_workers", indexed=summary["indexed"],
                   match_page=match["page"], preview_bytes=len(preview))

            for name, query, details in (
                    ("frozen_full_screenshot_geometry_match", screenshot_query,
                     {"browser_chrome": True, "query_format": "JPEG"}),
                    ("frozen_medium_crop_geometry_match", cropped_query,
                     {"retained_source_area": .45, "query_format": "JPEG", "jpeg_quality": 57})):
                status, matches, _ = client.request("/api/image-search/query", image=query)
                results = matches.get("results", [])
                require(status == 200 and results and results[0]["book"]["id"] == book["id"]
                        and results[0]["page"] == 2 and results[0]["match"] == "similar",
                        f"Frozen local-feature worker did not find the third source page: {name}")
                require("source_root" not in results[0]["book"] and "path" not in results[0]["book"],
                        "Screenshot/crop results must not expose source directories")
                require(source_image.read_bytes() == image and tiny_source.read_bytes() == tiny
                        and second_source.read_bytes() == second_image
                        and hashlib.sha256(detailed_source.read_bytes()).hexdigest() == detailed_digest,
                        "Screenshot/crop search modified an original source file")
                passed(name, match_page=results[0]["page"], query_bytes=len(query), **details)

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

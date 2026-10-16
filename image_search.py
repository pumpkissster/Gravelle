"""Local, permission-aware page fingerprints and an isolated image decoder.

Only catalogue-owned sources are indexed. Query uploads travel over the worker's
stdin and are never retained. Shared fingerprints and small previews are kept in
SQLite; account visibility is applied before scoring or limiting candidates.
"""
from __future__ import annotations

import argparse
import base64
import gc
from contextlib import closing, contextmanager, nullcontext
import hashlib
import heapq
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError
import image_search_features as local_features

ALGORITHM_VERSION = 1
LOCAL_VERSION = 1
MIB = 1024 * 1024
MAX_BYTES = 64 * MIB
MAX_QUERY_BYTES = MAX_BYTES
MAX_QUERY_PIXELS = 100_000_000
MAX_SOURCE_PIXELS = 250_000_000
MEMORY_MB = 1024
TIMEOUT_SECONDS = 60
PREVIEW_SIDE = 384
MAX_PREVIEW_BYTES = 192 * 1024
MAX_MATCH_CANDIDATES = 64
MAX_MATCH_INPUT = 4 * MIB
CATALOGUE_BATCH_SIZE = 64
INDEX_WORKER_PAGES = 32
INDEX_WORKER_LIFETIME = 300
INDEX_WORKER_IDLE = 10
INDEX_WORKER_RECYCLE_RSS = 512 * MIB
INDEX_PROTOCOL = 1
INDEX_MAX_REQUEST = 16 * 1024


class _UnsupportedImage(ValueError):
    def __init__(self, actual_format):
        self.actual_format = str(actual_format or "UNKNOWN")[:20]
        super().__init__(f"当前图片实际格式为 {self.actual_format}，图片索引暂不支持；支持 JPG、PNG 和 WebP。")


def _worker_error(error):
    message = str(error) if isinstance(error, (ValueError, MemoryError)) else "图片无法解码或内存不足，请使用有效的 JPG、PNG 或 WebP 图片。"
    result = {"ok": False, "error": message[:300], "failure_code": "decode_error"}
    if isinstance(error, _UnsupportedImage):
        result.update(actual_format=error.actual_format, failure_code="unsupported_format")
    return result


def _source_fingerprint(path, preview):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("图片任务输入不能是链接。")
    if not 0 < info.st_size <= MAX_BYTES:
        raise ValueError("图片文件为空或超过 64 MiB。")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("图片任务输入发生变化。")
        digest = hashlib.sha256()
        total = 0
        while chunk := source.read(MIB):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("图片文件超过 64 MiB。")
            digest.update(chunk)
        source.seek(0)
        result = _features(source, preview=preview, max_pixels=MAX_SOURCE_PIXELS)
    return {"ok": True, "digest": digest.hexdigest(), **result}


def _worker_peak_rss():
    """A recycle hint, not a replacement for the enforced address-space limit."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                    ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel.GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return 0
            return int(counters.PeakWorkingSetSize)
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * (1 if sys.platform == "darwin" else 1024)
    except (ImportError, OSError, AttributeError):
        return 0


def _serve_index_worker():
    """Private one-request-at-a-time protocol; no query uploads enter this worker."""
    local_features._native()  # Pay native import cost once, under the OS budget.
    started = time.monotonic()
    print(json.dumps({"ready": True, "protocol": INDEX_PROTOCOL}), flush=True)
    for count in range(1, INDEX_WORKER_PAGES + 1):
        raw = sys.stdin.buffer.readline(INDEX_MAX_REQUEST + 1)
        if not raw:
            return 0
        request_id = None
        try:
            if len(raw) > INDEX_MAX_REQUEST or not raw.endswith(b"\n"):
                raise ValueError("索引任务输入无效。")
            request = json.loads(raw)
            if (not isinstance(request, dict) or set(request) != {"id", "source", "preview"}
                    or type(request["id"]) is not int or request["id"] != count
                    or not isinstance(request["source"], str) or not 0 < len(request["source"]) < 4096
                    or type(request["preview"]) is not bool):
                raise ValueError("索引任务输入无效。")
            request_id = request["id"]
            result = _source_fingerprint(Path(request["source"]), request["preview"])
        except (Exception, MemoryError) as error:
            result = _worker_error(error)
        gc.collect()
        peak = _worker_peak_rss()
        retire = (count >= INDEX_WORKER_PAGES or time.monotonic() - started >= INDEX_WORKER_LIFETIME
                  or peak >= INDEX_WORKER_RECYCLE_RSS or not result["ok"])
        print(json.dumps({"id": request_id, "retire": retire, "peak_rss_bytes": peak, "result": result}, separators=(",", ":")), flush=True)
        del result
        if retire:
            return 0
    return 0


class _IndexWorkerSession:
    """Bounded pipe reader; the manager checks cancellation while awaiting a line."""
    def __init__(self, process):
        self.process = process
        self.responses = Queue(maxsize=2)
        self.count = 0
        self.started = time.monotonic()
        self.busy = True
        self.broken = False
        self.timer = None
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._read, name="image-index-pipe", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_PREVIEW_BYTES * 2 + 1)
                if not line or len(line) > MAX_PREVIEW_BYTES * 2 or not line.endswith(b"\n"):
                    self.responses.put(None, timeout=1)
                    return
                self.responses.put(line, timeout=1)
        except (OSError, ValueError, Full):
            try:
                self.responses.put_nowait(None)
            except Full:
                pass

    def close(self):
        if self.timer:
            self.timer.cancel()
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                pass
        self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()
        if self.reader is not threading.current_thread():
            self.reader.join(timeout=1)
        self.closed.set()


def _png_without_bad_exif(source):
    """Return a bounded decode-only copy after discarding only bad eXIf CRCs.

    Called solely after Pillow rejects the input. Every other chunk retains its
    original bytes and checksum, and must pass framing/order/CRC checks before
    the copy is decoded. No source bytes or source digest are changed.
    """
    import zlib

    source.seek(0)
    signature = source.read(8)
    if signature != b"\x89PNG\r\n\x1a\n":
        return None
    source.seek(0, os.SEEK_END)
    size = source.tell()
    if size > MAX_BYTES:
        raise ValueError("PNG 图片超过 64 MiB，无法建立索引。")
    source.seek(8)
    output = BytesIO()
    output.write(signature)
    seen_ihdr = seen_plte = seen_idat = seen_exif = False
    idat_closed = removed = False
    color_type = None
    try:
        for _ in range(20_000):
            header = source.read(8)
            if len(header) != 8:
                raise ValueError("PNG 图片结构不完整或无效。")
            length, kind = int.from_bytes(header[:4], "big"), header[4:]
            if (any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind)
                    or kind[2] & 32 or length > size - source.tell() - 4):
                raise ValueError("PNG 图片块边界无效。")
            if not seen_ihdr and kind != b"IHDR":
                raise ValueError("PNG 图片缺少有效的 IHDR。")
            if kind[0] & 32 == 0 and kind not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
                raise ValueError("PNG 图片包含不支持的关键数据块。")
            if kind == b"IHDR":
                if seen_ihdr or length != 13:
                    raise ValueError("PNG 图片的 IHDR 无效。")
                seen_ihdr = True
            elif kind == b"PLTE":
                if seen_plte or seen_idat or not 0 < length <= 768 or length % 3:
                    raise ValueError("PNG 图片的 PLTE 无效。")
                seen_plte = True
            elif kind == b"IDAT":
                if idat_closed or (color_type == 3 and not seen_plte):
                    raise ValueError("PNG 图片的 IDAT 顺序无效。")
                seen_idat = True
            elif kind == b"eXIf":
                if seen_exif or seen_idat:
                    raise ValueError("PNG 图片的 eXIf 顺序或数量无效。")
                seen_exif = True
            elif kind == b"IEND":
                if length != 0 or not seen_idat:
                    raise ValueError("PNG 图片缺少有效的 IEND。")
            if seen_idat and kind != b"IDAT":
                idat_closed = True
            checkpoint = output.tell()
            output.write(header)
            checksum = zlib.crc32(kind)
            remaining = length
            while remaining:
                chunk = source.read(min(MIB, remaining))
                if not chunk:
                    raise ValueError("PNG 图片数据不完整。")
                remaining -= len(chunk)
                checksum = zlib.crc32(chunk, checksum)
                output.write(chunk)
                if kind == b"IHDR":
                    color_type = chunk[9]
            stored = source.read(4)
            if len(stored) != 4:
                raise ValueError("PNG 图片校验值不完整。")
            if checksum & 0xffffffff != int.from_bytes(stored, "big"):
                if kind != b"eXIf":
                    raise ValueError("PNG 图片的数据校验失败。")
                # The failed checksum means its orientation and other metadata
                # are not trusted, even if a TIFF parser could read them.
                output.seek(checkpoint)
                output.truncate()
                removed = True
            else:
                output.write(stored)
            if kind == b"IEND":
                if source.tell() != size:
                    raise ValueError("PNG 图片的 IEND 后存在额外数据。")
                if removed:
                    output.seek(0)
                    return output
                output.close()
                return None
        raise ValueError("PNG 图片的数据块数量超过安全上限。")
    except BaseException:
        output.close()
        raise


@contextmanager
def _open_feature_image(source):
    normalized = None
    try:
        try:
            image = Image.open(source)
        except UnidentifiedImageError:
            normalized = _png_without_bad_exif(source)
            if normalized is None:
                raise
            image = Image.open(normalized)
        with image:
            yield image
    finally:
        if normalized is not None:
            normalized.close()


def _features(source, *, preview=False, max_pixels=MAX_QUERY_PIXELS):
    """Run only inside a bounded worker (or on tiny images in pure unit tests)."""
    with _open_feature_image(source) as original:
        width, height = original.size
        if width <= 0 or height <= 0 or width * height > max_pixels:
            raise ValueError("图片像素超过安全上限。")
        if getattr(original, "is_animated", False):
            raise ValueError("暂不支持动画图片，请选择静态 JPG、PNG 或 WebP 图片。")
        if original.format not in ("JPEG", "PNG", "WEBP"):
            raise _UnsupportedImage(original.format)
        # JPEG decoder reduction avoids allocating the full 69 MP source. PNG
        # and WebP still decode in the child under the enforced memory ceiling.
        if original.format == "JPEG":
            original.draft("RGB", (local_features.FEATURE_SIDE, local_features.FEATURE_SIDE))
        if original.width * original.height * 6 > MEMORY_MB * MIB * .85:
            raise MemoryError("图片解码所需内存超过安全预算。")
        orientation = original.getexif().get(274, 1)
        original.thumbnail((local_features.FEATURE_SIDE, local_features.FEATURE_SIDE), Image.Resampling.LANCZOS, reducing_gap=3)
        # Apply orientation after reduction to avoid a second full-size buffer.
        original.getexif()[274] = orientation
        normalized = ImageOps.exif_transpose(original)
        if normalized.mode in ("RGBA", "LA") or "transparency" in normalized.info:
            rgba = normalized.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
        else:
            rgb = normalized.convert("RGB")
        blob = local_features.extract(rgb)
        basic = rgb.copy()
        basic.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS, reducing_gap=3)
        gray = basic.convert("L")
        difference = list(gray.resize((9, 8), Image.Resampling.LANCZOS).getdata())
        dhash = 0
        for y in range(8):
            for x in range(8):
                dhash = (dhash << 1) | (difference[y * 9 + x] > difference[y * 9 + x + 1])
        pixels = list(gray.resize((32, 32), Image.Resampling.LANCZOS).getdata())
        cosines = [[math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)] for u in range(8)]
        horizontal = [[sum(pixels[y * 32 + x] * cosines[u][x] for x in range(32)) for u in range(8)] for y in range(32)]
        coefficients = [sum(horizontal[y][u] * cosines[v][y] for y in range(32)) for v in range(8) for u in range(8)]
        median = sorted(coefficients[1:])[31]
        phash = 0
        for value in coefficients:
            phash = (phash << 1) | (value > median)
        mean = [round(value) for value in ImageStat.Stat(basic.resize((8, 8))).mean]
        result = {"dhash": f"{dhash:016x}", "phash": f"{phash:016x}", "mean": mean,
                  "width": width, "height": height}
        result["local_features"] = base64.b64encode(blob).decode("ascii")
        result["local_sketch"] = base64.b64encode(local_features.sketch(blob)).decode("ascii")
        if preview:
            output = BytesIO()
            basic.save(output, "JPEG", quality=78, optimize=True)
            data = output.getvalue()
            if len(data) > MAX_PREVIEW_BYTES:
                raise ValueError("图片预览超过安全上限。")
            result["preview"] = base64.b64encode(data).decode("ascii")
        return result


def worker_main(argv=None):
    parser = argparse.ArgumentParser(description="Private Gravelle local image search worker")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--match", action="store_true")
    parser.add_argument("--serve-index", action="store_true")
    args = parser.parse_args(argv)
    try:
        from transcoding import _restrict_worker, _lower_background_priority
        # Native libraries must not create a BLAS/OpenMP thread pool per task.
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[name] = "1"
        if args.source or args.serve_index:
            _lower_background_priority()
        limit_handle = _restrict_worker(MEMORY_MB * MIB)
        if args.serve_index and (args.stdin or args.source or args.preview or args.match):
            raise ValueError("索引进程参数无效。")
        if args.match:
            if not args.stdin or args.source or args.preview:
                raise ValueError("图片匹配任务输入无效。")
            data = sys.stdin.buffer.read(MAX_MATCH_INPUT + 1)
            if not data or len(data) > MAX_MATCH_INPUT:
                raise ValueError("图片匹配任务超过安全上限。")
            payload = json.loads(data)
            query = base64.b64decode(payload["query"], validate=True)
            candidates = payload["candidates"]
            if not local_features.validate(query) or not isinstance(candidates, list) or len(candidates) > MAX_MATCH_CANDIDATES:
                raise ValueError("图片匹配任务输入无效。")
            matches = []
            for index, value in enumerate(candidates):
                blob = base64.b64decode(value, validate=True)
                if not local_features.validate(blob):
                    raise ValueError("图片匹配索引无效。")
                distance = local_features.match(query, blob)
                if distance is not None:
                    matches.append([index, distance])
            print(json.dumps({"ok": True, "matches": matches}, separators=(",", ":")))
            return 0
        Image.MAX_IMAGE_PIXELS = None  # Private child has its own pixel budget.
        if args.serve_index:
            if args.stdin or args.source or args.preview or args.match:
                raise ValueError("索引进程参数无效。")
            return _serve_index_worker()
        if args.stdin == bool(args.source):
            raise ValueError("图片任务输入无效。")
        if args.stdin:
            data = sys.stdin.buffer.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES:
                raise ValueError("图片文件为空或超过 64 MiB。")
            source = BytesIO(data)
            digest = hashlib.sha256(data).hexdigest()
        else:
            print(json.dumps(_source_fingerprint(args.source, args.preview), separators=(",", ":")))
            return 0
        with source:
            result = _features(source, preview=args.preview,
                               max_pixels=MAX_QUERY_PIXELS if args.stdin else MAX_SOURCE_PIXELS)
        print(json.dumps({"ok": True, "digest": digest, **result}, separators=(",", ":")))
        return 0
    except (Exception, MemoryError) as error:
        print(json.dumps(_worker_error(error), ensure_ascii=True))
        return 1


class ImageSearchManager:
    def __init__(self, library, error_type=None):
        self.library = library
        self.error_type = error_type
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.query_lock = threading.Lock()
        self.decoder_condition = threading.Condition()
        self.decoder_active = False
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.auto_start = True
        self._index_turn_due = None
        self.activity_lock = threading.Lock()
        now = time.time()
        self.activity = {"stage": "idle", "generation": None, "done": 0, "total": 0,
                         "startedAt": now, "lastProgressAt": now, "heartbeatAt": now}
        self._activity_logged_at = 0
        self._failed_generation = None
        self.processes = set()
        self._index_worker = None
        self.root = library.data / "image-search-work"
        self._initialize()

    @property
    def waiting_queries(self):
        # A request may still be reading SQLite in its cancellation check.
        # Report it waiting only after registration in the priority queue, so
        # an observed query cannot race a newly released background lease.
        return self.library.task_scheduler.waiting_count(priority=True, resource="image-search")

    def _error(self, message, status=400):
        if self.error_type is None:
            from server import ReaderError
            self.error_type = ReaderError
        return self.error_type(message, status)

    def _initialize(self):
        with closing(self.library.connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS is_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                    paused INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1, reconcile_pending INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS is_pages (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE ON UPDATE CASCADE,
                    page_key TEXT NOT NULL, page_index INTEGER NOT NULL, source_version TEXT NOT NULL,
                    book_signature TEXT NOT NULL, algorithm INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', digest TEXT NOT NULL DEFAULT '',
                    dhash TEXT NOT NULL DEFAULT '', phash TEXT NOT NULL DEFAULT '',
                    mean TEXT NOT NULL DEFAULT '[]', width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0, preview BLOB, error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL, PRIMARY KEY(book_id,page_key));
                CREATE INDEX IF NOT EXISTS is_pages_status ON is_pages(status,book_id,page_index);
            """)
            connection.execute("INSERT OR IGNORE INTO is_settings(id) VALUES(1)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(is_pages)")}
            for name, declaration in (("local_features", "BLOB"), ("local_sketch", "BLOB"),
                                      ("local_version", "INTEGER NOT NULL DEFAULT 0"),
                                      ("local_state", "TEXT NOT NULL DEFAULT 'pending'"),
                                      ("local_error", "TEXT NOT NULL DEFAULT ''"),
                                      ("failure_code", "TEXT NOT NULL DEFAULT ''"),
                                      ("actual_format", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE is_pages ADD COLUMN {name} {declaration}")
            connection.execute("UPDATE is_pages SET status='pending' WHERE status='running'")
            connection.execute("UPDATE is_pages SET local_state='pending' WHERE local_state='running' OR local_version<>?", (LOCAL_VERSION,))
            connection.execute("UPDATE is_pages SET status='pending' WHERE algorithm<>?", (ALGORITHM_VERSION,))
            connection.execute("UPDATE is_settings SET reconcile_pending=1")
        # Recover only our flat, private temporary jobs, and never follow links.
        if self.root.exists():
            try:
                root = self._work_directory()
                for candidate in root.iterdir():
                    info = candidate.lstat()
                    if not re.fullmatch(r"\.index-[a-z0-9_]{8}", candidate.name) or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    entries = list(candidate.iterdir())
                    if len(entries) == 1 and entries[0].name == "input":
                        child = entries[0].lstat()
                        if stat.S_ISREG(child.st_mode) and not stat.S_ISLNK(child.st_mode) and not getattr(child, "st_file_attributes", 0) & 0x400:
                            entries[0].unlink()
                            candidate.rmdir()
                    elif not entries:
                        candidate.rmdir()
            except Exception:
                # Optional indexing must not prevent opening an existing
                # reader when its private scratch directory needs repair.
                with closing(self.library.connect()) as connection, connection:
                    connection.execute("UPDATE is_settings SET error=?", ("图片索引临时目录无效，请检查服务端资料目录。",))

    def _settings(self, connection):
        return dict(connection.execute("SELECT * FROM is_settings WHERE id=1").fetchone())

    def _set_activity(self, stage, generation, *, done=0, total=0):
        """Keep maintenance progress in memory, without per-page SQLite writes."""
        now = time.time()
        done, total = max(0, int(done)), max(0, int(total))
        with self.activity_lock:
            old = self.activity
            if old["generation"] is not None and generation < old["generation"]:
                return
            changed = old["stage"] != stage or old["generation"] != generation
            advanced = changed or done > old["done"]
            self.activity = {"stage": stage, "generation": generation, "done": done, "total": total,
                             "startedAt": now if changed else old["startedAt"],
                             "lastProgressAt": now if advanced else old["lastProgressAt"],
                             "heartbeatAt": now}
            report = stage == "reconciling" and (changed or now - self._activity_logged_at >= 10
                                                  or (advanced and done == total))
            if report:
                self._activity_logged_at = now
        if report:
            logging.getLogger("sumi").info("Image index catalogue check: %d/%d", done, total)

    def _activity_snapshot(self, settings, summary):
        with self.activity_lock:
            activity = dict(self.activity)
        alive = bool(self.thread and self.thread.is_alive())
        if activity.pop("generation") != settings["generation"]:
            activity.update(stage="starting", done=0, total=0)
        if not settings["enabled"]:
            activity["stage"] = "disabled"
        elif settings["paused"]:
            activity["stage"] = "paused"
        elif self.stop_event.is_set() or self.library.stop_event.is_set():
            activity["stage"] = "stopping" if alive else "stopped"
        elif self.thread is not None and not alive:
            activity["stage"] = "stopped"
        elif activity["stage"] == "idle" and (summary["pending"] or settings["reconcile_pending"]):
            activity["stage"] = "queued"
        activity["workerAlive"] = alive
        activity["background"] = self.library.task_scheduler.snapshot_debug()
        return activity

    def _work_directory(self):
        self.root.mkdir(exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or self.root.resolve().parent != self.library.data.resolve():
            raise self._error("图片索引临时目录无效，请检查服务端资料目录。", 422)
        return self.root

    def start(self):
        with self.lock:
            if self.stop_event.is_set():
                return
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="local-image-search", daemon=True)
                self.thread.start()
        self.wake.set()

    def close(self):
        self.stop_event.set()
        try:
            self._stop_index_worker()
        except (OSError, subprocess.TimeoutExpired):
            logging.getLogger("sumi").error("Image index worker did not exit within its shutdown deadline")
        self.wake.set()
        self.library.task_scheduler.wake()
        with self.decoder_condition:
            self.decoder_condition.notify_all()
        with self.lock:
            processes = tuple(self.processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)

    def _loop(self):
        while not self.stop_event.is_set() and not self.library.stop_event.is_set():
            with self.activity_lock:
                attempt_generation = self.activity["generation"]
                self._failed_generation = None
            try:
                if self.run_once():
                    continue
            except Exception as error:
                # Failed error persistence must not silently kill indexing.
                # Log exception classes only; decoder/source paths stay private.
                logging.getLogger("sumi").error("Image index coordinator error (%s)", type(error).__name__)
                with self.activity_lock:
                    generation = self._failed_generation if self._failed_generation is not None else attempt_generation
                if generation is not None:
                    self._set_activity("error", generation)
                try:
                    with closing(self.library.connect()) as connection, connection:
                        connection.execute("UPDATE is_settings SET error=? WHERE generation=? AND enabled=1 AND paused=0",
                                           ("图片索引任务失败，正在重试，请检查服务端日志。", generation))
                except Exception as persistence_error:
                    logging.getLogger("sumi").error("Image index error could not be recorded (%s)", type(persistence_error).__name__)
            self.wake.wait(2)
            self.wake.clear()

    def reconcile(self):
        """Catalogue mutation hook: queue work without inspecting sources here."""
        with closing(self.library.connect()) as connection, connection:
            connection.execute("UPDATE is_settings SET reconcile_pending=1,revision=revision+1")
        self.wake.set()
        if self.auto_start:
            self.start()

    def configure(self, body):
        with self.lock, closing(self.library.connect()) as connection, connection:
            self.library.require_admin(connection)
            if not isinstance(body, dict) or set(body) - {"enabled", "action"} or not body:
                raise self._error("图片搜索设置无效。")
            if "enabled" in body and type(body["enabled"]) is not bool:
                raise self._error("图片搜索开关需要 true 或 false。")
            action = body.get("action")
            if "action" in body and (not isinstance(action, str) or action not in ("rebuild", "pause", "resume", "retry", "clear")):
                raise self._error("不支持的图片索引操作。")
            if "enabled" in body and action is not None:
                raise self._error("一次只能修改图片搜索开关或索引操作。")
            connection.execute("BEGIN IMMEDIATE")
            if "enabled" in body:
                connection.execute("UPDATE is_settings SET enabled=?,paused=0,reconcile_pending=1", (int(body["enabled"]),))
            elif action == "pause":
                connection.execute("UPDATE is_settings SET paused=1")
            elif action == "resume":
                connection.execute("UPDATE is_settings SET enabled=1,paused=0,reconcile_pending=1")
            elif action == "rebuild":
                connection.execute("DELETE FROM is_pages")
                connection.execute("UPDATE is_settings SET enabled=1,paused=0,reconcile_pending=1,error=''")
            elif action == "retry":
                connection.execute("""UPDATE is_pages SET status='pending',error='' WHERE status='failed' AND failure_code<>'unsupported_format'
                    AND error<>'请选择 JPG、PNG 或 WebP 图片。' AND local_error<>'请选择 JPG、PNG 或 WebP 图片。'""")
                connection.execute("""UPDATE is_pages SET local_state='pending',local_error='' WHERE local_state='failed' AND failure_code<>'unsupported_format'
                    AND error<>'请选择 JPG、PNG 或 WebP 图片。' AND local_error<>'请选择 JPG、PNG 或 WebP 图片。'""")
                connection.execute("UPDATE is_settings SET paused=0,error='',reconcile_pending=1")
            elif action == "clear":
                connection.execute("DELETE FROM is_pages")
                connection.execute("UPDATE is_settings SET enabled=0,paused=0,reconcile_pending=1,error=''")
            connection.execute("UPDATE is_pages SET status='pending' WHERE status='running'")
            connection.execute("UPDATE is_pages SET local_state='pending' WHERE local_state='running'")
            connection.execute("UPDATE is_settings SET generation=generation+1,revision=revision+1")
            settings = self._settings(connection)
        self._set_activity("disabled" if not settings["enabled"] else "paused" if settings["paused"] else "queued",
                           settings["generation"])
        self._stop_index_worker()
        if self.auto_start:
            self.start()
        self.wake.set()
        self.library.task_scheduler.wake()
        return self.snapshot()

    def _visible_counts(self, connection):
        access, arguments = self.library._access_sql(connection, visible=True)
        rows = connection.execute(f"""SELECT p.status,p.local_state,COUNT(*) AS n FROM is_pages p
            JOIN books b ON b.id=p.book_id JOIN media_libraries l ON l.id=b.library_id
            WHERE b.active=1 AND b.removed=0 AND {access} GROUP BY p.status,p.local_state""", arguments)
        return self._counts(rows)

    def _failure_format(self, row):
        actual = row["actual_format"]
        if actual or "请选择 JPG、PNG 或 WebP 图片" not in (row["error"] + row["local_error"]):
            return actual
        # Older indexes did not record actual format. Inspect only a bounded
        # signature for the old unsupported-format error; never decode on GET.
        try:
            with self.library.transcoding._open_source(row, row["page_key"]) as (source, version):
                if version != row["source_version"]:
                    return ""
                head = source.read(64)
            if head.startswith(b"BM"):
                return "BMP"
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return "GIF"
            if head[4:8] == b"ftyp" and b"avif" in head[8:64]:
                return "AVIF"
        except Exception:
            pass
        return ""

    def failures(self, page=1, page_size=20):
        if type(page) is not int or not 1 <= page <= 1_000_000 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise self._error("失败列表分页参数无效。")
        with closing(self.library.connect()) as connection:
            self.library.require_admin(connection)
            where = "b.active=1 AND b.removed=0 AND (p.status='failed' OR (p.status='ready' AND p.local_state='failed'))"
            total = connection.execute("SELECT COUNT(*) FROM is_pages p JOIN books b ON b.id=p.book_id WHERE " + where).fetchone()[0]
            rows = [dict(row) for row in connection.execute("""SELECT b.*,p.page_key,p.page_index,p.source_version,
                p.error,p.local_error,p.failure_code,p.actual_format,p.updated_at,COALESCE(NULLIF(m.title,''),b.title) AS display_title,
                l.name AS library_name FROM is_pages p JOIN books b ON b.id=p.book_id
                JOIN media_libraries l ON l.id=b.library_id LEFT JOIN book_metadata m ON m.book_id=b.id WHERE """ + where +
                " ORDER BY p.updated_at DESC,p.book_id,p.page_index LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size))]
        items = []
        for row in rows:
            actual = self._failure_format(row)
            unsupported = (row["failure_code"] == "unsupported_format" or actual in ("AVIF", "BMP", "GIF")
                           or "请选择 JPG、PNG 或 WebP 图片。" in (row["error"], row["local_error"]))
            reason = (f"当前图片实际格式为 {actual}，图片索引暂不支持；支持 JPG、PNG 和 WebP。" if unsupported and actual
                      else "当前图片格式暂不支持索引；支持 JPG、PNG 和 WebP。" if unsupported
                      else row["error"] or row["local_error"] or "图片索引失败，请重试或检查源图片。")
            items.append({"book_id": row["id"], "page_key": row["page_key"], "page": row["page_index"],
                          "book_title": row["display_title"], "library_name": row["library_name"],
                          "filename": Path(row["page_key"].replace("\\", "/")).name, "reason": reason[:300],
                          "actual_format": actual, "failure_code": "unsupported_format" if unsupported else row["failure_code"] or "decode_error",
                          "retryable": not unsupported, "updated_at": row["updated_at"]})
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def retry_failures(self, body):
        if not isinstance(body, dict) or set(body) != {"items"} or not isinstance(body["items"], list) or not 1 <= len(body["items"]) <= 100:
            raise self._error("请选择 1 至 100 张失败图片。")
        keys = set()
        for item in body["items"]:
            if (not isinstance(item, dict) or set(item) != {"book_id", "page_key"}
                    or not isinstance(item["book_id"], str) or not re.fullmatch(r"[a-f0-9]{24}", item["book_id"])
                    or not isinstance(item["page_key"], str) or not 0 < len(item["page_key"]) <= 4096):
                raise self._error("失败图片选择无效。")
            keys.add((item["book_id"], item["page_key"]))
        with closing(self.library.connect()) as connection:
            self.library.require_admin(connection)
            rows = []
            for book_id, key in keys:
                row = connection.execute("""SELECT b.*,p.page_key,p.source_version,p.failure_code,p.actual_format,p.error,p.local_error
                    FROM is_pages p JOIN books b ON b.id=p.book_id WHERE p.book_id=? AND p.page_key=?
                    AND b.active=1 AND b.removed=0 AND (p.status='failed' OR (p.status='ready' AND p.local_state='failed'))""", (book_id, key)).fetchone()
                if row:
                    rows.append(dict(row))
        formats = {(row["id"], row["page_key"]): self._failure_format(row) for row in rows}
        retried = 0
        with self.lock, closing(self.library.connect()) as connection, connection:
            self.library.require_admin(connection)
            connection.execute("BEGIN IMMEDIATE")
            settings = self._settings(connection)
            if not settings["enabled"]:
                raise self._error("请先启用图片索引。", 409)
            for row in rows:
                actual = formats[(row["id"], row["page_key"])]
                if (row["failure_code"] == "unsupported_format" or actual in ("AVIF", "BMP", "GIF")
                        or "请选择 JPG、PNG 或 WebP 图片。" in (row["error"], row["local_error"])):
                    continue
                cursor = connection.execute("""UPDATE is_pages SET status=CASE WHEN status='failed' THEN 'pending' ELSE status END,
                    local_state='pending',error='',local_error='',failure_code='',actual_format=''
                    WHERE book_id=? AND page_key=? AND source_version=? AND (status='failed' OR (status='ready' AND local_state='failed'))
                    AND EXISTS(SELECT 1 FROM books b WHERE b.id=is_pages.book_id AND b.active=1 AND b.removed=0)""",
                    (row["id"], row["page_key"], row["source_version"]))
                retried += cursor.rowcount
            if retried:
                connection.execute("UPDATE is_pages SET status='pending' WHERE status='running'")
                connection.execute("UPDATE is_pages SET local_state='pending' WHERE local_state='running'")
                connection.execute("UPDATE is_settings SET paused=0,error='',generation=generation+1,revision=revision+1,reconcile_pending=1")
                settings = self._settings(connection)
        if retried:
            self._stop_index_worker()
            self._set_activity("queued", settings["generation"])
            self.wake.set()
            self.library.task_scheduler.wake()
            if self.auto_start:
                self.start()
        return {"retried": retried, "skipped": len(keys) - retried, "snapshot": self.snapshot()}

    @staticmethod
    def _counts(rows):
        counts = {}
        for row in rows:
            state = row["status"] if row["status"] != "ready" else row["local_state"]
            counts[state] = counts.get(state, 0) + row["n"]
        return {"total": sum(counts.values()), "indexed": counts.get("ready", 0),
                "pending": counts.get("pending", 0) + counts.get("running", 0), "failed": counts.get("failed", 0)}

    def snapshot(self, user_id=None):
        actor_context = self.library.acting_as(user_id) if user_id is not None else nullcontext()
        with actor_context, closing(self.library.connect()) as connection:
            actor = self.library._actor(connection)
            settings = self._settings(connection)
            # Administrator maintenance counts include optimization-pending
            # works; ordinary availability is derived only from visible works.
            if actor["role"] == "admin":
                rows = connection.execute("""SELECT p.status,p.local_state,COUNT(*) AS n FROM is_pages p JOIN books b ON b.id=p.book_id
                    WHERE b.active=1 AND b.removed=0 GROUP BY p.status,p.local_state""")
                summary = self._counts(rows)
            else:
                summary = self._visible_counts(connection)
            visible = self._visible_counts(connection)
            access, arguments = self.library._access_sql(connection, visible=True)
            available = connection.execute(f"""SELECT 1 FROM is_pages p JOIN books b ON b.id=p.book_id
                WHERE p.status='ready' AND b.active=1 AND b.removed=0 AND {access} LIMIT 1""", arguments).fetchone()
            result = {"enabled": bool(settings["enabled"]), "available": bool(settings["enabled"] and available),
                      "indexing": bool(settings["enabled"] and not settings["paused"] and (visible["pending"] or settings["reconcile_pending"]))}
            if actor["role"] == "admin":
                result.update(paused=bool(settings["paused"]), summary=summary,
                              progress={"done": summary["indexed"] + summary["failed"], "total": summary["total"]},
                              revision=settings["revision"], error=settings["error"],
                              activity=self._activity_snapshot(settings, summary))
            return result

    def _current(self, generation, connection=None):
        if self.stop_event.is_set() or self.library.stop_event.is_set():
            return False
        if connection is None:
            with closing(self.library.connect()) as own:
                return self._current(generation, own)
        settings = self._settings(connection)
        return bool(settings["enabled"] and not settings["paused"] and settings["generation"] == generation)

    def _apply_catalogue_batch(self, connection, generation, checked):
        # Sources have already been opened and closed outside this short
        # transaction. A concurrent scan or account action must be rechecked
        # after reserving the writer before any old observations are applied.
        with self.lock, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._current(generation, connection):
                return False
            current_books = {}
            changed = False
            for book, index, key, version, error in checked:
                if book["id"] not in current_books:
                    current_books[book["id"]] = connection.execute(
                        "SELECT signature FROM books WHERE id=? AND active=1 AND removed=0",
                        (book["id"],)).fetchone()
                current_book = current_books[book["id"]]
                if not current_book or current_book["signature"] != book["signature"]:
                    changed = True
                    continue
                old = connection.execute("""SELECT source_version,algorithm,page_index,book_signature
                    FROM is_pages WHERE book_id=? AND page_key=?""", (book["id"], key)).fetchone()
                same = bool(old and old["source_version"] == version and old["algorithm"] == ALGORITHM_VERSION)
                if same:
                    if old["page_index"] != index or old["book_signature"] != book["signature"]:
                        connection.execute("UPDATE is_pages SET page_index=?,book_signature=? WHERE book_id=? AND page_key=?",
                                           (index, book["signature"], book["id"], key))
                else:
                    connection.execute("""INSERT INTO is_pages(book_id,page_key,page_index,source_version,book_signature,algorithm,status,error,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id,page_key) DO UPDATE SET
                        page_index=excluded.page_index,source_version=excluded.source_version,book_signature=excluded.book_signature,
                        algorithm=excluded.algorithm,status=excluded.status,error=excluded.error,digest='',dhash='',phash='',mean='[]',preview=NULL,
                        local_features=NULL,local_sketch=NULL,local_state='pending',local_version=0,local_error='',failure_code='',actual_format='',updated_at=excluded.updated_at""",
                        (book["id"], key, index, version, book["signature"], ALGORITHM_VERSION,
                         "failed" if error else "pending", error, time.time()))
            if changed:
                connection.execute("UPDATE is_settings SET reconcile_pending=1 WHERE generation=?", (generation,))
        return True

    def _remove_catalogue_batch(self, connection, generation, obsolete):
        with self.lock, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._current(generation, connection):
                return False
            current_keys = {}
            for book_id, page_key in obsolete:
                if book_id not in current_keys:
                    book = connection.execute("SELECT pages FROM books WHERE id=? AND active=1 AND removed=0",
                                              (book_id,)).fetchone()
                    current_keys[book_id] = set(json.loads(book["pages"])) if book else set()
                # The manifest may have changed after cleanup candidates were
                # collected. Never delete a newly restored or added page.
                if page_key not in current_keys[book_id]:
                    connection.execute("DELETE FROM is_pages WHERE book_id=? AND page_key=?", (book_id, page_key))
        return True

    def _sync_catalogue(self, generation):
        # Keep one connection for the pass, with no read transaction held while
        # inspecting files. This also avoids last-connection WAL checkpoints
        # and connection/schema setup for each individual page.
        with closing(self.library.connect()) as connection:
            with self.lock, connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self._current(generation, connection):
                    return
                # Clear first so a concurrent scan can request another pass
                # without its request being lost when this one completes.
                connection.execute("UPDATE is_settings SET reconcile_pending=0 WHERE generation=?", (generation,))
                books = [dict(row) for row in connection.execute("""SELECT id,source_root,path,format,pages,signature
                    FROM books WHERE active=1 AND removed=0 ORDER BY id""")]
            total = sum(len(json.loads(book["pages"])) for book in books)
            done = 0
            self._set_activity("reconciling", generation, done=done, total=total)
            checked = []
            for book in books:
                for index, key in enumerate(json.loads(book["pages"])):
                    if not self._current(generation, connection):
                        return
                    error = ""
                    try:
                        with self.library.transcoding._open_source(book, key) as (source, version):
                            pass
                    except Exception:
                        version = "unreadable"
                        error = "源图片无法读取，请重新扫描后重试。"
                    checked.append((book, index, key, version, error))
                    done += 1
                    self._set_activity("reconciling", generation, done=done, total=total)
                    if len(checked) >= CATALOGUE_BATCH_SIZE:
                        if not self._apply_catalogue_batch(connection, generation, checked):
                            return
                        checked.clear()
            if checked and not self._apply_catalogue_batch(connection, generation, checked):
                return
            if not self._current(generation, connection):
                return
            live = {row["id"]: set(json.loads(row["pages"])) for row in connection.execute(
                "SELECT id,pages FROM books WHERE active=1 AND removed=0")}
            obsolete = [(row["book_id"], row["page_key"]) for row in connection.execute(
                "SELECT book_id,page_key FROM is_pages") if row["page_key"] not in live.get(row["book_id"], set())]
            for start in range(0, len(obsolete), CATALOGUE_BATCH_SIZE):
                if not self._remove_catalogue_batch(connection, generation, obsolete[start:start + CATALOGUE_BATCH_SIZE]):
                    return
            with self.lock, connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self._current(generation, connection):
                    return
                connection.execute("UPDATE is_settings SET revision=revision+1,error=''")

    def run_once(self):
        """One coordination pass or one page; useful for deterministic tests."""
        if not self.run_lock.acquire(blocking=False):
            return False
        generation = None
        try:
            with self.lock:
                broken = self._index_worker if self._index_worker and self._index_worker.broken else None
            if broken:
                # Do not claim further pages while a failed child still needs
                # reaping. Retry cleanup on the coordination loop instead.
                self._stop_index_worker(broken)
            with closing(self.library.connect()) as connection:
                settings = self._settings(connection)
            generation = settings["generation"]
            if not self._current(settings["generation"]):
                self._stop_index_worker()
                self._set_activity("disabled" if not settings["enabled"] else "paused", generation)
                return False
            if settings["reconcile_pending"] and self._index_turn_due != settings["generation"]:
                self._sync_catalogue(settings["generation"])
                # A scan can request another pass while this one is still
                # checking sources. Give one page a turn before reconciling
                # again; otherwise a slow catalogue can starve decoding.
                self._index_turn_due = settings["generation"]
                self._set_activity("idle", generation)
                return True
            self._index_turn_due = None
            with self.lock, closing(self.library.connect()) as connection:
                job = connection.execute("""SELECT b.*,p.page_key,p.page_index,p.source_version,p.status AS index_status FROM is_pages p JOIN books b ON b.id=p.book_id
                    WHERE b.active=1 AND b.removed=0 AND (p.status='pending' OR (p.status='ready' AND p.local_state='pending'))
                    ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,b.id,p.page_index LIMIT 1""").fetchone()
                if not job:
                    self._stop_index_worker()
                    self._set_activity("idle", generation)
                    return bool(settings["reconcile_pending"])
                job = dict(job)
            # Keep the row pending while another family owns the shared budget.
            # The decoder resource also prevents queries and indexing from
            # reserving two global slots for the same single decoder.
            self._set_activity("waiting", generation)
            with self.library.task_scheduler.slot(resource="image-search", cancel=lambda: not self._current(settings["generation"])) as acquired:
                if not acquired:
                    return True
                with self.lock, closing(self.library.connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute("""SELECT p.status,p.local_state,p.source_version,b.signature FROM is_pages p JOIN books b ON b.id=p.book_id
                        WHERE p.book_id=? AND p.page_key=? AND b.active=1 AND b.removed=0""", (job["id"], job["page_key"])).fetchone()
                    if (not self._current(settings["generation"]) or not row or row["status"] != job["index_status"]
                            or (row["status"] == "ready" and row["local_state"] != "pending")
                            or row["source_version"] != job["source_version"] or row["signature"] != job["signature"]):
                        return True
                    connection.execute("UPDATE is_pages SET status=CASE WHEN status='ready' THEN 'ready' ELSE 'running' END,local_state='running' WHERE book_id=? AND page_key=?", (job["id"], job["page_key"]))
                result = self._run_index_job(job, settings["generation"])
                self._set_activity("idle", generation)
                return result
        except Exception:
            # Keep the failing attempt's generation even if configure() has
            # already published a later pause/clear/resume while it unwinds.
            with self.activity_lock:
                self._failed_generation = generation
            raise
        finally:
            self.run_lock.release()

    def _run_index_job(self, job, generation):
        try:
            result = self._index_page(job, generation)
            with self.lock, closing(self.library.connect()) as connection, connection:
                if not self._job_current(connection, job, generation):
                    return True
                connection.execute("""UPDATE is_pages SET status='ready',digest=?,dhash=?,phash=?,mean=?,width=?,height=?,preview=?,error='',updated_at=?,
                    local_features=?,local_sketch=?,local_version=?,local_state='ready',local_error='',failure_code='',actual_format=''
                    WHERE book_id=? AND page_key=?""", (result["digest"], result["dhash"], result["phash"], json.dumps(result["mean"]),
                    result["width"], result["height"], result["preview"], time.time(), result.get("local_features"), result.get("local_sketch"),
                    LOCAL_VERSION, job["id"], job["page_key"]))
                connection.execute("UPDATE is_settings SET revision=revision+1,error=''")
        except Exception as error:
            with self.lock, closing(self.library.connect()) as connection, connection:
                if self._job_current(connection, job, generation):
                    message = str(error) if isinstance(error, self._error("x").__class__) else "图片索引失败，请重试或检查源图片。"
                    connection.execute("""UPDATE is_pages SET status=CASE WHEN status='ready' THEN 'ready' ELSE 'failed' END,error=?,
                        local_state='failed',local_version=?,local_error=?,failure_code=?,actual_format=?,updated_at=? WHERE book_id=? AND page_key=?""",
                                       (message[:300], LOCAL_VERSION, message[:300], getattr(error, "failure_code", "decode_error"),
                                        getattr(error, "actual_format", ""), time.time(), job["id"], job["page_key"]))
                    connection.execute("UPDATE is_settings SET revision=revision+1,error=?", (message[:300],))
                else:
                    connection.execute("UPDATE is_pages SET status='pending' WHERE book_id=? AND page_key=? AND status='running'",
                                       (job["id"], job["page_key"]))
                    connection.execute("UPDATE is_pages SET local_state='pending' WHERE book_id=? AND page_key=? AND local_state='running'", (job["id"], job["page_key"]))
        return True

    def _job_current(self, connection, job, generation):
        if not self._current(generation):
            return False
        row = connection.execute("""SELECT b.pages,b.signature,p.source_version,p.status,p.local_state FROM is_pages p JOIN books b ON b.id=p.book_id
            WHERE p.book_id=? AND p.page_key=? AND b.active=1 AND b.removed=0""", (job["id"], job["page_key"])).fetchone()
        return bool(row and row["signature"] == job["signature"] and row["source_version"] == job["source_version"]
                    and row["status"] in ("running", "ready") and row["local_state"] == "running" and job["page_key"] in json.loads(row["pages"]))

    def _index_page(self, job, generation):
        self._set_activity("reading", generation)
        with tempfile.TemporaryDirectory(prefix=".index-", dir=self._work_directory()) as temporary:
            source_path = Path(temporary) / "input"
            with self.library.transcoding._open_source(job, job["page_key"]) as (source, version):
                if version != job["source_version"]:
                    self.reconcile()
                    raise self._error("源图片已变化，请重新扫描后重试。", 422)
                # Reuse one autocommit connection for cancellation checks;
                # every chunk still observes newly committed account actions.
                with source_path.open("xb") as sink, closing(self.library.connect()) as connection:
                    total = 0
                    while chunk := source.read(MIB):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise self._error("图片超过 64 MiB，无法建立索引。", 422)
                        if not self._current(generation, connection):
                            raise self._error("索引任务已暂停或取消。", 409)
                        sink.write(chunk)
                        self._set_activity("reading", generation, done=total)
            self._set_activity("decoding", generation)
            result = self._decode(source_path=source_path, preview=True, generation=generation, reusable=True)
            with self.library.transcoding._open_source(job, job["page_key"]) as (source, version):
                if version != job["source_version"]:
                    self.reconcile()
                    raise self._error("源图片在建立索引时发生变化，请重试。", 422)
            return result

    def _stop_index_worker(self, expected=None, *, idle=False):
        with self.lock:
            session = self._index_worker
            if session is None or (expected is not None and session is not expected) or (idle and session.busy):
                return
            self._index_worker = None
        try:
            session.close()
        except BaseException:
            if session.process.poll() is None:
                session.broken = True
                with self.lock:
                    if self._index_worker is None:
                        self._index_worker = session
            raise
        finally:
            if session.process.poll() is not None:
                with self.lock:
                    self.processes.discard(session.process)

    def _receive_index(self, session, deadline, generation, connection):
        while True:
            if not self._current(generation, connection):
                raise self._error("索引任务已暂停或取消。", 409)
            if time.monotonic() >= deadline:
                raise self._error("图片索引超时，请重试或缩小源图片。", 422)
            try:
                line = session.responses.get(timeout=.1)
                break
            except Empty:
                continue
        if line is None:
            from transcoding import _worker_exit_message
            returncode = session.process.poll()
            if returncode is None:
                # EOF can precede process reaping by a few milliseconds.
                try:
                    returncode = session.process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    pass
            resource_error = _worker_exit_message(returncode)
            if resource_error:
                raise self._error(resource_error, 422)
            raise self._error("图片识别进程失败或内存不足，请缩小图片后重试。", 422)
        try:
            result = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise self._error("图片识别结果无效。", 422)
        if not isinstance(result, dict):
            raise self._error("图片识别结果无效。", 422)
        return result

    def _decode_reusable_index(self, source_path, preview, generation):
        deadline = time.monotonic() + TIMEOUT_SECONDS
        with closing(self.library.connect()) as connection:
            try:
                with self.lock:
                    session = self._index_worker
                    fresh = session is None
                    if not fresh:
                        if session.timer:
                            session.timer.cancel()
                        session.busy = True
                if session and (session.process.poll() is not None or session.count >= INDEX_WORKER_PAGES
                                or time.monotonic() - session.started >= INDEX_WORKER_LIFETIME):
                    self._stop_index_worker(session)
                    session = None
                    fresh = True
                if session is None:
                    if not self._current(generation, connection):
                        raise self._error("索引任务已暂停或取消。", 409)
                    prefix = [sys.executable, "--image-search-worker"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve()), "--worker"]
                    process = subprocess.Popen([*prefix, "--serve-index"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, close_fds=True,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    session = _IndexWorkerSession(process)
                    with self.lock:
                        self._index_worker = session
                        self.processes.add(process)
                if fresh:
                    ready = self._receive_index(session, deadline, generation, connection)
                    if (set(ready) != {"ready", "protocol"} or ready["ready"] is not True
                            or type(ready["protocol"]) is not int or ready["protocol"] != INDEX_PROTOCOL):
                        raise self._error("图片索引进程启动失败，请查看服务端运行环境。", 422)
                if not self._current(generation, connection):
                    raise self._error("索引任务已暂停或取消。", 409)
                session.count += 1
                request = json.dumps({"id": session.count, "source": str(source_path), "preview": bool(preview)}, separators=(",", ":")).encode() + b"\n"
                if len(request) > INDEX_MAX_REQUEST:
                    raise self._error("图片索引任务路径过长。", 422)
                session.process.stdin.write(request)
                session.process.stdin.flush()
                response = self._receive_index(session, deadline, generation, connection)
                if (set(response) != {"id", "retire", "peak_rss_bytes", "result"} or type(response["id"]) is not int or response["id"] != session.count
                        or type(response["retire"]) is not bool or type(response["peak_rss_bytes"]) is not int
                        or not 0 <= response["peak_rss_bytes"] <= MEMORY_MB * MIB * 2):
                    raise self._error("图片识别结果无效。", 422)
                result = self._parse_worker_result(json.dumps(response["result"]).encode(), preview=preview, strict=True)
                if (response["retire"] or response["peak_rss_bytes"] >= INDEX_WORKER_RECYCLE_RSS
                        or session.count >= INDEX_WORKER_PAGES or time.monotonic() - session.started >= INDEX_WORKER_LIFETIME):
                    self._stop_index_worker(session)
                else:
                    with self.lock:
                        if self._index_worker is session:
                            session.busy = False
                            session.timer = threading.Timer(INDEX_WORKER_IDLE, lambda: self._stop_index_worker(session, idle=True))
                            session.timer.daemon = True
                            session.timer.start()
                return result
            except BaseException as error:
                if not self._index_worker or not self._index_worker.broken:
                    self._stop_index_worker()
                if isinstance(error, (BrokenPipeError, OSError, ValueError)):
                    raise self._error("图片识别进程失败或内存不足，请重试。", 422) from error
                raise

    def _decode(self, data=None, *, source_path=None, preview=False, generation=None, reusable=False):
        query = data is not None
        active = False
        def cancelled():
            if self.stop_event.is_set() or self.library.stop_event.is_set():
                return True
            if not query:
                return not self._current(generation)
            with closing(self.library.connect()) as connection:
                return not self._settings(connection)["enabled"]
        try:
            with self.library.task_scheduler.slot(resource="image-search", priority=query, cancel=cancelled) as acquired:
                if not acquired:
                    raise self._error("图片任务已暂停或取消。", 409)
                with self.decoder_condition:
                    while self.decoder_active:
                        if cancelled():
                            raise self._error("图片任务已暂停或取消。", 409)
                        self.decoder_condition.wait(.1)
                    self.decoder_active = True
                    active = True
                if query:
                    self._stop_index_worker()  # Free the idle batch before a foreground query.
                return self._decode_exclusive(data, source_path=source_path, preview=preview, generation=generation, reusable=reusable)
        finally:
            with self.decoder_condition:
                if active:
                    self.decoder_active = False
                self.decoder_condition.notify_all()

    def _decode_exclusive(self, data=None, *, source_path=None, preview=False, generation=None, matching=False, reusable=False):
        if reusable and source_path is not None and data is None and generation is not None and not matching:
            return self._decode_reusable_index(source_path, preview, generation)
        prefix = [sys.executable, "--image-search-worker"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve()), "--worker"]
        command = [*prefix, "--stdin"] if data is not None else [*prefix, "--source", str(source_path)]
        if preview:
            command.append("--preview")
        if matching:
            command.append("--match")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # stdout is small bounded metadata. stderr is discarded, never logged
        # with private paths, upload bytes or decoder-dependent exceptions.
        process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        with self.lock:
            self.processes.add(process)
            # close() may have taken its process snapshot between Popen and
            # registration; a query must not then wait its full decode timeout.
            if self.stop_event.is_set() or self.library.stop_event.is_set():
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            if data is not None:
                try:
                    output, _ = process.communicate(data, timeout=TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise self._error("图片识别超时，请缩小图片后重试。", 422)
            else:
                # SELECT checks do not retain a transaction across polls, so
                # pause/clear remain visible without reconnecting at 10 Hz.
                with closing(self.library.connect()) as connection:
                    deadline = time.monotonic() + TIMEOUT_SECONDS
                    while process.poll() is None:
                        if not self._current(generation, connection):
                            process.kill()
                            raise self._error("索引任务已暂停或取消。", 409)
                        if time.monotonic() > deadline:
                            process.kill()
                            raise self._error("图片索引超时，请重试或缩小源图片。", 422)
                        # Drain through communicate to avoid a full stdout pipe
                        # blocking a valid preview response before process exit.
                        try:
                            output, _ = process.communicate(timeout=.1)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    else:
                        output, _ = process.communicate()
            return self._parse_worker_result(output, preview=preview, matching=matching, returncode=process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for pipe in (getattr(process, "stdin", None), getattr(process, "stdout", None)):
                if pipe:
                    pipe.close()
            with self.lock:
                self.processes.discard(process)

    def _parse_worker_result(self, output, *, preview=False, matching=False, returncode=0, strict=False):
        from transcoding import _worker_exit_message
        resource_error = _worker_exit_message(returncode)
        if resource_error:
            raise self._error(resource_error, 422)
        if len(output) > MAX_PREVIEW_BYTES * 2:
            raise self._error("图片识别结果无效。", 422)
        try:
            result = json.loads(output)
        except (ValueError, UnicodeDecodeError):
            raise self._error("图片识别进程失败或内存不足，请缩小图片后重试。", 422)
        if not isinstance(result, dict):
            raise self._error("图片识别结果无效。", 422)
        if strict and type(result.get("ok")) is not bool:
            raise self._error("图片识别结果无效。", 422)
        if returncode or not result.get("ok"):
            error = self._error(str(result.get("error", "图片识别失败。"))[:300], 422)
            code = result.get("failure_code", "decode_error")
            actual = result.get("actual_format", "")
            error.failure_code = code if code in ("unsupported_format", "decode_error") else "decode_error"
            error.actual_format = actual if isinstance(actual, str) and re.fullmatch(r"[A-Z0-9_-]{1,20}", actual) else ""
            raise error
        if strict:
            if (any(type(result.get(name)) is not int or result[name] <= 0 for name in ("width", "height"))
                    or result["width"] * result["height"] > MAX_SOURCE_PIXELS
                    or not isinstance(result.get("mean"), list) or len(result["mean"]) != 3
                    or any(type(value) is not int or not 0 <= value <= 255 for value in result["mean"])):
                raise self._error("图片识别尺寸或颜色结果无效。", 422)
        if matching:
            matches = result.get("matches")
            if (not isinstance(matches, list) or len(matches) > MAX_MATCH_CANDIDATES
                    or any(not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int
                           or not 0 <= item[0] < MAX_MATCH_CANDIDATES or type(item[1]) not in (int, float)
                           or not math.isfinite(item[1]) or not 0 <= item[1] <= 13 for item in matches)):
                raise self._error("图片匹配结果无效。", 422)
            if len({item[0] for item in matches}) != len(matches):
                raise self._error("图片匹配结果无效。", 422)
            return matches
        if not all(isinstance(result.get(key), str) and re.fullmatch(f"[0-9a-f]{{{length}}}", result[key]) for key, length in (("digest", 64), ("dhash", 16), ("phash", 16))):
            raise self._error("图片指纹无效。", 422)
        if preview:
            result["preview"] = base64.b64decode(result["preview"], validate=True)
            if not 0 < len(result["preview"]) <= MAX_PREVIEW_BYTES:
                raise self._error("图片预览无效。", 422)
        for name, validator in (("local_features", local_features.validate), ("local_sketch", local_features.validate_sketch)):
            try:
                blob = base64.b64decode(result[name], validate=True)
            except (ValueError, KeyError, TypeError):
                raise self._error("图片局部索引无效。", 422)
            if not validator(blob):
                raise self._error("图片局部索引无效。", 422)
            result[name] = blob
        if strict and local_features.sketch(result["local_features"]) != result["local_sketch"]:
            raise self._error("图片局部索引不一致。", 422)
        return result

    @staticmethod
    def _distance(feature, row):
        if feature["digest"] == row["digest"]:
            return 0, "exact"
        p = (int(feature["phash"], 16) ^ int(row["phash"], 16)).bit_count()
        d = (int(feature["dhash"], 16) ^ int(row["dhash"], 16)).bit_count()
        mean = json.loads(row["mean"])
        color = sum(abs(a - b) for a, b in zip(feature["mean"], mean)) / 3
        distance = round(p * .65 + d * .35, 2)
        if distance > 13 or color > 50:
            return None
        return distance, "similar"

    def _fresh(self, row):
        try:
            keys = json.loads(row["pages"])
            if row["page_index"] >= len(keys) or keys[row["page_index"]] != row["page_key"]:
                return False
            with self.library.transcoding._open_source(row, row["page_key"]) as (source, version):
                return version == row["source_version"]
        except Exception:
            return False

    def _crop_candidates(self, connection, feature, access, arguments, deadline=None):
        """Bounded, crop-aware retrieval; invisible pages never enter ranking.

        Compact bit sketches are streamed, rather than keeping every page's
        descriptors in RAM or creating millions of SQL postings. Global hashes
        are deliberately not a prefilter: a crop can change all of them.
        """
        sketch = feature.get("local_sketch")
        if not local_features.validate_sketch(sketch):
            return []
        rows = connection.execute(f"""SELECT p.book_id,p.page_key,p.page_index,p.source_version,p.local_sketch,
            b.source_root,b.path,b.format,b.pages FROM is_pages p JOIN books b ON b.id=p.book_id
            WHERE p.status='ready' AND p.algorithm=? AND p.local_version=? AND p.local_state='ready'
            AND b.active=1 AND b.removed=0 AND {access}""", [ALGORITHM_VERSION, LOCAL_VERSION, *arguments])
        heap = []
        for sequence, row in enumerate(rows):
            if sequence % 256 == 0:
                self._check_query_budget(deadline)
            if not local_features.validate_sketch(row["local_sketch"]):
                continue
            score = local_features.rank(sketch, row["local_sketch"])
            if not math.isfinite(score) or score <= 0:
                continue
            entry = (score, sequence, dict(row))
            if len(heap) < MAX_MATCH_CANDIDATES:
                heapq.heappush(heap, entry)
            elif score > heap[0][0]:
                heapq.heapreplace(heap, entry)
        candidates = []
        for _, _, row in sorted(heap, reverse=True):
            # Permissions/source metadata can change during a large sketch scan.
            current = connection.execute(f"""SELECT p.local_features FROM is_pages p JOIN books b ON b.id=p.book_id
                WHERE p.book_id=? AND p.page_key=? AND p.source_version=? AND p.status='ready'
                AND p.local_version=? AND p.local_state='ready' AND b.active=1 AND b.removed=0 AND {access}""",
                [row["book_id"], row["page_key"], row["source_version"], LOCAL_VERSION, *arguments]).fetchone()
            if current and local_features.validate(current["local_features"]):
                row.pop("local_sketch")
                row["local_features"] = bytes(current["local_features"])
                candidates.append(row)
        return candidates

    def _check_query_budget(self, deadline):
        if self.stop_event.is_set() or self.library.stop_event.is_set():
            raise self._error("图片任务已暂停或取消。", 409)
        if deadline is not None and time.monotonic() > deadline:
            raise self._error("图片搜索超时，请稍后重试。", 422)
        with closing(self.library.connect()) as connection:
            if not self._settings(connection)["enabled"]:
                raise self._error("管理员已关闭以图搜图。", 409)

    def _match_candidates(self, feature, candidates):
        if not candidates or not local_features.validate(feature.get("local_features")):
            return []
        payload = json.dumps({"query": base64.b64encode(feature["local_features"]).decode("ascii"),
                              "candidates": [base64.b64encode(row["local_features"]).decode("ascii") for row in candidates]},
                             separators=(",", ":")).encode("ascii")
        if len(payload) > MAX_MATCH_INPUT:
            raise self._error("图片匹配任务超过安全上限。", 422)
        matches = self._decode_exclusive(payload, matching=True)
        if any(index >= len(candidates) for index, distance in matches):
            raise self._error("图片匹配结果无效。", 422)
        return [(candidates[index], distance) for index, distance in matches]

    def query(self, image_bytes, user_id=None, limit=30):
        if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= MAX_BYTES:
            raise self._error("请选择不超过 64 MiB 的 JPG、PNG 或 WebP 图片。", 422)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise self._error("图片搜索结果数量无效。")
        actor_context = self.library.acting_as(user_id) if user_id is not None else nullcontext()
        with actor_context:
            with closing(self.library.connect()) as connection:
                self.library._actor(connection)
                if not self._settings(connection)["enabled"]:
                    raise self._error("管理员尚未开启以图搜图。", 409)
            if not self.query_lock.acquire(blocking=False):
                raise self._error("另一个图片搜索正在进行，请稍后重试。", 429)
            try:
                def cancelled():
                    if self.stop_event.is_set() or self.library.stop_event.is_set():
                        return True
                    with closing(self.library.connect()) as connection:
                        return not self._settings(connection)["enabled"]
                with self.library.task_scheduler.slot(resource="image-search", priority=True, cancel=cancelled) as acquired:
                    if not acquired:
                        raise self._error("图片任务已暂停或取消。", 409)
                    return self._query_locked(image_bytes, limit)
            finally:
                self.query_lock.release()

    def _query_locked(self, image_bytes, limit):
        deadline = time.monotonic() + TIMEOUT_SECONDS
        feature = self._decode(image_bytes)
        with closing(self.library.connect()) as connection:
            if not self._settings(connection)["enabled"]:
                raise self._error("管理员已关闭以图搜图。", 409)
            access, arguments = self.library._access_sql(connection, visible=True)
            # No LIMIT before authorization, matching or ranking. Hidden
            # pages never affect hit totals or consume result slots.
            rows = connection.execute(f"""SELECT p.book_id,p.page_key,p.page_index,p.source_version,p.digest,p.dhash,p.phash,p.mean,
                p.local_state,length(p.local_features) AS local_size,p.width,p.height,
                b.source_root,b.path,b.format,b.pages FROM is_pages p JOIN books b ON b.id=p.book_id
                JOIN media_libraries l ON l.id=b.library_id WHERE p.status='ready' AND p.algorithm=?
                AND b.active=1 AND b.removed=0 AND {access}""", [ALGORITHM_VERSION, *arguments])
            matches = []
            stale = False
            for sequence, row in enumerate(rows):
                if sequence % 256 == 0:
                    self._check_query_budget(deadline)
                match = self._distance(feature, row)
                if match is None:
                    continue
                # Once local evidence is available, a similar layout
                # alone is no longer proof that this is the same image.
                # Legacy and featureless pages retain the basic fallback.
                if (match[1] != "exact" and row["local_state"] == "ready"
                        and min(row["width"], row["height"], feature["width"], feature["height"]) >= 128
                        and (row["local_size"] or 0) >= 10 + local_features.MIN_FEATURES * 36
                        and len(feature.get("local_features", b"")) >= 10 + local_features.MIN_FEATURES * 36):
                    continue
                if not self._fresh(row):
                    stale = True
                    continue
                matches.append({"book_id": row["book_id"], "page": row["page_index"], "match": match[1], "distance": match[0]})
            candidates = self._crop_candidates(connection, feature, access, arguments, deadline)
        self._check_query_budget(deadline)
        present = {(item["book_id"], item["page"]) for item in matches}
        candidates = [row for row in candidates if (row["book_id"], row["page_index"]) not in present]
        for row, distance in self._match_candidates(feature, candidates):
            if not self._fresh(row):
                stale = True
                continue
            matches.append({"book_id": row["book_id"], "page": row["page_index"], "match": "similar", "distance": distance})
        if stale:
            self.reconcile()
        # A permission edit while decoding/scoring must also take
        # effect for direct internal callers, before totals and limit.
        with closing(self.library.connect()) as connection:
            if not self._settings(connection)["enabled"]:
                raise self._error("管理员已关闭以图搜图。", 409)
            access, arguments = self.library._access_sql(connection, visible=True)
            visible_ids = set()
            ids = list({item["book_id"] for item in matches})
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                marks = ",".join("?" for _ in batch)
                visible_ids.update(row[0] for row in connection.execute(
                    f"SELECT b.id FROM books b WHERE b.id IN ({marks}) AND b.active=1 AND b.removed=0 AND {access}", [*batch, *arguments]))
            matches = [item for item in matches if item["book_id"] in visible_ids]
        matches.sort(key=lambda item: (item["match"] != "exact", item["distance"], item["book_id"], item["page"]))
        status = self.snapshot()
        return {"results": matches[:limit], "total": len(matches), "indexing": status["indexing"]}

    def thumbnail(self, book_id, page, user_id=None):
        if type(page) is not int or page < 0:
            raise self._error("图片预览不存在。", 404)
        actor_context = self.library.acting_as(user_id) if user_id is not None else nullcontext()
        with actor_context, closing(self.library.connect()) as connection:
            access, arguments = self.library._access_sql(connection, visible=True)
            row = connection.execute(f"""SELECT p.*,b.source_root,b.path,b.format,b.pages FROM is_pages p JOIN books b ON b.id=p.book_id
                JOIN media_libraries l ON l.id=b.library_id WHERE p.book_id=? AND p.page_index=? AND p.status='ready'
                AND p.algorithm=? AND b.active=1 AND b.removed=0 AND {access}""", [book_id, page, ALGORITHM_VERSION, *arguments]).fetchone()
            if not self._settings(connection)["enabled"] or not row or not row["preview"]:
                raise self._error("图片预览不存在。", 404)
            if not self._fresh(row):
                self.reconcile()
                raise self._error("源图片已变化，请等待重新建立索引。", 404)
            # Recheck after filesystem inspection; permissions can change while
            # a network filesystem is slow.
            access, arguments = self.library._access_sql(connection, visible=True)
            if not connection.execute(f"SELECT 1 FROM books b WHERE b.id=? AND b.active=1 AND b.removed=0 AND {access}", [book_id, *arguments]).fetchone():
                raise self._error("图片预览不存在。", 404)
            data = bytes(row["preview"])
            return data, "image/jpeg", '"' + hashlib.sha256(data).hexdigest() + '"'


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--worker":
        arguments = arguments[1:]
    raise SystemExit(worker_main(arguments))

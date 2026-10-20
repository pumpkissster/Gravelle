"""Administrator controlled, persistent per-page reading derivatives.

The catalogue remains the authority for source paths. Workers receive only an
operator-owned temporary copy, never a user supplied path or a library root.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import errno
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import zipfile

from PIL import Image, ImageCms, ImageOps

MIB = 1024 * 1024
ALGORITHM_VERSION = 2
DEFAULT_RULES = {
    "enabled": False, "file_size_mb": 10, "max_source_pixels": 40_000_000,
    "max_source_side": 10_000, "output_format": "webp", "quality": 88,
    "max_output_side": 3840, "workers": 1, "memory_limit_mb": 1024,
    "timeout_seconds": 120, "storage_limit_mb": 10240, "min_free_space_mb": 512,
}
INTEGER_RANGES = {
    "max_source_pixels": (0, 1_000_000_000), "max_source_side": (0, 100000),
    "quality": (1, 95), "max_output_side": (512, 8192), "workers": (1, 1),
    "memory_limit_mb": (256, 8192), "timeout_seconds": (10, 600),
    "storage_limit_mb": (0, 1048576), "min_free_space_mb": (0, 1048576),
}


def normalize_rules(value, previous=None):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_RULES):
        raise ValueError("优化规则包含不支持的配置。")
    result = dict(previous or DEFAULT_RULES)
    result.update(value)
    if type(result["enabled"]) is not bool:
        raise ValueError("优化开关需要 true 或 false。")
    size = result["file_size_mb"]
    if type(size) not in (int, float) or not math.isfinite(size) or not 0 <= size <= 1024:
        raise ValueError("文件大小阈值需要在 0～1024 MiB 之间。")
    for key, (minimum, maximum) in INTEGER_RANGES.items():
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f"优化参数 {key} 超出有效范围（{minimum}～{maximum}）。")
    if result["output_format"] not in ("webp", "jpeg"):
        raise ValueError("输出格式需要是 webp 或 jpeg。")
    if result["enabled"] and not any(result[key] for key in ("file_size_mb", "max_source_pixels", "max_source_side")):
        raise ValueError("至少需要启用一项图片触发规则。")
    return result


def output_profile(rules):
    values = {key: rules[key] for key in ("output_format", "quality", "max_output_side")}
    values["algorithm"] = ALGORITHM_VERSION
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]


def match_reasons(page, rules):
    if not rules["enabled"]:
        return []
    reasons = []
    if rules["file_size_mb"] and page["size"] > rules["file_size_mb"] * MIB:
        reasons.append("文件大小超过阈值")
    if rules["max_source_pixels"] and page["width"] * page["height"] > rules["max_source_pixels"]:
        reasons.append("图片像素超过阈值")
    if rules["max_source_side"] and max(page["width"], page["height"]) > rules["max_source_side"]:
        reasons.append("图片边长超过阈值")
    if page.get("inspect_error"):
        # Unknown dimensions must not silently bypass an enabled pixel rule.
        reasons.append("图片信息读取失败")
    return reasons


def image_dimensions(handle):
    """Read headers only; no full image decode during a library scan."""
    handle.seek(0)
    head = handle.read(32)
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        kind = head[12:16]
        if kind == b"VP8X" and len(head) >= 30:
            return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        if kind == b"VP8L" and len(head) >= 25 and head[20] == 47:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
        if kind == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", head[26:30])
            return width & 0x3fff, height & 0x3fff
    if head.startswith(b"\xff\xd8"):
        handle.seek(2)
        # Bound header traversal, including malformed marker streams.
        while handle.tell() < MIB:
            byte = handle.read(1)
            if not byte:
                break
            if byte != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker or marker in (b"\xda", b"\xd9"):
                break
            code = marker[0]
            if code in (0, 1) or 0xd0 <= code <= 0xd8:
                continue
            raw = handle.read(2)
            if len(raw) != 2:
                break
            length = int.from_bytes(raw, "big")
            if length < 2:
                break
            if code in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                raw = handle.read(5)
                if len(raw) == 5:
                    height, width = struct.unpack(">HH", raw[1:5])
                    return width, height
                break
            handle.seek(length - 2, 1)
    handle.seek(0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with Image.open(handle) as image:
            return image.size


class _DarwinMemoryGuard:
    """Sample only this decoder's resident peak; never terminate the server.

    Darwin's dyld shared cache can make a small RLIMIT_AS fail before any
    image is decoded. A sampling guard is intentionally not advertised as an
    OS-enforced allocation ceiling. Existing pixel budgets and parent-owned
    job timeouts still apply, including when a native call delays the sampler.
    """
    interval = 0.1
    exceeded_exit_code = 86
    unavailable_exit_code = 87

    def __init__(self, memory_bytes, *, measure=None, exit_process=None):
        if type(memory_bytes) is not int or memory_bytes <= 0:
            raise ValueError("图片处理进程内存预算无效。")
        if measure is None:
            import resource
            measure = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.memory_bytes = memory_bytes
        self.measure = measure
        self.exit_process = exit_process or os._exit
        self.stopped = threading.Event()
        try:
            initial = self._read_bytes()
        except (OSError, ValueError, RuntimeError, MemoryError) as error:
            raise ValueError("无法监测图片处理进程内存，请查看服务端运行环境。") from error
        if initial > self.memory_bytes:
            raise MemoryError("图片处理所需内存超过任务预算，请提高内存限制或缩小图片。")
        self.thread = threading.Thread(target=self._watch, name="image-memory-guard", daemon=True)
        self.thread.start()

    def _read_bytes(self):
        # Darwin reports ru_maxrss in bytes, unlike Linux's KiB value.
        amount = self.measure()
        if type(amount) is not int or amount <= 0:
            raise RuntimeError("无法监测图片处理进程内存，请查看服务端运行环境。")
        return amount

    def _watch(self):
        while not self.stopped.wait(self.interval):
            try:
                exceeded = self._read_bytes() > self.memory_bytes
            except (OSError, ValueError, RuntimeError, MemoryError):
                self.exit_process(self.unavailable_exit_code)
                return
            if exceeded:
                # A private worker may be inside a native decoder with no safe
                # unwinding path. Its parent owns cleanup of all partial files.
                self.exit_process(self.exceeded_exit_code)
                return

    def close(self):
        self.stopped.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=1)


def _worker_exit_message(returncode):
    """Private decoder exit codes have stable, platform-neutral explanations."""
    if returncode == _DarwinMemoryGuard.exceeded_exit_code:
        return "图片处理所需内存超过任务预算，请提高内存限制或缩小图片。"
    if returncode == _DarwinMemoryGuard.unavailable_exit_code:
        return "无法监测图片处理进程内存，请查看服务端运行环境。"
    return None


def _restrict_worker(memory_bytes):
    """Bound an isolated decoder without applying limits to its parent."""
    if sys.platform == "darwin":
        return _DarwinMemoryGuard(memory_bytes)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        limits.ProcessMemoryLimit = memory_bytes
        if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise RuntimeError("无法设置优化进程内存限制。")
        # Keep the handle alive until process exit.
        return job
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    return None


def _colour_managed(image):
    """Convert an embedded RGB/CMYK/gray profile to tagged sRGB."""
    embedded = image.info.get("icc_profile")
    if not embedded:
        return image, None
    if not isinstance(embedded, bytes) or len(embedded) > 4 * MIB:
        raise ValueError("图片色彩配置无效，请使用原图或修复色彩配置。")
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(embedded))
        destination = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        space = source.profile.xcolor_space.strip()
        alpha = image.convert("RGBA").getchannel("A") if image.mode in ("RGBA", "LA") or "transparency" in image.info else None
        if space == "CMYK":
            if image.mode != "CMYK":
                raise ValueError("CMYK 色彩配置与图片内容不匹配。")
            base = image
        elif space == "RGB":
            if image.mode == "CMYK":
                raise ValueError("RGB 色彩配置与图片内容不匹配。")
            base = image.convert("RGB")
        elif space == "GRAY":
            if image.mode not in ("L", "LA"):
                raise ValueError("灰度色彩配置与图片内容不匹配。")
            base = image.convert("L")
        else:
            raise ValueError("暂不支持此图片色彩空间。")
        result = ImageCms.profileToProfile(base, source, destination, outputMode="RGB",
                                          renderingIntent=ImageCms.Intent.PERCEPTUAL)
        if alpha is not None:
            result.putalpha(alpha)
        return result, destination.tobytes()
    except (ImageCms.PyCMSError, OSError, ValueError) as error:
        raise ValueError("图片色彩配置无法转换，请使用原图或修复色彩配置。") from error


def _lower_background_priority():
    """Best effort only; never prevent a valid image job from running."""
    try:
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000)
        elif hasattr(os, "nice"):
            os.nice(10)
    except (OSError, AttributeError):
        pass


def worker_main(argv=None):
    parser = argparse.ArgumentParser(description="Private Gravelle image conversion worker")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--options", required=True)
    arguments = parser.parse_args(argv)
    try:
        _lower_background_priority()
        rules = normalize_rules(json.loads(arguments.options))
        limit_handle = _restrict_worker(rules["memory_limit_mb"] * MIB)
        Image.MAX_IMAGE_PIXELS = None  # The worker applies its own bounded budget.
        with Image.open(arguments.source) as original:
            if getattr(original, "is_animated", False):
                raise ValueError("动画图片暂不支持优化，请由管理员选择使用原图。")
            if original.width * original.height > 250_000_000:
                raise ValueError("图片像素超过单任务安全上限。")
            side = rules["max_output_side"]
            ratio = min(1, side / max(original.size))
            target = (max(1, round(original.width * ratio)), max(1, round(original.height * ratio)))
            if original.format == "JPEG":
                original.draft("RGB", target)
            if original.width * original.height * 12 > rules["memory_limit_mb"] * MIB * 0.85:
                raise MemoryError("图片解码所需内存超过任务预算，请提高内存限制。")
            original.seek(0)
            original.load()
            converted = ImageOps.exif_transpose(original)
            converted, icc = _colour_managed(converted)
            converted.thumbnail((side, side), Image.Resampling.LANCZOS, reducing_gap=3)
            if rules["output_format"] == "jpeg":
                if converted.mode in ("RGBA", "LA") or "transparency" in converted.info:
                    rgba = converted.convert("RGBA")
                    converted = Image.new("RGB", rgba.size, "white")
                    converted.paste(rgba, mask=rgba.getchannel("A"))
                elif converted.mode != "RGB":
                    converted = converted.convert("RGB")
                converted.save(arguments.output, "JPEG", quality=rules["quality"], optimize=True, progressive=True,
                               icc_profile=icc)
            else:
                if converted.mode not in ("RGB", "RGBA"):
                    converted = converted.convert("RGBA" if "transparency" in converted.info else "RGB")
                converted.save(arguments.output, "WEBP", quality=rules["quality"], method=4, icc_profile=icc or b"")
        with Image.open(arguments.output) as result:
            result.verify()
        print(json.dumps({"ok": True}))
        return 0
    except (Exception, MemoryError) as error:
        print(json.dumps({"ok": False, "error": str(error) or "优化进程内存不足。",
                          "storage_full": isinstance(error, OSError) and error.errno == errno.ENOSPC}))
        return 1


class TranscodingManager:
    def __init__(self, library, error_type=None):
        self.library = library
        self.error_type = error_type
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.auto_start = True
        self.processes = set()
        self.workers = set()
        self.active_books = set()
        self.storage_lock = threading.Lock()
        self.reserved_outputs = {}
        self.root = library.data / "transcoded"
        self._initialize()

    def _error(self, message, status=400):
        # Import lazily to avoid a module cycle and preserve ReaderError handling.
        if self.error_type is None:
            from server import ReaderError
            self.error_type = ReaderError
        return self.error_type(message, status)

    def _initialize(self):
        with closing(self.library.connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tc_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                    revision INTEGER NOT NULL, rules TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tc_books (
                    book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE ON UPDATE CASCADE,
                    decision TEXT NOT NULL DEFAULT 'undecided', status TEXT NOT NULL DEFAULT 'pending',
                    published INTEGER NOT NULL DEFAULT 0, rule_revision INTEGER NOT NULL,
                    source_signature TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL,
                    completed_at REAL);
                CREATE TABLE IF NOT EXISTS tc_pages (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE ON UPDATE CASCADE,
                    page_key TEXT NOT NULL, page_index INTEGER NOT NULL, source_version TEXT NOT NULL,
                    size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    inspect_error TEXT NOT NULL DEFAULT '', matched INTEGER NOT NULL,
                    reasons TEXT NOT NULL, desired_profile TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                    desired_file TEXT NOT NULL DEFAULT '', desired_bytes INTEGER NOT NULL DEFAULT 0,
                    active_file TEXT NOT NULL DEFAULT '', active_source TEXT NOT NULL DEFAULT '',
                    active_profile TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(book_id,page_key));
                CREATE TABLE IF NOT EXISTS tc_outputs (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE ON UPDATE CASCADE,
                    page_key TEXT NOT NULL, source_version TEXT NOT NULL, profile TEXT NOT NULL,
                    file TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, mime TEXT NOT NULL,
                    digest TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(book_id,page_key,source_version,profile));
                CREATE INDEX IF NOT EXISTS tc_pages_status ON tc_pages(book_id,matched,status);
                CREATE INDEX IF NOT EXISTS tc_pages_active_file ON tc_pages(active_file);
                CREATE INDEX IF NOT EXISTS tc_pages_desired_file ON tc_pages(desired_file);
            """)
            self._migrate_update_cascade(connection)
            # Old derivative records have no reliable parameter snapshot. Keep
            # them empty rather than attributing today's settings to old files.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tc_outputs)")}
            if "output_parameters" not in columns:
                connection.execute("ALTER TABLE tc_outputs ADD COLUMN output_parameters TEXT NOT NULL DEFAULT ''")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tc_books)")}
            if "completed_fingerprint" not in columns:
                connection.execute("ALTER TABLE tc_books ADD COLUMN completed_fingerprint TEXT NOT NULL DEFAULT ''")
            for row in connection.execute("SELECT book_id FROM tc_books WHERE completed_at IS NOT NULL AND completed_fingerprint=''").fetchall():
                active = connection.execute("""SELECT p.page_key,p.active_source,p.active_profile,p.active_file,o.created_at
                    FROM tc_pages p LEFT JOIN tc_outputs o ON o.file=p.active_file
                    WHERE p.book_id=? AND p.active_file<>'' ORDER BY p.page_key""", (row["book_id"],)).fetchall()
                marker = hashlib.sha256(json.dumps([tuple(item) for item in active], ensure_ascii=False).encode()).hexdigest()
                connection.execute("UPDATE tc_books SET completed_fingerprint=? WHERE book_id=?", (marker, row["book_id"]))
            connection.execute("INSERT OR IGNORE INTO tc_settings(id,enabled,revision,rules) VALUES(1,0,1,?)",
                               (json.dumps(DEFAULT_RULES),))
            # Process termination never means a partially converted book is ready.
            connection.execute("UPDATE tc_books SET status='queued' WHERE status='running'")
            connection.execute("UPDATE tc_pages SET status='pending' WHERE status='running'")
        # Library owns the data-directory process lock before constructing this
        # manager. No worker has started yet, so abandoned jobs can be recovered
        # without racing a live writer or removing a registered generation.
        self._recover_abandoned_files()

    def _recover_abandoned_files(self):
        if not self.root.exists():
            return
        with closing(self.library.connect()) as connection:
            retained = {row[0] for row in connection.execute("SELECT file FROM tc_outputs")}
            retained.update(row[0] for row in connection.execute("SELECT active_file FROM tc_pages WHERE active_file<>''"))
            retained.update(row[0] for row in connection.execute("SELECT desired_file FROM tc_pages WHERE desired_file<>''"))
        try:
            root = self._storage_directory()
            folders = list(root.iterdir())
        except Exception:
            return
        for folder in folders:
            if not re.fullmatch(r"[a-f0-9]{24}", folder.name):
                continue
            try:
                folder = self._storage_directory(folder.name)
                for candidate in folder.iterdir():
                    info = candidate.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    if stat.S_ISREG(info.st_mode) and re.fullmatch(r"[a-f0-9]{64}\.(webp|jpg)", candidate.name):
                        if folder.name + "/" + candidate.name not in retained:
                            self._output_path(folder.name + "/" + candidate.name).unlink()
                    elif stat.S_ISDIR(info.st_mode) and re.fullmatch(r"\.job-[a-z0-9_]{8}", candidate.name):
                        # A job contains only these private files, never nested
                        # folders. Leave unfamiliar content and links untouched.
                        files = list(candidate.iterdir())
                        if all(item.name in ("input", "output.webp", "output.jpg") and
                               stat.S_ISREG(item.lstat().st_mode) and
                               not stat.S_ISLNK(item.lstat().st_mode) and
                               not getattr(item.lstat(), "st_file_attributes", 0) & 0x400 for item in files):
                            for item in files:
                                item.unlink()
                            candidate.rmdir()
            except Exception:
                # A file still held by an interrupted OS process is retried on
                # the next start; recovery must not prevent opening the reader.
                continue

    @staticmethod
    def _migrate_update_cascade(connection):
        """Preserve early local schema rows while allowing historical ID moves."""
        # These three owned tables reference books directly, not one another.
        # Rebuilding them in one transaction therefore preserves all rows and
        # output mappings, without disabling foreign-key validation.
        connection.execute("BEGIN IMMEDIATE")
        for table in ("tc_books", "tc_pages", "tc_outputs"):
            foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            if all(row["on_update"] == "CASCADE" for row in foreign_keys if row["table"] == "books"):
                continue
            original = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            replacement = table + "_id_upgrade"
            create_sql = re.sub(r"(?i)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?" + table + r"[\"`\]]?",
                                "CREATE TABLE " + replacement, original, count=1)
            create_sql = re.sub(r"(?i)REFERENCES\s+books\s*\(\s*id\s*\)\s+ON\s+DELETE\s+CASCADE(?:\s+ON\s+UPDATE\s+(?:NO\s+ACTION|RESTRICT|CASCADE))?",
                                "REFERENCES books(id) ON DELETE CASCADE ON UPDATE CASCADE", create_sql)
            indexes = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
            connection.execute(create_sql)
            columns = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
            names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            connection.execute(f"INSERT INTO {replacement}({names}) SELECT {names} FROM {table}")
            connection.execute(f"DROP TABLE {table}")
            connection.execute(f"ALTER TABLE {replacement} RENAME TO {table}")
            for statement in indexes:
                connection.execute(statement)

    def settings(self, connection=None):
        if connection is None:
            with closing(self.library.connect()) as own:
                return self.settings(own)
        row = connection.execute("SELECT * FROM tc_settings WHERE id=1").fetchone()
        return json.loads(row["rules"]), row["revision"]

    def start(self):
        with self.lock:
            if self.stop_event.is_set():
                return
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="image-transcoding", daemon=True)
                self.thread.start()
        self.wake.set()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.library.task_scheduler.wake()
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
        deadline = time.monotonic() + 10
        with self.lock:
            workers = tuple(self.workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))

    def is_alive(self):
        with self.lock:
            return bool((self.thread and self.thread.is_alive()) or any(worker.is_alive() for worker in self.workers)
                        or self.active_books)

    def _loop(self):
        while not self.stop_event.is_set() and not self.library.stop_event.is_set():
            try:
                while not self.stop_event.is_set() and not self.library.stop_event.is_set():
                    with self.lock:
                        if len(self.workers) >= self.library.task_scheduler.concurrency:
                            break
                    selected = self._take_job()
                    if selected is None:
                        break
                    worker = threading.Thread(target=self._background_job, args=selected,
                                              name="image-optimization-page", daemon=True)
                    with self.lock:
                        self.workers.add(worker)
                    try:
                        worker.start()
                    except Exception:
                        with self.lock:
                            self.workers.discard(worker)
                            self.active_books.discard(selected[0]["id"])
                        raise
            except Exception:
                import logging
                logging.getLogger("sumi").exception("Transcoding task failed")
            self.wake.wait(.25)
            self.wake.clear()

    def _background_job(self, job, rules):
        try:
            self._process_job(job, rules)
        except Exception:
            import logging
            logging.getLogger("sumi").exception("Image optimization task failed")
        finally:
            with self.lock:
                self.workers.discard(threading.current_thread())
            self.wake.set()

    def _probe(self, handle, size, version, key, index):
        page = dict(page_key=key, page_index=index, source_version=version,
                    size=size, width=0, height=0, inspect_error="")
        try:
            page["width"], page["height"] = image_dimensions(handle)
            if not 0 < page["width"] <= 1_000_000 or not 0 < page["height"] <= 1_000_000:
                raise ValueError("图片尺寸无效。")
        except (OSError, ValueError, struct.error, Image.DecompressionBombError, zipfile.BadZipFile, RuntimeError, EOFError) as error:
            page["inspect_error"] = "无法读取图片尺寸，请检查文件。"
        return page

    @staticmethod
    def _file_version(info):
        return f"{info.st_size}:{info.st_mtime_ns}:{getattr(info, 'st_ino', 0)}"

    @staticmethod
    def _archive_version(info, entry):
        # Repacking an archive or inserting another page must not invalidate
        # unchanged entries. The canonical page key + ZIP's content CRC/size
        # are the stable source marker; the live archive is still revalidated
        # through its safe handle and central-directory limits on every read.
        return f"zip:{entry.CRC}:{entry.file_size}"

    def inspect_book(self, book):
        """Source headers are inspected outside catalogue write transactions."""
        # PDF pages already have bounded, on-demand rendering and its own cache.
        # Do not eagerly rasterize a document or apply source-image rules to it.
        if book["format"] in ("pdf", "epub"):
            return []
        from server import check_zip_directory, validate_archive_image
        root = self.library._directory_path(book["source_root"])
        keys = json.loads(book["pages"])
        with closing(self.library.connect()) as connection:
            cached = {row["page_key"]: dict(row) for row in connection.execute(
                "SELECT * FROM tc_pages WHERE book_id=?", (book["id"],))}
        result = []
        if book["format"] == "folder":
            for index, key in enumerate(keys):
                with self.library._open_media_file(root, key) as handle:
                    info = os.fstat(handle.fileno())
                    version = self._file_version(info)
                    old = cached.get(key)
                    page = dict(old) if old and old["source_version"] == version else self._probe(handle, info.st_size, version, key, index)
                    page["page_index"] = index
                    result.append(page)
        else:
            with self.library._open_media_file(root, book["path"]) as handle:
                info = os.fstat(handle.fileno())
                check_zip_directory(handle)
                with zipfile.ZipFile(handle) as archive:
                    for index, key in enumerate(keys):
                        entry = archive.getinfo(key)
                        validate_archive_image(entry)
                        version = self._archive_version(info, entry)
                        old = cached.get(key)
                        if old and old["source_version"] == version:
                            page = dict(old)
                        else:
                            with archive.open(entry) as source:
                                page = self._probe(source, entry.file_size, version, key, index)
                        page["page_index"] = index
                        result.append(page)
        return result

    def apply_book(self, connection, book, pages):
        """Publish scan metadata and eligibility atomically with catalogue rows."""
        rules, revision = self.settings(connection)
        old_book = connection.execute("SELECT * FROM tc_books WHERE book_id=?", (book["id"],)).fetchone()
        previous = {row["page_key"]: dict(row) for row in connection.execute(
            "SELECT * FROM tc_pages WHERE book_id=?", (book["id"],))}
        profile = output_profile(rules)
        decision = old_book["decision"] if old_book else "undecided"
        published = bool(old_book and old_book["published"])
        source_changed = bool(old_book and old_book["source_signature"] != book["signature"])
        connection.execute("""INSERT INTO tc_books(book_id,decision,status,published,rule_revision,source_signature,updated_at)
            VALUES(?,?,'pending',?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
            rule_revision=excluded.rule_revision,source_signature=excluded.source_signature,updated_at=excluded.updated_at""",
            (book["id"], decision, int(published), revision, book["signature"], time.time()))
        current_keys = set()
        matched = []
        changed = source_changed
        invalid_source = False
        for page in pages:
            key = page["page_key"]
            current_keys.add(key)
            old = previous.get(key)
            reasons = match_reasons(page, rules)
            same_source = bool(old and old["source_version"] == page["source_version"])
            old_active = old["active_file"] if same_source else ""
            active_source = old["active_source"] if same_source else ""
            active_profile = old["active_profile"] if same_source else ""
            missing_active = bool(old_active and not self._output_exists(old_active))
            if missing_active:
                old_active, active_source, active_profile = "", "", ""
            output = connection.execute("""SELECT * FROM tc_outputs WHERE book_id=? AND page_key=?
                AND source_version=? AND profile=?""", (book["id"], key, page["source_version"], profile)).fetchone()
            if output and not self._output_exists(output["file"]):
                output = None
            status = "success" if output and reasons else "pending"
            if (old and same_source and old["desired_profile"] == profile and bool(old["matched"]) == bool(reasons)
                    and (old["status"] != "success" or output)):
                status = "pending" if old["status"] == "running" else old["status"]
            else:
                changed = True
            if reasons:
                matched.append(key)
                if old_book and old_book["decision"] == "transcode" and (not same_source or missing_active):
                    invalid_source = True
            connection.execute("""INSERT INTO tc_pages(book_id,page_key,page_index,source_version,size,width,height,
                inspect_error,matched,reasons,desired_profile,status,error,desired_file,desired_bytes,active_file,active_source,active_profile)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id,page_key) DO UPDATE SET
                page_index=excluded.page_index,source_version=excluded.source_version,size=excluded.size,width=excluded.width,
                height=excluded.height,inspect_error=excluded.inspect_error,matched=excluded.matched,reasons=excluded.reasons,
                desired_profile=excluded.desired_profile,status=excluded.status,error=excluded.error,
                desired_file=excluded.desired_file,desired_bytes=excluded.desired_bytes,active_file=excluded.active_file,
                active_source=excluded.active_source,active_profile=excluded.active_profile""",
                (book["id"], key, page["page_index"], page["source_version"], page["size"], page["width"], page["height"],
                 page.get("inspect_error", ""), int(bool(reasons)), json.dumps(reasons, ensure_ascii=False), profile, status,
                 old["error"] if old and same_source and status == "failed" else "",
                 output["file"] if output else "", output["size"] if output else 0,
                 old_active, active_source, active_profile))
        for key in set(previous) - current_keys:
            connection.execute("DELETE FROM tc_pages WHERE book_id=? AND page_key=?", (book["id"], key))
            changed = True
        if not rules["enabled"] or not matched:
            status, published = "original", True
            connection.execute("UPDATE tc_pages SET active_file='',active_source='',active_profile='' WHERE book_id=?", (book["id"],))
        elif decision == "original":
            status, published = "original", True
        elif decision == "undecided":
            status, published = "pending", False
        else:
            pending = connection.execute("SELECT 1 FROM tc_pages WHERE book_id=? AND matched=1 AND status<>'success' LIMIT 1", (book["id"],)).fetchone()
            if not pending:
                self._publish(connection, book["id"])
                status, published = "ready", True
            elif changed:
                status = "pending"
                # Old source content must never stand in for replaced/new pages.
                if invalid_source:
                    published = False
            else:
                status = old_book["status"] if old_book else "pending"
        connection.execute("UPDATE tc_books SET status=?,published=?,generation=generation+?,error=CASE WHEN ? THEN '' ELSE error END WHERE book_id=?",
                           (status, int(published), int(changed), int(changed), book["id"]))

    def _publish(self, connection, book_id, new_output=False):
        outputs = connection.execute("""SELECT p.page_key,p.source_version,p.desired_profile,p.desired_file,o.created_at
            FROM tc_pages p LEFT JOIN tc_outputs o ON o.file=p.desired_file
            WHERE p.book_id=? AND p.matched=1 ORDER BY p.page_key""", (book_id,)).fetchall()
        fingerprint = hashlib.sha256(json.dumps([tuple(row) for row in outputs], ensure_ascii=False).encode()).hexdigest()
        previous = connection.execute("SELECT completed_at,completed_fingerprint FROM tc_books WHERE book_id=?", (book_id,)).fetchone()
        # Only the last successful conversion marks a batch completion. Scans,
        # threshold changes, and selecting already generated files do not.
        completed = previous["completed_at"]
        if new_output:
            completed = time.time()
        connection.execute("""UPDATE tc_pages SET active_file=CASE WHEN matched=1 THEN desired_file ELSE '' END,
            active_source=CASE WHEN matched=1 THEN source_version ELSE '' END,
            active_profile=CASE WHEN matched=1 THEN desired_profile ELSE '' END WHERE book_id=?""", (book_id,))
        connection.execute("UPDATE tc_books SET status='ready',published=1,error='',completed_at=?,completed_fingerprint=?,updated_at=? WHERE book_id=?",
                           (completed, fingerprint, time.time(), book_id))

    def _all_sources(self):
        with closing(self.library.connect()) as connection:
            books = [dict(row) for row in connection.execute("SELECT * FROM books WHERE active=1 AND removed=0")]
        inspected = []
        for book in books:
            try:
                pages = self.inspect_book(book)
            except Exception as error:
                # Fail closed: inspection is required to make a decision.
                pages = [dict(page_key=key, page_index=index, source_version="unavailable", size=0,
                              width=0, height=0, inspect_error="无法读取源图片，请检查目录权限并重新扫描。")
                         for index, key in enumerate(json.loads(book["pages"]))]
            inspected.append((book, pages))
        return inspected

    def preview_rules(self, body):
        self.library.require_admin()
        with self.lock:
            current, revision = self.settings()
            try:
                rules = normalize_rules(body.get("rules"), current)
            except ValueError as error:
                raise self._error(str(error)) from error
            sources = self._all_sources()
            return self._preview(rules, revision, sources)

    def _preview(self, rules, revision, sources):
        profile = output_profile(rules)
        impacts = []
        with closing(self.library.connect()) as connection:
            for book, pages in sources:
                old = {row["page_key"]: row for row in connection.execute("SELECT * FROM tc_pages WHERE book_id=?", (book["id"],))}
                newly, removed, redo = 0, 0, 0
                reasons = set()
                count = 0
                for page in pages:
                    current_reasons = match_reasons(page, rules)
                    reasons.update(current_reasons)
                    count += bool(current_reasons)
                    before = old.get(page["page_key"])
                    was_matched = bool(before and before["matched"])
                    if current_reasons and not was_matched:
                        newly += 1
                    elif was_matched and not current_reasons:
                        removed += 1
                    if current_reasons and before and before["active_file"] and (
                            before["active_profile"] != profile or before["active_source"] != page["source_version"]):
                        redo += 1
                if newly or removed or redo:
                    library_name = connection.execute("SELECT name FROM media_libraries WHERE id=?", (book["library_id"],)).fetchone()
                    impacts.append(dict(id=book["id"], title=book["title"], library_id=book["library_id"],
                                        library_name=library_name[0] if library_name else "", newly_matched=newly,
                                        no_longer_matched=removed, needs_retranscode=redo,
                                        matched_count=count, reasons=sorted(reasons)))
        totals = {key: sum(item[key] for item in impacts) for key in ("newly_matched", "no_longer_matched", "needs_retranscode")}
        totals["affected_books"] = len(impacts)
        return dict(rules=rules, revision=revision, impacts=impacts, totals=totals)

    def save_rules(self, body):
        self.library.require_admin()
        # A concurrent scan must not publish a newly discovered work under the
        # previous disabled rule between this snapshot and the rules commit.
        # Catalogue mutations use state_lock; metadata/reader writes still run
        # independently while only this configuration operation takes it.
        with self.lock, self.library.state_lock:
            current, revision = self.settings()
            expected = body.get("expected_revision", revision)
            if type(expected) is not int or expected != revision:
                raise self._error("优化规则已被修改，请刷新后重试。", 409)
            try:
                rules = normalize_rules(body.get("rules"), current)
            except ValueError as error:
                raise self._error(str(error)) from error
            sources = self._all_sources()
            preview = self._preview(rules, revision, sources)
            with closing(self.library.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.settings(connection)[1] != revision:
                    raise self._error("优化规则已被修改，请刷新后重试。", 409)
                connection.execute("UPDATE tc_settings SET rules=?,enabled=?,revision=revision+1 WHERE id=1",
                                   (json.dumps(rules), int(rules["enabled"])))
                for book, pages in sources:
                    current_book = connection.execute("SELECT * FROM books WHERE id=? AND active=1 AND removed=0", (book["id"],)).fetchone()
                    if current_book and current_book["signature"] == book["signature"]:
                        self.apply_book(connection, book, pages)
                # Saving parameters never queues pending work; existing queued
                # jobs stop at the current page and await an explicit decision.
                connection.execute("UPDATE tc_books SET status='pending',generation=generation+1 WHERE status IN ('queued','running')")
            result = self.snapshot()
            result["impacts"] = preview["impacts"]
            return result

    def summary(self):
        """Cheap administrator notifications without reading pages or files.

        The scan flag and catalogue rows share state_lock so a completed scan
        cannot be reported alongside the previous set of pending decisions.
        One read transaction also keeps status counts and IDs consistent with
        each other while the image worker finishes a job in another thread.
        Detailed page progress and storage statistics belong to snapshot().
        """
        self.library.require_admin()
        with self.library.state_lock, closing(self.library.connect()) as connection:
            connection.execute("BEGIN")
            rules, revision = self.settings(connection)
            statuses = {row["status"]: row["count"] for row in connection.execute("""
                SELECT t.status, COUNT(*) AS count FROM tc_books t
                JOIN books b ON b.id=t.book_id
                WHERE b.active=1 AND b.removed=0 AND EXISTS(
                    SELECT 1 FROM tc_pages p WHERE p.book_id=b.id AND p.matched=1)
                GROUP BY t.status""")}
            # Rules may return a previously optimized book to pending while
            # keeping the previous readable outputs. It needs another choice
            # just as an undecided new import does; paused/failed jobs do not.
            pending = [dict(row) for row in connection.execute("""
                SELECT b.id,b.library_id,t.generation,t.rule_revision
                FROM tc_books t JOIN books b ON b.id=t.book_id
                WHERE t.status='pending' AND b.active=1 AND b.removed=0
                    AND EXISTS(SELECT 1 FROM tc_pages p WHERE p.book_id=b.id AND p.matched=1)
                ORDER BY b.id""")]
            libraries = [dict(id=row["id"], last_scan=row["last_scan"],
                              scanning=row["id"] == self.library.current_scan or
                                       row["id"] in self.library.pending_scans)
                         for row in connection.execute("SELECT id,last_scan FROM media_libraries ORDER BY id")]
        summary = dict(total_books=sum(statuses.values()), pending_books=len(pending),
                       pending_decision_books=len(pending),
                       processing_books=statuses.get("queued", 0) + statuses.get("running", 0),
                       queued_books=statuses.get("queued", 0), failed_books=statuses.get("failed", 0),
                       completed_books=statuses.get("ready", 0), original_books=statuses.get("original", 0),
                       paused_books=statuses.get("paused", 0) + statuses.get("cancelled", 0))
        scans = [item["last_scan"] for item in libraries if item["last_scan"] is not None]
        return dict(summary=summary, enabled=rules["enabled"], revision=revision,
                    pending_book_ids=[item["id"] for item in pending], pending_records=pending,
                    scanning=any(item["scanning"] for item in libraries),
                    last_scan=max(scans) if scans else None, libraries=libraries)

    def snapshot(self):
        self.library.require_admin()
        with closing(self.library.connect()) as connection:
            rules, revision = self.settings(connection)
            records = connection.execute("""SELECT t.*,b.title,b.library_id,b.page_count,l.name AS library_name,
                COALESCE(m.title,b.title) AS display_title FROM tc_books t JOIN books b ON b.id=t.book_id
                JOIN media_libraries l ON l.id=b.library_id LEFT JOIN book_metadata m ON m.book_id=b.id
                WHERE b.active=1 AND b.removed=0 AND EXISTS(SELECT 1 FROM tc_pages p WHERE p.book_id=b.id AND matched=1)
                ORDER BY t.updated_at DESC,b.id""").fetchall()
            books = []
            for row in records:
                pages = list(connection.execute("SELECT * FROM tc_pages WHERE book_id=? AND matched=1", (row["book_id"],)))
                successful = sum(page["status"] == "success" for page in pages)
                failed = sum(page["status"] == "failed" for page in pages)
                running = sum(page["status"] == "running" for page in pages)
                active = list(connection.execute("""SELECT DISTINCT o.file,o.size FROM tc_pages p JOIN tc_outputs o
                    ON o.file=p.active_file WHERE p.book_id=?""", (row["book_id"],)))
                needs = any(page["active_file"] and (page["active_profile"] != page["desired_profile"] or page["active_source"] != page["source_version"]) for page in pages)
                needs = needs or (row["decision"] == "transcode" and successful < len(pages))
                output = dict(id=row["book_id"], title=row["display_title"], library_id=row["library_id"],
                              library_name=row["library_name"], status=row["status"], decision=row["decision"],
                              matched_count=len(pages), success_count=successful, failed_count=failed,
                              pending_count=len(pages) - successful - failed - running, running_count=running, page_count=row["page_count"],
                              reasons=sorted({reason for page in pages for reason in json.loads(page["reasons"])}),
                              actual_bytes=sum(item["size"] for item in active), output_count=len(active),
                              output_path=str(self.root / row["book_id"]) if active else "",
                              error=row["error"], needs_retranscode=bool(needs), published=bool(row["published"]),
                              rule_revision=row["rule_revision"], updated_at=row["updated_at"], completed_at=row["completed_at"])
                books.append(output)
        summary = dict(total_books=len(books), pending_books=sum(item["status"] == "pending" for item in books),
                       processing_books=sum(item["status"] in ("queued", "running") for item in books),
                       queued_books=sum(item["status"] == "queued" for item in books),
                       failed_books=sum(item["status"] == "failed" for item in books),
                       completed_books=sum(item["status"] == "ready" for item in books),
                       original_books=sum(item["status"] == "original" for item in books),
                       paused_books=sum(item["status"] in ("paused", "cancelled") for item in books),
                       matched_images=sum(item["matched_count"] for item in books), actual_bytes=sum(item["actual_bytes"] for item in books))
        summary["pending_decision_books"] = summary["pending_books"]
        with closing(self.library.connect()) as connection:
            summary["stored_bytes"] = connection.execute("SELECT COALESCE(SUM(size),0) FROM tc_outputs").fetchone()[0]
            reclaimable = connection.execute("""SELECT COUNT(*),COALESCE(SUM(o.size),0) FROM tc_outputs o
                WHERE NOT EXISTS(SELECT 1 FROM tc_pages p WHERE p.active_file=o.file OR p.desired_file=o.file)""").fetchone()
            summary["reclaimable_files"], summary["reclaimable_bytes"] = reclaimable[0], reclaimable[1]
        return dict(rules=rules, revision=revision, summary=summary, books=books)

    @staticmethod
    def _parameter_snapshot(record):
        try:
            value = json.loads(record or "")
            keys = {"output_format", "quality", "max_output_side", "algorithm", "rule_revision"}
            return value if isinstance(value, dict) and set(value) <= keys and all(
                key in value for key in ("output_format", "quality", "max_output_side", "algorithm")) else None
        except (ValueError, TypeError):
            return None

    def _display_output_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{24}/[a-f0-9]{64}\.(?:webp|jpg)", name):
            return ""
        return str(self.root / name)

    def details(self, book_id, page=1, page_size=40):
        """Paginated administrator metadata; never decode or prefetch images."""
        self.library.require_admin()
        if not isinstance(book_id, str) or not re.fullmatch(r"[a-f0-9]{24}", book_id):
            raise self._error("作品不存在。", 404)
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise self._error("图片明细页码或页面容量无效。")
        with closing(self.library.connect()) as connection:
            connection.execute("BEGIN")
            record = connection.execute("""SELECT t.*,b.path,b.format,b.source_root,b.page_count,b.library_id,
                COALESCE(m.title,b.title) AS title,l.name AS library_name FROM tc_books t JOIN books b ON b.id=t.book_id
                JOIN media_libraries l ON l.id=b.library_id LEFT JOIN book_metadata m ON m.book_id=b.id
                WHERE t.book_id=? AND b.active=1 AND b.removed=0""", (book_id,)).fetchone()
            if record is None:
                raise self._error("作品不存在，请刷新列表。", 404)
            rules, revision = self.settings(connection)
            counts = connection.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(matched),0) AS matched,
                COALESCE(SUM(matched=1 AND status='success'),0) AS success,
                COALESCE(SUM(matched=1 AND status='failed'),0) AS failed,
                COALESCE(SUM(matched=1 AND status='running'),0) AS running
                FROM tc_pages WHERE book_id=?""", (book_id,)).fetchone()
            total = counts["total"]
            pages_count = max(1, math.ceil(total / page_size))
            page = min(page, pages_count)
            used = connection.execute("""SELECT o.profile,o.output_parameters,COUNT(*) AS count,SUM(o.size) AS bytes
                FROM tc_pages p JOIN tc_outputs o ON o.file=p.active_file WHERE p.book_id=?
                GROUP BY o.profile,o.output_parameters ORDER BY o.profile""", (book_id,)).fetchall()
            actual_profiles = [dict(profile=row["profile"], parameters=self._parameter_snapshot(row["output_parameters"]),
                                    count=row["count"], bytes=row["bytes"]) for row in used]
            rows = connection.execute("""SELECT p.*,a.size AS active_bytes,a.output_parameters AS active_parameters,
                a.created_at AS active_created_at,d.output_parameters AS desired_parameters,d.created_at AS desired_created_at
                FROM tc_pages p LEFT JOIN tc_outputs a ON a.file=p.active_file LEFT JOIN tc_outputs d ON d.file=p.desired_file
                WHERE p.book_id=? ORDER BY p.page_index,p.page_key LIMIT ? OFFSET ?""",
                (book_id, page_size, (page - 1) * page_size)).fetchall()
            items = []
            for row in rows:
                original = str(Path(record["source_root"]) / (row["page_key"] if record["format"] == "folder" else record["path"]))
                items.append(dict(page_key=row["page_key"], page_index=row["page_index"], matched=bool(row["matched"]),
                    status=row["status"] if row["matched"] else "unmatched", reasons=json.loads(row["reasons"]),
                    error=row["error"] or row["inspect_error"], source_bytes=row["size"], width=row["width"], height=row["height"],
                    original_path=original, archive_entry=row["page_key"] if record["format"] != "folder" else "",
                    active_path=self._display_output_path(row["active_file"]), active_bytes=row["active_bytes"] or 0,
                    active_profile=row["active_profile"], active_parameters=self._parameter_snapshot(row["active_parameters"]),
                    desired_path=self._display_output_path(row["desired_file"]), desired_bytes=row["desired_bytes"],
                    desired_profile=row["desired_profile"], desired_parameters=self._parameter_snapshot(row["desired_parameters"]),
                    output_created_at=row["desired_created_at"], active_created_at=row["active_created_at"]))
        return dict(book=dict(id=book_id, title=record["title"], library_id=record["library_id"], library_name=record["library_name"],
                    status=record["status"], published=bool(record["published"]), matched_count=counts["matched"],
                    success_count=counts["success"], failed_count=counts["failed"], running_count=counts["running"],
                    pending_count=counts["matched"] - counts["success"] - counts["failed"] - counts["running"],
                    completed_at=record["completed_at"], actual_bytes=sum(item["bytes"] for item in actual_profiles),
                    output_count=sum(item["count"] for item in actual_profiles)),
                    current_rule_revision=revision, target_rule_revision=record["rule_revision"], target_profile=output_profile(rules),
                    target_parameters={**{key: rules[key] for key in ("output_format", "quality", "max_output_side")},
                                       "algorithm": ALGORITHM_VERSION, "rule_revision": record["rule_revision"]},
                    active_profiles=actual_profiles, pages=items,
                    pagination=dict(page=page, page_size=page_size, total=total, pages=pages_count))

    def cache_stats(self):
        self.library.require_admin()
        with closing(self.library.connect()) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM tc_outputs").fetchone()
            active = connection.execute("""SELECT COUNT(*),COALESCE(SUM(size),0) FROM tc_outputs o
                WHERE EXISTS(SELECT 1 FROM tc_pages p WHERE p.active_file=o.file)""").fetchone()
            waiting = connection.execute("""SELECT COUNT(*),COALESCE(SUM(size),0) FROM tc_outputs o
                WHERE EXISTS(SELECT 1 FROM tc_pages p WHERE p.desired_file=o.file)
                AND NOT EXISTS(SELECT 1 FROM tc_pages p WHERE p.active_file=o.file)""").fetchone()
            old = connection.execute("""SELECT COUNT(*),COALESCE(SUM(size),0) FROM tc_outputs o
                WHERE NOT EXISTS(SELECT 1 FROM tc_pages p WHERE p.active_file=o.file OR p.desired_file=o.file)""").fetchone()
            clearable = connection.execute("""SELECT COUNT(*),COALESCE(SUM(o.size),0) FROM tc_outputs o
                WHERE EXISTS(SELECT 1 FROM tc_pages p JOIN tc_books t ON t.book_id=p.book_id
                    JOIN books b ON b.id=p.book_id WHERE p.active_file=o.file AND t.status='ready'
                    AND t.published=1 AND b.active=1 AND b.removed=0
                    AND NOT EXISTS(SELECT 1 FROM tc_pages q WHERE q.book_id=p.book_id AND q.matched=1
                        AND (q.status<>'success' OR q.desired_file<>q.active_file)))""").fetchone()
        return dict(files=total[0], bytes=total[1], active_files=active[0], active_bytes=active[1],
                    waiting_files=waiting[0], waiting_bytes=waiting[1], reclaimable_files=old[0], reclaimable_bytes=old[1],
                    clearable_files=clearable[0], clearable_bytes=clearable[1])

    def clear_active(self, body):
        """Explicitly switch completed works to originals before discarding files.

        Pending publications and replacement generations are protected. Files
        are unlinked only after the metadata switch commits, so a filesystem
        failure cannot leave a live mapping pointing at a deleted derivative.
        """
        self.library.require_admin()
        if not isinstance(body, dict) or set(body) - {"ids", "confirm"} or body.get("confirm") is not True:
            raise self._error("清除正在使用的优化图片需要明确确认。")
        ids = self.library._batch_ids(body["ids"]) if "ids" in body else None
        if not self.run_lock.acquire(blocking=False):
            raise self._error("有图片正在处理，请暂停任务后再清理。", 409)
        removed_files = removed_bytes = skipped = 0
        outputs = []
        try:
            with self.lock, self.library.state_lock, closing(self.library.connect()) as connection:
                if self.active_books:
                    raise self._error("有图片正在处理，请暂停任务后再清理。", 409)
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    eligible = {row[0] for row in connection.execute("""SELECT t.book_id FROM tc_books t JOIN books b ON b.id=t.book_id
                        WHERE t.status='ready' AND t.published=1 AND b.active=1 AND b.removed=0
                        AND EXISTS(SELECT 1 FROM tc_pages p WHERE p.book_id=t.book_id AND p.active_file<>'')
                        AND NOT EXISTS(SELECT 1 FROM tc_pages p WHERE p.book_id=t.book_id AND p.matched=1
                            AND (p.status<>'success' OR p.desired_file<>p.active_file))""")}
                    selected = eligible if ids is None else set(ids)
                    if not selected <= eligible:
                        raise self._error("部分作品正在等待发布或重新优化，不能清除正在使用的版本。", 409)
                    for book_id in sorted(selected):
                        outputs.extend(dict(row) for row in connection.execute("SELECT file,size FROM tc_outputs WHERE book_id=?", (book_id,)))
                        connection.execute("""UPDATE tc_books SET decision='original',status='original',published=1,
                            generation=generation+1,error='',updated_at=? WHERE book_id=?""", (time.time(), book_id))
                        connection.execute("""UPDATE tc_pages SET active_file='',active_source='',active_profile='',
                            desired_file='',desired_bytes=0,status='pending',error='' WHERE book_id=?""", (book_id,))
                discarded = []
                for row in outputs:
                    try:
                        candidate = self._output_path(row["file"], allow_missing=True)
                        if candidate.exists():
                            size = candidate.stat().st_size
                            candidate.unlink()
                            removed_files += 1
                            removed_bytes += size
                        discarded.append(row["file"])
                    except Exception:
                        skipped += 1
                # Failed deletions remain registered as old unreferenced files,
                # visible to the existing cleanup operation and space accounting.
                with connection:
                    connection.executemany("DELETE FROM tc_outputs WHERE file=?", [(name,) for name in discarded])
        finally:
            self.run_lock.release()
        self.wake.set()
        self.library.task_scheduler.wake()
        return dict(books=len(selected), removed_files=removed_files, removed_bytes=removed_bytes, skipped_files=skipped,
                    switched_to_original=True)

    def cleanup(self, body):
        self.library.require_admin()
        if set(body) - {"ids"}:
            raise self._error("旧副本清理不接受路径或其他参数。")
        ids = self.library._batch_ids(body["ids"]) if "ids" in body else None
        if not self.run_lock.acquire(blocking=False):
            raise self._error("有图片正在处理，请暂停任务后再清理旧副本。", 409)
        removed_files, removed_bytes, skipped = 0, 0, 0
        try:
            with self.lock, closing(self.library.connect()) as connection, connection:
                if self.active_books:
                    raise self._error("有图片正在处理，请暂停任务后再清理旧副本。", 409)
                connection.execute("BEGIN IMMEDIATE")
                query = """SELECT * FROM tc_outputs o WHERE NOT EXISTS(
                    SELECT 1 FROM tc_pages p WHERE p.active_file=o.file OR p.desired_file=o.file)"""
                arguments = []
                if ids is not None:
                    self.library._validate_targets(connection, "books", ids)
                    query += " AND o.book_id IN (" + ",".join("?" for _ in ids) + ")"
                    arguments = ids
                for row in connection.execute(query, arguments).fetchall():
                    try:
                        candidate = self._output_path(row["file"], allow_missing=True)
                        if candidate.exists():
                            size = candidate.stat().st_size
                            candidate.unlink()
                            removed_files += 1
                            removed_bytes += size
                        connection.execute("DELETE FROM tc_outputs WHERE file=?", (row["file"],))
                    except Exception:
                        skipped += 1
        finally:
            self.run_lock.release()
        result = self.snapshot()
        result["cleanup"] = dict(removed_files=removed_files, removed_bytes=removed_bytes, skipped_files=skipped)
        return result

    def actions(self, body):
        self.library.require_admin()
        ids = self.library._batch_ids(body.get("ids"))
        action = body.get("action")
        if action not in ("original", "transcode", "pause", "resume", "cancel", "retry"):
            raise self._error("优化批量操作无效。")
        with self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rules, revision = self.settings(connection)
            if not rules["enabled"]:
                raise self._error("请先启用图片优化规则。")
            for book_id in ids:
                record = connection.execute("""SELECT t.* FROM tc_books t JOIN books b ON b.id=t.book_id
                    WHERE t.book_id=? AND b.active=1 AND b.removed=0 AND
                    EXISTS(SELECT 1 FROM tc_pages p WHERE p.book_id=b.id AND p.matched=1)""", (book_id,)).fetchone()
                if record is None:
                    raise self._error("部分所选作品不在优化名单中，请刷新后重试。", 404)
            for book_id in ids:
                if action == "original":
                    connection.execute("UPDATE tc_books SET decision='original',status='original',published=1,error='',generation=generation+1,updated_at=? WHERE book_id=?", (time.time(), book_id))
                    connection.execute("UPDATE tc_pages SET active_file='',active_source='',active_profile='' WHERE book_id=?", (book_id,))
                elif action in ("pause", "cancel"):
                    connection.execute("UPDATE tc_books SET status=?,generation=generation+1,error='',updated_at=? WHERE book_id=?",
                                       ("paused" if action == "pause" else "cancelled", time.time(), book_id))
                    connection.execute("UPDATE tc_pages SET status='pending' WHERE book_id=? AND status='running'", (book_id,))
                else:
                    connection.execute("UPDATE tc_books SET decision='transcode',status='queued',error='',generation=generation+1,updated_at=? WHERE book_id=?", (time.time(), book_id))
                    for page in connection.execute("SELECT page_key,desired_file FROM tc_pages WHERE book_id=? AND matched=1 AND status='success'", (book_id,)).fetchall():
                        if not page["desired_file"] or not self._output_exists(page["desired_file"]):
                            connection.execute("UPDATE tc_pages SET status='pending',desired_file='',desired_bytes=0 WHERE book_id=? AND page_key=?", (book_id, page["page_key"]))
                    connection.execute("UPDATE tc_pages SET status='pending',error='' WHERE book_id=? AND matched=1 AND status<>'success'", (book_id,))
                    if not connection.execute("SELECT 1 FROM tc_pages WHERE book_id=? AND matched=1 AND status<>'success' LIMIT 1", (book_id,)).fetchone():
                        self._publish(connection, book_id)
        self.wake.set()
        self.library.task_scheduler.wake()
        if self.auto_start:
            self.start()
        return self.snapshot()

    def _storage_directory(self, book_id=None):
        target = self.root if book_id is None else self.root / book_id
        if book_id is not None and not re.fullmatch(r"[a-f0-9]{24}", book_id):
            raise self._error("优化图片路径无效。", 422)
        for candidate in (self.root, target):
            candidate.mkdir(exist_ok=True)
            info = candidate.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise self._error("优化图片目录不能是链接，请检查服务端资料目录。", 422)
            if not candidate.resolve().is_relative_to(self.library.data):
                raise self._error("优化图片目录超出服务端资料目录。", 422)
        return target

    def _output_path(self, name, allow_missing=False):
        parts = PurePosixPath(name).parts
        if len(parts) != 2 or not re.fullmatch(r"[a-f0-9]{24}", parts[0]) or not re.fullmatch(r"[a-f0-9]{64}\.(webp|jpg)", parts[1]):
            raise self._error("优化图片映射无效，请重新优化。", 422)
        folder = self._storage_directory(parts[0])
        candidate = folder / parts[1]
        try:
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise self._error("优化图片不能是链接，请重新优化。", 422)
        except FileNotFoundError:
            if allow_missing:
                return candidate
            raise self._error("优化图片不存在，请重新优化或由管理员选择原图。", 422)
        return candidate

    def _output_exists(self, name):
        try:
            self._output_path(name)
            return True
        except Exception:
            return False

    def _open_source(self, book, page_key):
        """Yield a safe source stream with the current cheap version marker."""
        from contextlib import contextmanager
        from server import check_zip_directory, validate_archive_image
        @contextmanager
        def opened():
            if book["format"] in ("pdf", "epub"):
                raise self._error("PDF 页面使用按需渲染，不参与图片优化或搜图索引。", 422)
            root = self.library._directory_path(book["source_root"])
            if book["format"] == "folder":
                with self.library._open_media_file(root, page_key) as handle:
                    yield handle, self._file_version(os.fstat(handle.fileno()))
            else:
                with self.library._open_media_file(root, book["path"]) as handle:
                    info = os.fstat(handle.fileno())
                    check_zip_directory(handle)
                    with zipfile.ZipFile(handle) as archive:
                        entry = archive.getinfo(page_key)
                        validate_archive_image(entry)
                        with archive.open(entry) as source:
                            yield source, self._archive_version(info, entry)
        return opened()

    def optimized_image(self, book, page_key):
        with closing(self.library.connect()) as connection:
            rules, revision = self.settings(connection)
            if not rules["enabled"]:
                return None
            record = connection.execute("SELECT * FROM tc_books WHERE book_id=?", (book["id"],)).fetchone()
            page = connection.execute("SELECT * FROM tc_pages WHERE book_id=? AND page_key=?", (book["id"], page_key)).fetchone()
            if not record or not page or record["decision"] == "original":
                return None
            if not record["published"]:
                raise self._error("作品正在等待管理员处理。", 404)
            with self._open_source(book, page_key) as (source, version):
                if version != page["source_version"] or (page["active_file"] and version != page["active_source"]):
                    raise self._error("源图片已变化，请管理员重新扫描和处理。", 422)
            if not page["active_file"]:
                # A rule change can newly match a page that was already
                # published as an original. Preserve that active source choice
                # until the complete replacement generation is ready.
                return None
            output = connection.execute("SELECT * FROM tc_outputs WHERE file=?", (page["active_file"],)).fetchone()
            if not output:
                raise self._error("优化图片映射不存在，请重新优化。", 422)
            self._output_path(output["file"])
            # Source boundaries have just been rechecked; do not hash or decode
            # the original again for a derivative cache hit.
            from server import open_library_file
            with open_library_file(self.root, output["file"]) as handle:
                if os.fstat(handle.fileno()).st_size != output["size"]:
                    raise self._error("优化图片已变化，请重新优化。", 422)
                data = handle.read(output["size"] + 1)
            if len(data) != output["size"]:
                raise self._error("优化图片已变化，请重新优化。", 422)
            return data, output["mime"], '"' + output["digest"] + '"'

    def _job_current(self, book_id, generation):
        if self.stop_event.is_set() or self.library.stop_event.is_set():
            return False
        with closing(self.library.connect()) as connection:
            row = connection.execute("SELECT t.status,t.generation,b.active,b.removed FROM tc_books t JOIN books b ON b.id=t.book_id WHERE t.book_id=?", (book_id,)).fetchone()
            return bool(row and row["active"] and not row["removed"] and row["generation"] == generation and row["status"] in ("queued", "running"))

    def run_once(self):
        """Execute at most one page. Exposed for isolated integration tests."""
        selected = self._take_job()
        if selected is None:
            return False
        return self._process_job(*selected)

    def _take_job(self):
        """Reserve one book in memory; waiting must not claim a running SQL row."""
        if not self.run_lock.acquire(blocking=False):
            return None
        try:
            with self.lock, closing(self.library.connect()) as connection:
                rules, revision = self.settings(connection)
                if not rules["enabled"] or self.stop_event.is_set() or self.library.stop_event.is_set():
                    return None
                excluded = sorted(self.active_books)
                extra = " AND b.id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
                job = connection.execute("""SELECT t.generation,t.rule_revision,b.*,p.page_key,p.source_version,p.desired_profile,p.inspect_error
                    FROM tc_books t JOIN books b ON b.id=t.book_id JOIN tc_pages p ON p.book_id=b.id
                    WHERE t.status IN ('queued','running') AND t.decision='transcode' AND b.active=1 AND b.removed=0
                    AND p.matched=1 AND p.status='pending'""" + extra + " ORDER BY t.updated_at,b.id,p.page_index LIMIT 1", excluded).fetchone()
                if not job:
                    return None
                job = dict(job)
                self.active_books.add(job["id"])
                return job, rules
        finally:
            self.run_lock.release()

    def _process_job(self, job, rules):
        try:
            with self.library.task_scheduler.slot(cancel=lambda: not self._job_current(job["id"], job["generation"])) as acquired:
                if not acquired:
                    return True
                with self.lock, closing(self.library.connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute("""SELECT t.generation,t.status,p.status AS page_status,p.desired_profile
                        FROM tc_books t JOIN books b ON b.id=t.book_id JOIN tc_pages p ON p.book_id=b.id
                        WHERE t.book_id=? AND p.page_key=? AND b.active=1 AND b.removed=0""", (job["id"], job["page_key"])).fetchone()
                    if (not current or current["generation"] != job["generation"] or current["status"] not in ("queued", "running")
                            or current["page_status"] != "pending" or current["desired_profile"] != job["desired_profile"]
                            or self.stop_event.is_set() or self.library.stop_event.is_set()):
                        return True
                    connection.execute("UPDATE tc_books SET status='running' WHERE book_id=?", (job["id"],))
                    connection.execute("UPDATE tc_pages SET status='running' WHERE book_id=? AND page_key=?", (job["id"], job["page_key"]))
                return self._execute_job(job, rules)
        finally:
            with self.lock:
                self.active_books.discard(job["id"])
            self.wake.set()

    def _execute_job(self, job, rules):
        result = None
        try:
            result = self._convert(job, rules)
            if not self._job_current(job["id"], job["generation"]):
                self._discard_unreferenced(result["file"])
                return True
            with self.lock, closing(self.library.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self._job_current(job["id"], job["generation"]):
                    self._discard_unreferenced(result["file"])
                    return True
                parameters = {key: rules[key] for key in ("output_format", "quality", "max_output_side")}
                parameters.update(algorithm=ALGORITHM_VERSION, rule_revision=job.get("rule_revision"))
                connection.execute("""INSERT OR REPLACE INTO tc_outputs(book_id,page_key,source_version,profile,file,size,mime,digest,created_at,output_parameters)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (job["id"], job["page_key"], job["source_version"], job["desired_profile"],
                                                  result["file"], result["size"], result["mime"], result["digest"], time.time(), json.dumps(parameters)))
                connection.execute("UPDATE tc_pages SET status='success',error='',desired_file=?,desired_bytes=? WHERE book_id=? AND page_key=?",
                                   (result["file"], result["size"], job["id"], job["page_key"]))
                remaining = connection.execute("SELECT 1 FROM tc_pages WHERE book_id=? AND matched=1 AND status<>'success' LIMIT 1", (job["id"],)).fetchone()
                if not remaining:
                    # Revalidate every source before atomically releasing a
                    # newly imported work or swapping a published generation.
                    expected = {page["page_key"]: page["source_version"] for page in connection.execute("SELECT * FROM tc_pages WHERE book_id=?", (job["id"],))}
                    current = {page["page_key"]: page["source_version"] for page in self.inspect_book(job)}
                    if expected != current:
                        raise self._error("源图片已变化，请重新扫描后再优化。", 422)
                    self._publish(connection, job["id"], new_output=True)
                else:
                    connection.execute("UPDATE tc_books SET status='queued',updated_at=? WHERE book_id=?", (time.time(), job["id"]))
        except Exception as error:
            if result:
                self._discard_unreferenced(result["file"])
            with self.lock, closing(self.library.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("""SELECT t.status,t.generation,b.active,b.removed FROM tc_books t
                    JOIN books b ON b.id=t.book_id WHERE t.book_id=?""", (job["id"],)).fetchone()
                same_generation = bool(current and current["generation"] == job["generation"])
                if (same_generation and current["active"] and not current["removed"]
                        and current["status"] in ("queued", "running")
                        and not self.stop_event.is_set() and not self.library.stop_event.is_set()):
                    paused = isinstance(error, StoragePaused) or (isinstance(error, OSError) and error.errno == errno.ENOSPC)
                    connection.execute("UPDATE tc_pages SET status=?,error=? WHERE book_id=? AND page_key=?",
                                       ("pending" if paused else "failed", str(error)[:500], job["id"], job["page_key"]))
                    connection.execute("UPDATE tc_books SET status=?,error=?,updated_at=? WHERE book_id=?",
                                       ("paused" if paused else "failed", str(error)[:500], time.time(), job["id"]))
                elif same_generation:
                    connection.execute("UPDATE tc_pages SET status='pending' WHERE book_id=? AND page_key=? AND status='running'",
                                       (job["id"], job["page_key"]))
        finally:
            if result:
                with self.storage_lock:
                    self.reserved_outputs.pop(result["file"], None)
        return True

    def _discard_unreferenced(self, name):
        with closing(self.library.connect()) as connection:
            if connection.execute("SELECT 1 FROM tc_outputs WHERE file=?", (name,)).fetchone():
                return
        try:
            self._output_path(name).unlink()
        except Exception:
            pass

    def remove_books(self, ids):
        """Remove only Gravelle-owned derivative files, never library sources."""
        self.wake.set()
        for book_id in ids:
            try:
                folder = self.root / book_id
                if not folder.exists():
                    continue
                folder = self._storage_directory(book_id)
                for candidate in folder.iterdir():
                    info = candidate.lstat()
                    if (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                            and not getattr(info, "st_file_attributes", 0) & 0x400
                            and re.fullmatch(r"[a-f0-9]{64}\.(webp|jpg)", candidate.name)):
                        candidate.unlink()
                # Active worker temporary directories are left for its own
                # cancellation finally block, which removes only its temp files.
                try:
                    folder.rmdir()
                except OSError:
                    pass
            except Exception:
                continue

    def _convert(self, job, rules):
        from server import MAX_PAGE_BYTES
        if job["inspect_error"]:
            raise self._error(job["inspect_error"], 422)
        folder = self._storage_directory(job["id"])
        if shutil.disk_usage(folder).free < rules["min_free_space_mb"] * MIB:
            raise StoragePaused("存储剩余空间不足，优化已暂停。")
        with closing(self.library.connect()) as connection:
            used = connection.execute("SELECT COALESCE(SUM(size),0) FROM tc_outputs").fetchone()[0]
        if rules["storage_limit_mb"] and used >= rules["storage_limit_mb"] * MIB:
            raise StoragePaused("已达到优化图片存储上限，优化已暂停。")
        fingerprint = hashlib.sha256((job["page_key"] + "\0" + job["source_version"] + "\0" + job["desired_profile"]).encode()).hexdigest()
        extension = "webp" if rules["output_format"] == "webp" else "jpg"
        name = fingerprint + "." + extension
        with tempfile.TemporaryDirectory(prefix=".job-", dir=folder) as temporary:
            temporary = Path(temporary)
            source_path = temporary / "input"
            destination = temporary / ("output." + extension)
            with self._open_source(job, job["page_key"]) as (source, version):
                if version != job["source_version"]:
                    raise self._error("源图片已变化，请重新扫描后再优化。", 422)
                with source_path.open("xb") as sink:
                    total = 0
                    while chunk := source.read(MIB):
                        total += len(chunk)
                        if total > MAX_PAGE_BYTES:
                            raise self._error("图片超过单任务读取上限。", 422)
                        if not self._job_current(job["id"], job["generation"]):
                            raise self._error("任务已暂停或取消。", 409)
                        sink.write(chunk)
            if getattr(sys, "frozen", False):
                prefix = [sys.executable, "--transcode-worker"]
            else:
                prefix = [sys.executable, str(Path(__file__).resolve()), "--worker"]
            command = [*prefix, "--source", str(source_path), "--output", str(destination), "--options", json.dumps(rules)]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with tempfile.TemporaryFile(mode="w+b") as log:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           creationflags=flags, close_fds=True)
                with self.lock:
                    self.processes.add(process)
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        if not self._job_current(job["id"], job["generation"]):
                            process.kill()
                            raise self._error("任务已暂停或取消。", 409)
                        if time.monotonic() - started > rules["timeout_seconds"]:
                            process.kill()
                            raise self._error("图片优化超时，请调整任务限制后重试。", 422)
                        time.sleep(0.1)
                    if process.returncode:
                        resource_error = _worker_exit_message(process.returncode)
                        if resource_error:
                            raise self._error(resource_error, 422)
                        log.seek(0)
                        message = log.read(4096).decode("utf-8", errors="replace").strip()
                        try:
                            response = json.loads(message)
                            if response.get("storage_full"):
                                raise StoragePaused("存储空间不足，优化已暂停。")
                            message = response["error"]
                        except (ValueError, KeyError, TypeError):
                            message = "优化进程失败或内存不足，请调整任务限制后重试。"
                        raise self._error(message, 422)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    with self.lock:
                        self.processes.discard(process)
            if not destination.is_file() or destination.stat().st_size <= 0:
                raise self._error("优化未生成有效图片。", 422)
            size = destination.stat().st_size
            with self._open_source(job, job["page_key"]) as (source, version):
                if version != job["source_version"]:
                    raise self._error("源图片在优化时发生变化，请重新扫描。", 422)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            output = job["id"] + "/" + name
            with self.storage_lock:
                with closing(self.library.connect()) as connection:
                    stored = {row["file"]: row["size"] for row in connection.execute("SELECT file,size FROM tc_outputs")}
                used = sum(stored.values()) + sum(value for key, value in self.reserved_outputs.items() if key not in stored)
                if rules["storage_limit_mb"] and used + size > rules["storage_limit_mb"] * MIB:
                    raise StoragePaused("新图片会超过优化图片存储上限，优化已暂停。")
                if shutil.disk_usage(folder).free < rules["min_free_space_mb"] * MIB:
                    raise StoragePaused("存储剩余空间不足，优化已暂停。")
                if not self._job_current(job["id"], job["generation"]):
                    raise self._error("任务已暂停或取消。", 409)
                os.replace(destination, folder / name)
                self.reserved_outputs[output] = size
            return dict(file=output, size=size,
                        mime="image/webp" if extension == "webp" else "image/jpeg", digest=digest)


class StoragePaused(Exception):
    pass


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--worker":
        arguments = arguments[1:]
    raise SystemExit(worker_main(arguments))

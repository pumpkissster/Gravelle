"""Read-only PDF inspection and bounded, disposable server-rendered pages.

PDFium is imported only by the private subprocess. The HTTP application never
opens a PDF parser, executes document actions, or passes library paths to it.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time

MIB = 1024 * 1024
MAX_PDF_BYTES = 512 * MIB
MAX_PAGES = 10_000
MAX_SIDE = 4096
MAX_PIXELS = 16_000_000
MAX_OUTPUT_BYTES = 32 * MIB
DEFAULT_CACHE_MB = 1024
MEMORY_MB = 1024
TIMEOUT_SECONDS = 60
INPUT_IDLE_SECONDS = 300
MAX_CACHE_ENTRIES = 100_000
RENDER_VERSION = 1
WIDTH_BUCKETS = (600, 800, 1024, 1600, 2400, 3200, 4096)
_CACHE_NAME = re.compile(r"([a-f0-9]{24})-([a-f0-9]{24})-([0-9]{6})-([0-9]{4})-v1\.jpg")
_TEMP_NAME = re.compile(r"\.tmp-[a-f0-9]{24}")
_INPUT_NAME = re.compile(r"\.input-[a-zA-Z0-9_-]{8,32}")


def source_version(info):
    """Cheap identity of the *opened* source; never a path or user input."""
    values = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def page_key(index):
    if type(index) is not int or not 0 <= index < MAX_PAGES:
        raise ValueError("PDF 页码无效。")
    return f"pdf:{index + 1:06d}"


def page_index(key):
    if not isinstance(key, str) or not re.fullmatch(r"pdf:[0-9]{6}", key):
        raise ValueError("PDF 页码无效。")
    value = int(key[4:]) - 1
    page_key(value)
    return value


def width_bucket(width):
    if type(width) is not int or not 1 <= width <= MAX_SIDE:
        raise ValueError("PDF 图片宽度需要在 1～4096 之间。")
    return next(value for value in WIDTH_BUCKETS if value >= width)


def _plain(info, directory=False):
    return ((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400
            and (directory or info.st_nlink == 1))


def _same_opened_file(path_info, opened_info):
    # CPython 3.13 on Windows can report different meanings of st_ctime from
    # path.stat and fstat (file change time versus legacy creation time). Keep
    # full ctime-based stamps for before/after comparisons within each API;
    # cross-API identity uses stable device/inode/size/mtime and plain-file
    # checks instead of discarding ctime protection globally.
    return (_plain(path_info) and _plain(opened_info)
            and (path_info.st_dev, path_info.st_ino, path_info.st_size, path_info.st_mtime_ns)
            == (opened_info.st_dev, opened_info.st_ino, opened_info.st_size, opened_info.st_mtime_ns))


def _dimensions(size):
    if (not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or not 0 < value <= 1_000_000 for value in size)):
        raise ValueError("PDF 页面尺寸无效或过大。")
    return [float(size[0]), float(size[1])]


def _render_scale(size, width):
    w, h = _dimensions(size)
    scale = min(width / w, MAX_SIDE / max(w, h), math.sqrt(MAX_PIXELS / (w * h)))
    # PDFium rounds each side up. Leave enough headroom for that rounding.
    while math.ceil(w * scale) * math.ceil(h * scale) > MAX_PIXELS:
        scale *= .999
    return scale


def worker_main(argv=None):
    """Private command, dispatched before GUI startup in frozen launchers."""
    parser = argparse.ArgumentParser(description="Private Gravelle PDF page worker")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("inspect", "render"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--memory-mb", type=int, default=MEMORY_MB)
    args = parser.parse_args(argv)
    guard = None
    try:
        if not 256 <= args.memory_mb <= 4096:
            raise ValueError("PDF 处理内存预算无效。")
        from transcoding import _lower_background_priority, _restrict_worker
        _lower_background_priority()
        guard = _restrict_worker(args.memory_mb * MIB)
        # Do not move this import into the service process. No forms environment
        # or document/action callbacks are initialized, including JavaScript.
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
        info = args.source.lstat()
        if not _plain(info) or not 0 < info.st_size <= MAX_PDF_BYTES:
            raise ValueError("PDF 文件为空、超过 512 MiB 或不是普通文件。")
        with args.source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _same_opened_file(info, opened):
                raise ValueError("PDF 临时文件发生变化，请重试。")
            if not handle.read(1024).lstrip().startswith(b"%PDF-"):
                raise ValueError("PDF 文件无效或已损坏。")
            handle.seek(0)
            try:
                document = pdfium.PdfDocument(handle)
            except pdfium.PdfiumError as error:
                if getattr(error, "err_code", None) == 4:
                    raise ValueError("暂不支持加密的 PDF，请先移除密码后重新扫描。") from error
                raise ValueError("PDF 文件无效或已损坏。") from error
            with document:
                if raw.FPDF_GetSecurityHandlerRevision(document.raw) >= 0:
                    raise ValueError("暂不支持加密的 PDF，请先移除密码后重新扫描。")
                count = len(document)
                if count < 1:
                    raise ValueError("PDF 没有可阅读的页面。")
                if count > MAX_PAGES:
                    raise ValueError("单本 PDF 超过 10,000 页，请拆成分卷。")
                if args.mode == "inspect":
                    sizes = [_dimensions(document.get_page_size(i)) for i in range(count)]
                    result = dict(ok=True, page_count=count, sizes=sizes)
                else:
                    if not 0 <= args.page < count or args.output is None:
                        raise ValueError("PDF 页码不存在。")
                    width = width_bucket(args.width)
                    with closing(document[args.page]) as page:
                        size = _dimensions(page.get_size())
                        scale = _render_scale(size, width)
                        with closing(page.render(scale=scale, may_draw_forms=False, draw_annots=False,
                                                  fill_color=(255, 255, 255, 255))) as bitmap:
                            image = bitmap.to_pil().convert("RGB")
                            if (max(image.size) > MAX_SIDE or image.width * image.height > MAX_PIXELS):
                                raise ValueError("PDF 页面渲染尺寸超过安全上限。")
                            with args.output.open("xb") as output:
                                image.save(output, "JPEG", quality=90, optimize=True, progressive=True)
                            image.close()
                    result = dict(ok=True, page_count=count, size=size)
            if (source_version(os.fstat(handle.fileno())) != source_version(opened)
                    or source_version(args.source.lstat()) != source_version(info)):
                raise ValueError("PDF 临时文件发生变化，请重试。")
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 0
    except MemoryError:
        message = "PDF 处理所需内存超过任务预算，请拆分文件后重试。"
    except ImportError:
        message = "PDF 阅读组件未安装，请检查服务端安装是否完整。"
    except (ValueError, OSError) as error:
        # Only our fixed messages cross the worker boundary. Native decoder
        # errors may contain local paths and must stay private.
        message = str(error) if isinstance(error, ValueError) else "PDF 页面处理失败，请检查文件和存储空间。"
    except Exception:
        message = "PDF 页面处理失败，文件可能损坏或不受支持。"
    finally:
        if guard is not None and hasattr(guard, "close"):
            guard.close()
    print(json.dumps(dict(ok=False, error=message), ensure_ascii=True), flush=True)
    return 1


class PdfManager:
    """Single-lane private decoder and small, shared LRU page cache.

    Callers authorize the book before calling image(). Inspection accepts only
    the server's securely opened source handle, never an HTTP-supplied path.
    """
    def __init__(self, library, error_type=None):
        self.library = library
        self.error_type = error_type
        self.root = Path(library.data) / "pdf-pages"
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.processes = set()
        self.generation = 0
        self._input = None
        self._input_invalidated = False
        self._input_used = 0
        self._accessed = {}
        self._directory()
        self._clean_abandoned()
        manager = getattr(library, "cache_manager", None)
        if manager is not None:
            manager.register_category("pdf", self)

    def _error(self, message, status=422):
        if self.error_type is None:
            from server import ReaderError
            self.error_type = ReaderError
        return self.error_type(message, status)

    def _cancelled(self):
        stopped = getattr(self.library, "stop_event", None)
        return self.stop_event.is_set() or bool(stopped and stopped.is_set())

    def _check_cancelled(self):
        if self._cancelled():
            raise self._error("PDF 任务已取消。", 409)

    def _directory(self):
        self.root.mkdir(exist_ok=True)
        if (not _plain(self.root.lstat(), True)
                or self.root.resolve().parent != Path(self.library.data).resolve()):
            raise self._error("PDF 缓存目录不安全，请检查服务端资料目录。")
        return self.root

    def _limit_bytes(self):
        # Read before taking self.lock: cache settings hold their own lock and
        # may call provider.snapshot()/clear_cache() in the opposite direction.
        cache = getattr(self.library, "cache_manager", None)
        settings = cache.settings() if cache else {}
        value = settings.get("pdf_limit_mb", DEFAULT_CACHE_MB)
        return (value if type(value) is int and 0 <= value <= 1_048_576 else DEFAULT_CACHE_MB) * MIB

    def _open_book(self, book):
        if book.get("format") != "pdf":
            raise self._error("此作品不是 PDF。", 404)
        root = self.library._directory_path(book["source_root"])
        return self.library._open_media_file(root, book["path"])

    @staticmethod
    def source_version(info):
        return source_version(info)

    @staticmethod
    def page_key(index):
        return page_key(index)

    def _source_info(self, handle):
        info = os.fstat(handle.fileno())
        # Source hard links are allowed only if the library's safe opener does;
        # this module never opens that path independently.
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PDF_BYTES:
            raise self._error("PDF 文件为空、超过 512 MiB 或不是普通文件。")
        return info

    def _assert_source(self, handle, version, book=None):
        if source_version(self._source_info(handle)) != version:
            raise self._error("PDF 文件已变化，请重新扫描写真库。", 409)
        if book is not None:
            with self._open_book(book) as current:
                if source_version(self._source_info(current)) != version:
                    raise self._error("PDF 文件已变化，请重新扫描写真库。", 409)

    def _remove_input_directory(self, directory):
        if directory.parent != self._directory() or not _INPUT_NAME.fullmatch(directory.name):
            return
        if not _plain(directory.lstat(), True):
            return
        for name in ("source.pdf", "render.jpg"):
            path = directory / name
            try:
                if _plain(path.lstat()):
                    path.unlink()
            except FileNotFoundError:
                pass
        # Deliberately fails when foreign files/links remain. Never follow
        # them or recursively delete a directory constructed from a path.
        directory.rmdir()

    def _clean_abandoned(self):
        with os.scandir(self._directory()) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_CACHE_ENTRIES:
                    break
                path = Path(entry.path)
                try:
                    if _INPUT_NAME.fullmatch(entry.name):
                        self._remove_input_directory(path)
                    elif _TEMP_NAME.fullmatch(entry.name) and _plain(path.lstat()):
                        path.unlink()
                except OSError:
                    pass

    @contextmanager
    def _lane(self):
        with self.run_lock:
            try:
                yield
            finally:
                limit = self._limit_bytes()
                with self.lock:
                    discard = self._input_invalidated or self._cancelled() or not limit
                    if discard:
                        self._input_invalidated = False
                if discard:
                    self._drop_input()

    def _drop_input(self):
        # Caller owns run_lock. Never recursively remove a computed directory.
        current, self._input = self._input, None
        if not current:
            return
        directory, _, _ = current
        try:
            self._remove_input_directory(directory)
        except OSError:
            pass

    def _private_source(self, handle, version):
        self._input_used = time.monotonic()
        with self.lock:
            invalidated = self._input_invalidated
            self._input_invalidated = False
        if invalidated:
            self._drop_input()
        if self._input and self._input[1] == version:
            directory, _, saved = self._input
            try:
                info = (directory / "source.pdf").lstat()
                if (_plain(directory.lstat(), True) and _plain(info)
                        and source_version(info) == saved):
                    return directory / "source.pdf"
            except OSError:
                pass
        self._drop_input()
        directory = Path(tempfile.mkdtemp(prefix=".input-", dir=self._directory()))
        self._input = (directory, version, "")
        path = directory / "source.pdf"
        handle.seek(0)
        total = 0
        try:
            with path.open("xb") as output:
                while chunk := handle.read(MIB):
                    self._check_cancelled()
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise self._error("PDF 文件超过 512 MiB。")
                    output.write(chunk)
            self._assert_source(handle, version)
            self._input = (directory, version, source_version(path.lstat()))
            return path
        except BaseException:
            self._drop_input()
            raise

    def _worker_command(self):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--pdf-worker"]
        return [sys.executable, str(Path(__file__).resolve())]

    def _run_worker(self, source, mode, index=0, width=1600):
        self._check_cancelled()
        output = source.parent / "render.jpg"
        if output.exists():
            if not _plain(output.lstat()):
                raise self._error("PDF 临时输出文件无效。")
            output.unlink()
        command = [*self._worker_command(), "--source", str(source), "--mode", mode,
                   "--memory-mb", str(MEMORY_MB)]
        if mode == "render":
            command += ["--output", str(output), "--page", str(index), "--width", str(width)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        with self.lock:
            self.processes.add(process)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        try:
            while True:
                self._check_cancelled()
                if time.monotonic() >= deadline:
                    raise self._error("PDF 页面处理超时，请拆分文件后重试。")
                try:
                    raw, _ = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
            from transcoding import _worker_exit_message
            resource_error = _worker_exit_message(process.returncode)
            if resource_error:
                raise self._error(resource_error.replace("图片", "PDF"))
            if len(raw) > MIB:
                raise self._error("PDF 处理结果无效。")
            self._check_cancelled()
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as error:
                raise self._error("PDF 处理进程失败或内存不足，请拆分文件后重试。") from error
            if not isinstance(result, dict):
                raise self._error("PDF 处理结果无效。")
            if process.returncode or result.get("ok") is not True:
                message = result.get("error")
                raise self._error(message if isinstance(message, str) and len(message) <= 200 else "PDF 页面处理失败。")
            count = result.get("page_count")
            if type(count) is not int or not 1 <= count <= MAX_PAGES:
                raise self._error("PDF 页面数量无效。")
            if mode == "inspect":
                sizes = result.get("sizes")
                if not isinstance(sizes, list) or len(sizes) != count:
                    raise self._error("PDF 页面尺寸无效。")
                try:
                    result["sizes"] = [_dimensions(size) for size in sizes]
                except ValueError as error:
                    raise self._error(str(error)) from error
            else:
                result["data"] = self._read_path(output, output=True)
            return result
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            with self.lock:
                self.processes.discard(process)
            try:
                if output.exists() and _plain(output.lstat()):
                    output.unlink()
            except OSError:
                pass

    def inspect(self, handle):
        with self._lane():
            self._check_cancelled()
            version = source_version(self._source_info(handle))
            source = self._private_source(handle, version)
            result = self._run_worker(source, "inspect")
            self._assert_source(handle, version)
            return dict(page_count=result["page_count"], sizes=result["sizes"], source_version=version)

    def _read_path(self, path, output=False):
        if not output and (path.parent != self._directory() or not _CACHE_NAME.fullmatch(path.name)):
            raise self._error("PDF 缓存文件无效。")
        info = path.lstat()
        if not _plain(info) or not 0 < info.st_size <= MAX_OUTPUT_BYTES:
            raise self._error("PDF 页面缓存无效或过大。")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _same_opened_file(info, opened):
                raise self._error("PDF 页面缓存发生变化。")
            data = handle.read(MAX_OUTPUT_BYTES + 1)
            if (source_version(os.fstat(handle.fileno())) != source_version(opened)
                    or source_version(path.lstat()) != source_version(info)):
                raise self._error("PDF 页面缓存发生变化。")
        if not data.startswith(b"\xff\xd8") or len(data) > MAX_OUTPUT_BYTES:
            raise self._error("PDF 页面缓存无效。")
        # Pixel decoding happens in the bounded child; check JPEG headers only.
        from PIL import Image
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG" or max(image.size) > MAX_SIDE or image.width * image.height > MAX_PIXELS:
                    raise ValueError("Invalid rendered page size")
        except (ValueError, OSError) as error:
            raise self._error("PDF 页面缓存无效。") from error
        return data

    def image(self, book, index, width=1600):
        try:
            width = width_bucket(width)
            page_key(index)
        except ValueError as error:
            raise self._error(str(error), 400) from error
        if not 0 <= index < book["page_count"]:
            raise self._error("PDF 页码不存在。", 404)
        if not re.fullmatch(r"[a-f0-9]{24}", book["id"]):
            raise self._error("PDF 作品无效。", 404)
        limit = self._limit_bytes()
        # The single lane also coalesces simultaneous requests for the same
        # page. No parser or duplicate source snapshot runs concurrently.
        with self._lane():
            self._check_cancelled()
            with self._open_book(book) as handle:
                version = source_version(self._source_info(handle))
                expected = book.get("pdf_source_version")
                if expected and expected != version:
                    raise self._error("PDF 文件已变化，请重新扫描写真库。", 409)
                name = f"{book['id']}-{version[:24]}-{index + 1:06d}-{width:04d}-v{RENDER_VERSION}.jpg"
                with self.lock:
                    generation = self.generation
                    path = self._directory() / name
                    data = None
                    if limit:
                        try:
                            data = self._read_path(path)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            pass
                        except Exception as error:
                            if self.error_type is None or not isinstance(error, self.error_type):
                                raise
                        if data is not None:
                            self._assert_source(handle, version, book)
                            # Windows does not implement no-follow utime.
                            # Keep access recency in memory rather than risk
                            # touching a substituted symlink's target.
                            self._accessed[name] = time.time_ns()
                            return data, "image/jpeg", '"' + hashlib.sha256(data).hexdigest() + '"'
                source = self._private_source(handle, version)
                result = self._run_worker(source, "render", index, width)
                self._assert_source(handle, version, book)
                if result["page_count"] != book["page_count"]:
                    raise self._error("PDF 页数已变化，请重新扫描写真库。", 409)
                data = result["data"]
                # An administrator can disable persistence while this render
                # is in flight. Read settings outside self.lock before commit.
                limit = self._limit_bytes()
                with self.lock:
                    if limit and generation == self.generation and not self._cancelled():
                        temporary = self._directory() / (".tmp-" + os.urandom(12).hex())
                        try:
                            with temporary.open("xb") as sink:
                                sink.write(data)
                            # os.replace replaces the directory entry itself;
                            # a pre-existing link is never followed for writing.
                            os.replace(temporary, path)
                        finally:
                            if temporary.exists() and _plain(temporary.lstat()):
                                temporary.unlink()
                        self._trim(limit)
                return data, "image/jpeg", '"' + hashlib.sha256(data).hexdigest() + '"'

    @contextmanager
    def open_page(self, book, index, width=1024):
        data, _, _ = self.image(book, index, width)
        with self._open_book(book) as handle:
            version = source_version(self._source_info(handle))
            expected = book.get("pdf_source_version")
            if expected and version != expected:
                raise self._error("PDF 文件已变化，请重新扫描写真库。", 409)
        with BytesIO(data) as stream:
            yield stream, version

    def _entries(self):
        rows = []
        with os.scandir(self._directory()) as listing:
            for count, entry in enumerate(listing):
                if count >= MAX_CACHE_ENTRIES:
                    break
                if not _CACHE_NAME.fullmatch(entry.name):
                    continue
                try:
                    info = Path(entry.path).lstat()
                    if _plain(info):
                        rows.append((Path(entry.path), info))
                except OSError:
                    pass
        return rows

    @staticmethod
    def _remove(path, expected):
        current = path.lstat()
        if not _plain(current) or source_version(current) != source_version(expected):
            return False
        path.unlink()
        return True

    def _trim(self, limit):
        rows = self._entries()
        total = sum(info.st_size for _, info in rows)
        for path, info in sorted(rows, key=lambda row: self._accessed.get(row[0].name, row[1].st_mtime_ns)):
            if total <= limit:
                break
            try:
                if self._remove(path, info):
                    total -= info.st_size
                    self._accessed.pop(path.name, None)
            except OSError:
                pass

    def prune(self):
        limit = self._limit_bytes()
        with closing(self.library.connect()) as connection:
            current = {row["id"]: row["signature"][:24] for row in connection.execute(
                "SELECT id,signature FROM books WHERE active=1 AND removed=0 AND format='pdf'")}
        with self.lock:
            self.generation += 1
            for path, info in self._entries():
                match = _CACHE_NAME.fullmatch(path.name)
                if current.get(match.group(1)) != match.group(2):
                    try:
                        if self._remove(path, info):
                            self._accessed.pop(path.name, None)
                    except OSError:
                        pass
            self._trim(limit)
        if self.run_lock.acquire(blocking=False):
            try:
                if not limit or time.monotonic() - self._input_used >= INPUT_IDLE_SECONDS:
                    self._drop_input()
            finally:
                self.run_lock.release()

    def snapshot(self):
        with self.lock:
            rows = self._entries()
            return dict(files=len(rows), bytes=sum(info.st_size for _, info in rows),
                        retention="disposable", can_clear=bool(rows))

    def clear_cache(self):
        with self.lock:
            self.generation += 1
            self._input_invalidated = True
            result = dict(files=0, bytes=0, skipped_files=0)
            for path, info in self._entries():
                try:
                    if self._remove(path, info):
                        result["files"] += 1
                        result["bytes"] += info.st_size
                        self._accessed.pop(path.name, None)
                except OSError:
                    result["skipped_files"] += 1
        if self.run_lock.acquire(blocking=False):
            try:
                self._drop_input()
            finally:
                self.run_lock.release()
        return result

    def is_alive(self):
        with self.lock:
            return any(process.poll() is None for process in self.processes)

    def close(self):
        self.stop_event.set()
        with self.lock:
            self.generation += 1
            processes = tuple(self.processes)
        for process in processes:
            if process.poll() is None:
                process.kill()
        # Existing render owners release within the short child reap timeout.
        if self.run_lock.acquire(timeout=10):
            try:
                self._drop_input()
            finally:
                self.run_lock.release()


if __name__ == "__main__":
    raise SystemExit(worker_main())

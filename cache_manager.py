"""Administrator cache accounting and bounded disposable-cover maintenance.

Uploaded covers are user metadata. Optimized reading images are persistent
derivatives, never automatically evicted by the disposable-cover budget.
"""
from contextlib import closing
import os
from pathlib import Path
import re
import stat
import threading
import time

MIB = 1024 * 1024
DEFAULT_COVER_LIMIT_MB = 512
MAX_ENTRIES = 100_000
PAGE_COVER = re.compile(r"[a-f0-9]{24}-page-[a-f0-9]{24}\.jpg")
UPLOAD_COVER = re.compile(r"[a-f0-9]{24}-upload-[a-f0-9]{24}\.jpg")
LIBRARY_COVER = re.compile(r"[a-f0-9]{24}-[a-f0-9]{24}\.jpg")


def _plain(info, directory=False):
    return ((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)


class CacheManager:
    def __init__(self, library, library_cover_manager=None):
        self.library = library
        self.library_covers = library_cover_manager
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self._inventory = None
        self._inventory_at = 0
        self._last_maintenance = 0
        self._providers = {}
        with closing(library.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS cache_settings(
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                cover_limit_mb INTEGER NOT NULL)""")
            connection.execute("INSERT OR IGNORE INTO cache_settings VALUES(1,1,?)", (DEFAULT_COVER_LIMIT_MB,))

    def _error(self, message, status=400):
        from server import ReaderError
        return ReaderError(message, status)

    def start(self):
        with self.lock:
            if self.stop_event.is_set():
                return
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="cover-cache-maintenance", daemon=True)
                self.thread.start()
        self.wake.set()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def enforce_cover_limit(self):
        # Producers call this after releasing their own locks. Coalesced work
        # avoids traversing all covers after every new thumbnail.
        self.wake.set()

    def _loop(self):
        while not self.stop_event.is_set():
            self.wake.wait(60)
            self.wake.clear()
            if self.stop_event.is_set():
                break
            delay = 30 - (time.monotonic() - self._last_maintenance)
            if delay > 0:
                if self.stop_event.wait(delay):
                    break
            try:
                self.maintain()
            except Exception:
                # Unsafe directories or a locked file must not stop the reader.
                pass
            self._last_maintenance = time.monotonic()

    def register_category(self, name, provider):
        """Trusted application integration point; no PDF renderer is installed.

        Providers expose snapshot() and clear_cache(). A future page renderer
        can register a category without overloading optimized-image retention.
        """
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z_]{0,31}", name) or name in {"covers", "library_covers", "optimized"}:
            raise ValueError("Invalid cache category")
        if not callable(getattr(provider, "snapshot", None)) or not callable(getattr(provider, "clear_cache", None)):
            raise ValueError("A cache provider needs snapshot and clear_cache")
        with self.lock:
            self._providers[name] = provider

    def settings(self):
        with closing(self.library.connect()) as connection:
            row = connection.execute("SELECT revision,cover_limit_mb FROM cache_settings WHERE id=1").fetchone()
        return dict(revision=row["revision"], cover_limit_mb=row["cover_limit_mb"])

    def _cover_directory(self):
        directory = self.library.data / "covers"
        info = directory.lstat()
        if not _plain(info, True) or directory.resolve().parent != self.library.data.resolve():
            raise self._error("封面缓存目录不安全，请检查服务端资料目录。", 422)
        return directory

    def _page_entries(self):
        entries, uploads, truncated = [], [], False
        with self.library.cover_lock:
            directory = self._cover_directory()
            with os.scandir(directory) as listing:
                for count, item in enumerate(listing):
                    if count >= MAX_ENTRIES:
                        truncated = True
                        break
                    kind = "covers" if PAGE_COVER.fullmatch(item.name) else "uploaded" if UPLOAD_COVER.fullmatch(item.name) else ""
                    if not kind:
                        continue
                    try:
                        # Windows DirEntry.stat can omit link count/inode;
                        # lstat supplies the identity used again before unlink.
                        info = Path(item.path).lstat()
                        if not _plain(info) or info.st_nlink != 1:
                            continue
                        entry = dict(path=Path(item.path), size=info.st_size, modified=info.st_mtime,
                                     stamp=(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns), kind=kind)
                        (entries if kind == "covers" else uploads).append(entry)
                    except OSError:
                        continue
        return entries, uploads, truncated

    def _library_entries(self):
        if self.library_covers is None:
            return []
        root = self.library.data / "library-covers"
        result = []
        for row in self.library_covers.cache_entries()[:MAX_ENTRIES]:
            path = Path(row["path"])
            if path.parent != root or not LIBRARY_COVER.fullmatch(path.name):
                continue
            result.append({**row, "kind": "library_covers"})
        return result

    def _read_inventory(self, force=False):
        with self.lock:
            if not force and self._inventory is not None and time.monotonic() - self._inventory_at < 30:
                return self._inventory
            entries, uploads, truncated = self._page_entries()
            entries += self._library_entries()
            self._inventory = (entries, uploads, truncated)
            self._inventory_at = time.monotonic()
            return self._inventory

    def _remove_page(self, entry):
        with self.library.cover_lock:
            directory = self._cover_directory()
            path = Path(entry["path"])
            if path.parent != directory or not PAGE_COVER.fullmatch(path.name):
                return dict(files=0, bytes=0)
            try:
                info = path.lstat()
                if (not _plain(info) or info.st_nlink != 1 or
                        entry.get("stamp") != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
                    return dict(files=0, bytes=0)
                path.unlink()
                return dict(files=1, bytes=info.st_size)
            except FileNotFoundError:
                return dict(files=0, bytes=0)

    def maintain(self):
        """Low-frequency disposable cache eviction; never touches tc_outputs."""
        with self.lock:
            limit = self.settings()["cover_limit_mb"] * MIB
            entries, _, truncated = self._read_inventory(force=True)
            used = sum(item["size"] for item in entries)
            removed_files = removed_bytes = skipped = 0
            if limit:
                for entry in sorted(entries, key=lambda item: (item["modified"], str(item["path"]))):
                    if used <= limit or self.stop_event.is_set():
                        break
                    try:
                        result = (self._remove_page(entry) if entry["kind"] == "covers" else
                                  self.library_covers.remove_cache_file(entry["path"]))
                        freed = max(0, int(result.get("bytes", 0)))
                        used -= freed
                        removed_files += int(result.get("files", 0))
                        removed_bytes += freed
                    except Exception:
                        skipped += 1
            self._inventory = None
        return dict(removed_files=removed_files, removed_bytes=removed_bytes, skipped_files=skipped, truncated=truncated)

    def snapshot(self):
        self.library.require_admin()
        with self.lock:
            settings = self.settings()
            entries, uploads, truncated = self._read_inventory()
            groups = {kind: [item for item in entries if item["kind"] == kind] for kind in ("covers", "library_covers")}
            categories = [dict(kind=kind, files=len(rows), bytes=sum(item["size"] for item in rows),
                               retention="disposable", can_clear=bool(rows)) for kind, rows in groups.items()]
            categories[0].update(protected_files=len(uploads), protected_bytes=sum(item["size"] for item in uploads))
            optimized = self.library.transcoding.cache_stats()
            categories.append(dict(kind="optimized", **optimized, retention="persistent", can_clear=optimized["clearable_files"] > 0))
            for kind, provider in self._providers.items():
                categories.append(dict(kind=kind, **provider.snapshot()))
        return dict(settings=settings, categories=categories,
                    summary=dict(bytes=sum(item["bytes"] for item in categories) + sum(item["size"] for item in uploads),
                                 disposable_bytes=sum(item["size"] for item in entries),
                                 protected_cover_bytes=sum(item["size"] for item in uploads), truncated=truncated))

    def save_settings(self, body):
        self.library.require_admin()
        if not isinstance(body, dict) or set(body) != {"expected_revision", "cover_limit_mb"}:
            raise self._error("缓存设置包含不支持的参数。")
        if type(body["cover_limit_mb"]) is not int or not 0 <= body["cover_limit_mb"] <= 1_048_576:
            raise self._error("封面缓存容量需要在 0～1048576 MiB 之间。")
        if type(body["expected_revision"]) is not int:
            raise self._error("缓存设置版本无效。")
        with self.lock, closing(self.library.connect()) as connection, connection:
            changed = connection.execute("UPDATE cache_settings SET cover_limit_mb=?,revision=revision+1 WHERE id=1 AND revision=?",
                (body["cover_limit_mb"], body["expected_revision"])).rowcount
            if changed != 1:
                raise self._error("缓存设置已变化，请刷新后重试。", 409)
        self.wake.set()
        return self.snapshot()

    def clear(self, body):
        self.library.require_admin()
        if not isinstance(body, dict) or set(body) - {"kind", "confirm", "ids"} or body.get("confirm") is not True:
            raise self._error("清理缓存需要明确确认。")
        kind = body.get("kind")
        if kind != "optimized" and "ids" in body:
            raise self._error("此类缓存不接受作品选择或路径。")
        with self.lock:
            if kind == "optimized":
                result = self.library.transcoding.clear_active({key: body[key] for key in ("confirm", "ids") if key in body})
            elif kind == "library_covers" and self.library_covers is not None:
                result = self.library_covers.clear_cache()
            elif kind == "covers":
                entries, _, _ = self._page_entries()
                result = dict(files=0, bytes=0, skipped_files=0)
                for entry in entries:
                    try:
                        removed = self._remove_page(entry)
                        result["files"] += removed["files"]
                        result["bytes"] += removed["bytes"]
                    except OSError:
                        result["skipped_files"] += 1
            elif kind in self._providers:
                result = self._providers[kind].clear_cache()
            else:
                raise self._error("缓存类型不存在。")
            self._inventory = None
        payload = self.snapshot()
        payload["cleared"] = dict(kind=kind, **result)
        return payload

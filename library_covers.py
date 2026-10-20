"""Persistent, permission-aware library montages made from small book covers.

HTTP callers only read a ready JPEG or a neutral placeholder. One background
worker assembles small cached thumbnails; the original photographs stay intact.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from PIL import Image, ImageDraw, ImageOps


WIDTH, HEIGHT = 960, 480
MAX_COVER_BYTES = 2 * 1024 * 1024
MAX_COVER_PIXELS = 400 * 600
_NAME = re.compile(r"([a-f0-9]{24})-([a-f0-9]{24})\.jpg")
_TEMP = re.compile(r"\.tmp-[a-f0-9]{24}")


def _hash(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()[:24]


def _ordinary(info, *, directory=False):
    return (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) and not (
        stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)


class LibraryCoverManager:
    def __init__(self, library, error_type=None):
        self.library = library
        self.error_type = error_type
        self.lock = threading.RLock()
        self.cache_lock = self.lock
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.pending = set()
        self.random = secrets.SystemRandom()
        self.root = library.data / "library-covers"
        self.placeholder = self._placeholder()
        self.placeholder_etag = '"' + hashlib.sha256(self.placeholder).hexdigest() + '"'
        self._initialize()

    def _error(self, message, status=400):
        if self.error_type is None:
            from server import ReaderError
            self.error_type = ReaderError
        return self.error_type(message, status)

    @staticmethod
    def _placeholder():
        image = Image.new("RGB", (WIDTH, HEIGHT), "#f6edf1")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((432, 187, 528, 293), radius=9, outline="#d5b7c6", width=4)
        draw.line((449, 187, 449, 293), fill="#d5b7c6", width=4)
        output = BytesIO()
        image.save(output, "JPEG", quality=82, optimize=True)
        return output.getvalue()

    def _directory(self):
        self.root.mkdir(exist_ok=True)
        if not _ordinary(self.root.lstat(), directory=True):
            raise self._error("写真库封面缓存目录无效。", 422)
        return self.root

    def _initialize(self):
        self._directory()
        with closing(self.library.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS library_cover_cache (
                library_id TEXT PRIMARY KEY REFERENCES media_libraries(id) ON DELETE CASCADE ON UPDATE CASCADE,
                selection TEXT NOT NULL DEFAULT '[]', source_signature TEXT NOT NULL DEFAULT '',
                generation INTEGER NOT NULL DEFAULT 0, ready_signature TEXT NOT NULL DEFAULT '',
                cache_file TEXT NOT NULL DEFAULT '', cache_etag TEXT NOT NULL DEFAULT '',
                deferred INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(library_cover_cache)")}
            if "error" not in columns:
                connection.execute("ALTER TABLE library_cover_cache ADD COLUMN error TEXT NOT NULL DEFAULT ''")
        # Only abandoned files owned by this manager are removed on startup.
        for path in self.root.iterdir():
            if _TEMP.fullmatch(path.name) and _ordinary(path.lstat()):
                path.unlink(missing_ok=True)

    def _authorize(self, connection, library_id, *, admin=False):
        if not isinstance(library_id, str) or not re.fullmatch(r"[a-f0-9]{24}", library_id):
            raise self._error("写真库不存在。", 404)
        actor = self.library._actor(connection)
        if admin:
            self.library.require_admin(connection)
        if not connection.execute("SELECT 1 FROM media_libraries WHERE id=?", (library_id,)).fetchone():
            raise self._error("写真库不存在。", 404)
        if actor["role"] != "admin" and not connection.execute(
                "SELECT 1 FROM user_libraries WHERE user_id=? AND library_id=?", (actor["id"], library_id)).fetchone():
            raise self._error("写真库不存在。", 404)
        if connection.execute("SELECT 1 FROM user_hidden_libraries WHERE user_id=? AND library_id=?",
                              (actor["id"], library_id)).fetchone():
            raise self._error("写真库不存在。", 404)

    def _candidates(self, connection, library_id):
        rows = connection.execute("""SELECT b.id,b.signature,
            COALESCE(m.cover_source,'page') AS cover_source, COALESCE(m.cover_page,0) AS cover_page,
            COALESCE(m.cover_file,'') AS cover_file, COALESCE(m.cover_revision,0) AS cover_revision
            FROM books b LEFT JOIN book_metadata m ON m.book_id=b.id
            WHERE b.library_id=? AND b.active=1 AND b.removed=0
            AND (NOT EXISTS(SELECT 1 FROM tc_settings WHERE enabled=1)
                 OR NOT EXISTS(SELECT 1 FROM tc_books tc WHERE tc.book_id=b.id AND tc.published=0))
            ORDER BY b.id""", (library_id,))
        return {row["id"]: _hash([row[key] for key in row.keys()]) for row in rows}

    @staticmethod
    def _selected_source_versions(connection, selected):
        # Per-page records can be large. Inspect source identity only for the
        # final four choices, never once for every book in the whole library.
        if not selected:
            return {}
        marks = ",".join("?" for _ in selected)
        rows = connection.execute(f"""SELECT b.id,
            COALESCE((SELECT p.source_version FROM tc_pages p WHERE p.book_id=b.id
                AND p.page_index=MIN(MAX(COALESCE(m.cover_page,0),0),MAX(b.page_count-1,0))),'') AS source_version
            FROM books b LEFT JOIN book_metadata m ON m.book_id=b.id WHERE b.id IN ({marks})""", selected)
        return {row["id"]: row["source_version"] for row in rows}

    def _plan(self, connection, library_id, *, shuffle=False, request=False):
        """Caller holds self.lock; the short transaction never performs image I/O."""
        candidates = self._candidates(connection, library_id)
        row = connection.execute("SELECT * FROM library_cover_cache WHERE library_id=?", (library_id,)).fetchone()
        old = json.loads(row["selection"]) if row else []
        selected = [item for item in old if item in candidates][:4]
        count = min(4, len(candidates))
        if shuffle and len(candidates) > 4:
            # Replace at least one member rather than gambling on repeated draws.
            replacements = [item for item in candidates if item not in selected]
            if selected and replacements:
                selected[self.random.randrange(len(selected))] = self.random.choice(replacements)
                self.random.shuffle(selected)
        elif shuffle:
            self.random.shuffle(selected)
        available = [item for item in candidates if item not in selected]
        if len(selected) < count:
            selected.extend(self.random.sample(available, count - len(selected)))
        versions = self._selected_source_versions(connection, selected)
        signature = _hash([(item, candidates[item], versions.get(item, "")) for item in selected])
        changed = shuffle or row is None or selected != old or signature != row["source_signature"]
        if changed:
            connection.execute("""INSERT INTO library_cover_cache(library_id,selection,source_signature,generation,updated_at)
                VALUES(?,?,?,1,?) ON CONFLICT(library_id) DO UPDATE SET selection=excluded.selection,
                source_signature=excluded.source_signature,generation=generation+1,ready_signature='',cache_file='',
                cache_etag='',deferred=0,retry_after=0,error='',updated_at=excluded.updated_at""",
                (library_id, json.dumps(selected), signature, time.time()))
            row = connection.execute("SELECT * FROM library_cover_cache WHERE library_id=?", (library_id,)).fetchone()
        elif request and row["deferred"]:
            connection.execute("UPDATE library_cover_cache SET deferred=0 WHERE library_id=?", (library_id,))
            row = connection.execute("SELECT * FROM library_cover_cache WHERE library_id=?", (library_id,)).fetchone()
        if row["ready_signature"] == signature and row["cache_file"]:
            try:
                owned = _NAME.fullmatch(row["cache_file"])
                valid = bool(owned and owned.group(1) == library_id)
                if valid:
                    cached = (self._directory() / row["cache_file"]).lstat()
                    valid = _ordinary(cached) and 0 < cached.st_size <= MAX_COVER_BYTES
            except OSError:
                valid = False
            if not valid:
                connection.execute("""UPDATE library_cover_cache SET ready_signature='',cache_file='',cache_etag='',
                    retry_after=0 WHERE library_id=?""", (library_id,))
                row = connection.execute("SELECT * FROM library_cover_cache WHERE library_id=?", (library_id,)).fetchone()
        if selected and row["ready_signature"] != signature and (request or not row["deferred"]) and row["retry_after"] <= time.time():
            self.pending.add(library_id)
            self.wake.set()
        return dict(row)

    def _metadata(self, row):
        count = len(json.loads(row["selection"]))
        ready = count and row["ready_signature"] == row["source_signature"] and bool(row["cache_file"])
        pending = bool(count and not ready and not row["deferred"] and row["retry_after"] <= time.time() and not self._cancelled())
        return {"url": f"/api/libraries/{row['library_id']}/cover", "version": _hash([
            row["source_signature"], row["generation"], row["cache_etag"] if ready else "pending"]),
            "pending": pending, "count": count, "error": row["error"] if not pending else "",
            "retry_after": row["retry_after"]}

    def metadata(self, library_id):
        with self.library.auth_lock, self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, library_id)
            return self._metadata(self._plan(connection, library_id, request=True))

    def metadata_many(self, library_ids):
        return {library_id: self.metadata(library_id) for library_id in library_ids}

    def shuffle(self, library_id):
        with self.library.auth_lock, self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, library_id, admin=True)
            row = self._plan(connection, library_id, shuffle=True, request=True)
            return self._metadata(row)

    def _read(self, name):
        if not _NAME.fullmatch(name):
            raise ValueError("Unowned library-cover filename")
        path = self._directory() / name
        info = path.lstat()
        if not _ordinary(info) or not 0 < info.st_size <= MAX_COVER_BYTES:
            raise ValueError("Invalid library-cover cache file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _ordinary(opened) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("Library-cover cache changed")
            data = stream.read(MAX_COVER_BYTES + 1)
        if len(data) > MAX_COVER_BYTES:
            raise ValueError("Library-cover cache too large")
        return data

    def image(self, library_id):
        with self.library.auth_lock, self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, library_id)
            row = self._plan(connection, library_id, request=True)
            if row["ready_signature"] == row["source_signature"] and row["cache_file"]:
                try:
                    data = self._read(row["cache_file"])
                    if '"' + hashlib.sha256(data).hexdigest() + '"' != row["cache_etag"]:
                        raise ValueError("Library-cover digest changed")
                    return data, "image/jpeg", row["cache_etag"]
                except (OSError, ValueError):
                    connection.execute("UPDATE library_cover_cache SET ready_signature='',cache_file='',cache_etag='',deferred=0 WHERE library_id=?",
                                       (library_id,))
                    self.pending.add(library_id)
                    self.wake.set()
            return self.placeholder, "image/jpeg", self.placeholder_etag

    def reconcile(self, library_id=None):
        with self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in connection.execute("SELECT id FROM media_libraries" if library_id is None else
                    "SELECT id FROM media_libraries WHERE id=?", () if library_id is None else (library_id,))]
            for value in ids:
                self._plan(connection, value)
        self._prune_obsolete()
        self.wake.set()

    def start(self):
        with self.lock:
            if self.stop_event.is_set() or self.library.stop_event.is_set() or self.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name="library-covers", daemon=True)
            self.thread.start()
        self.reconcile()

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=30)

    def _cancelled(self):
        return self.stop_event.is_set() or self.library.stop_event.is_set()

    def _loop(self):
        while not self._cancelled():
            try:
                if self.run_once():
                    continue
            except Exception as error:
                logging.getLogger("sumi").warning("Library cover task failed (%s)", type(error).__name__)
            self.wake.wait(30)
            self.wake.clear()
            if not self._cancelled():
                try:
                    self.reconcile()
                except Exception as error:
                    logging.getLogger("sumi").warning("Library cover catalogue check failed (%s)", type(error).__name__)

    @staticmethod
    def _compose(covers):
        canvas = Image.new("RGB", (WIDTH, HEIGHT), "#f6edf1")
        count = len(covers)
        for index, data in enumerate(covers):
            if not 0 < len(data) <= MAX_COVER_BYTES:
                raise ValueError("Book cover size exceeded limit")
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG" or image.width * image.height > MAX_COVER_PIXELS:
                    raise ValueError("Library montage only accepts small cached JPEG covers")
                left, right = index * WIDTH // count, (index + 1) * WIDTH // count
                panel = ImageOps.fit(image, (right - left, HEIGHT), Image.Resampling.LANCZOS)
                canvas.paste(panel, (left, 0))
        output = BytesIO()
        canvas.save(output, "JPEG", quality=85, optimize=True)
        return output.getvalue()

    def run_once(self):
        if not self.run_lock.acquire(blocking=False):
            return False
        temporary = None
        library_id = None
        failed_generation = None
        try:
            if self._cancelled():
                return False
            with self.lock, closing(self.library.connect()) as connection, connection:
                if not self.pending:
                    return False
                library_id = self.pending.pop()
                if not connection.execute("SELECT 1 FROM media_libraries WHERE id=?", (library_id,)).fetchone():
                    return True
                row = self._plan(connection, library_id)
                failed_generation = row["generation"]
                self.pending.discard(library_id)
                if row["deferred"] or row["ready_signature"] == row["source_signature"]:
                    return True
                selected = json.loads(row["selection"])
            if not selected:
                return True
            covers = []
            # A single family uses one service-wide task lease. Small thumbnail
            # reads and composition never run in parallel with another montage.
            with closing(self.library.connect()) as probe:
                def cancelled():
                    return self._cancelled() or not probe.execute(
                        "SELECT 1 FROM library_cover_cache WHERE library_id=? AND generation=? AND deferred=0",
                        (library_id, row["generation"])).fetchone()
                with self.library.task_scheduler.slot(resource="library-covers", cancel=cancelled) as acquired:
                    if not acquired:
                        return not self._cancelled()
                    for book_id in selected:
                        # The cover lock comes first; explicit combined cache
                        # cleanup can invalidate this generation before another
                        # book thumbnail is generated. The probe is autocommit.
                        with self.library.cover_lock:
                            if cancelled():
                                return not self._cancelled()
                            covers.append(self.library.cover(book_id)[0])
                    data = self._compose(covers)
            if self._cancelled():
                return False
            # Publish only if all selected books are still eligible and neither
            # shuffle nor explicit cleanup replaced this generation meanwhile.
            with self.lock, closing(self.library.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if not connection.execute("SELECT 1 FROM media_libraries WHERE id=?", (library_id,)).fetchone():
                    return True
                current = self._plan(connection, library_id)
                if current["generation"] != row["generation"] or current["source_signature"] != row["source_signature"] or current["deferred"]:
                    return True
                name = library_id + "-" + _hash([row["source_signature"], row["generation"]]) + ".jpg"
                temporary = self._directory() / (".tmp-" + secrets.token_hex(12))
                with temporary.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.root / name)
                temporary = None
                connection.execute("""UPDATE library_cover_cache SET ready_signature=source_signature,cache_file=?,cache_etag=?,
                    updated_at=?,retry_after=0,error='' WHERE library_id=? AND generation=?""",
                    (name, '"' + hashlib.sha256(data).hexdigest() + '"', time.time(), library_id, row["generation"]))
            self._prune(library_id, name)
            cache_manager = getattr(self.library, "cache_manager", None)
            if cache_manager:
                cache_manager.enforce_cover_limit()
            return True
        except Exception:
            if library_id is not None:
                with self.lock, closing(self.library.connect()) as connection, connection:
                    connection.execute("UPDATE library_cover_cache SET retry_after=?,error=? WHERE library_id=? AND generation=?",
                                       (time.time() + 60, "写真库封面暂时生成失败，可换一组封面或稍后重试。", library_id, failed_generation))
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            self.run_lock.release()

    def _prune(self, library_id, keep):
        with self.lock:
            for path in self._directory().glob(library_id + "-*.jpg"):
                if path.name != keep and _NAME.fullmatch(path.name) and _ordinary(path.lstat()):
                    path.unlink(missing_ok=True)

    def _prune_obsolete(self):
        with self.lock, closing(self.library.connect()) as connection:
            current = {row[0] for row in connection.execute("SELECT cache_file FROM library_cover_cache WHERE cache_file<>''")}
            for entry in self.cache_entries():
                if entry["path"].name not in current:
                    entry["path"].unlink(missing_ok=True)

    def cache_entries(self):
        with self.lock:
            entries = []
            for path in self._directory().iterdir():
                if _NAME.fullmatch(path.name):
                    try:
                        info = path.lstat()
                        if _ordinary(info):
                            entries.append({"path": path, "size": info.st_size, "modified": info.st_mtime})
                    except FileNotFoundError:
                        pass
            return entries

    def remove_cache_file(self, path):
        path = Path(path)
        with self.lock:
            self._directory()
            if path.parent != self.root or not _NAME.fullmatch(path.name):
                return {"files": 0, "bytes": 0}
            try:
                info = path.lstat()
            except FileNotFoundError:
                return {"files": 0, "bytes": 0}
            if not _ordinary(info):
                return {"files": 0, "bytes": 0}
            with closing(self.library.connect()) as connection, connection:
                connection.execute("""UPDATE library_cover_cache SET generation=generation+1,ready_signature='',cache_file='',
                    cache_etag='',deferred=1,error='',retry_after=0 WHERE cache_file=?""", (path.name,))
                path.unlink(missing_ok=True)
            return {"files": 1, "bytes": info.st_size}

    def clear_cache(self):
        with self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("""UPDATE library_cover_cache SET generation=generation+1,ready_signature='',cache_file='',
                cache_etag='',deferred=1,error='',retry_after=0""")
            self.pending.clear()
            result = {"files": 0, "bytes": 0}
            for entry in self.cache_entries():
                entry["path"].unlink(missing_ok=True)
                result["files"] += 1
                result["bytes"] += entry["size"]
            return result

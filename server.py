#!/usr/bin/env python3
"""Gravelle: a single-person manga and photo reader for a local NAS.

Library files are always read in place. Archives are never extracted, and the
SQLite catalogue and reader state live separately in DATA_PATH.
"""

from __future__ import annotations

import base64
import binascii
from collections import deque
from contextlib import closing
from datetime import date
import fnmatch
import hashlib
import hmac
import ipaddress
from io import BytesIO
from http import HTTPStatus
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import socket
import sqlite3
import stat
import struct
import threading
import time
import unicodedata
import warnings
from urllib.parse import parse_qs, urlsplit
import zipfile

from PIL import Image, ImageOps, UnidentifiedImageError

VERSION = "1.8.3"
MEDIA_TYPES = {"manga_jp", "manga_kr", "comic_us", "photo"}
IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
}
ARCHIVE_TYPES = {".cbz", ".zip"}
MAX_PAGE_BYTES = 64 * 1024 * 1024
MAX_BOOK_BYTES = 8 * 1024 * 1024 * 1024
MAX_PAGES = 10000
MAX_ZIP_ENTRIES = 50000
MAX_ZIP_DIRECTORY_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = 16384
MAX_COVER_UPLOAD_BYTES = 6 * 1024 * 1024
MAX_COVER_BODY_BYTES = 8 * 1024 * 1024 + MAX_BODY_BYTES
MAX_COVER_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_COVER_PIXELS
SESSION_SECONDS = 30 * 24 * 60 * 60
COOKIE_NAME = "sumi_session"
PASSWORD_ITERATIONS = 600_000
STATIC_FILES = {
    "/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
    "/styles.css": "styles.css", "/favicon.svg": "favicon.svg",
    "/favicon.ico": "favicon.ico", "/manifest.webmanifest": "manifest.webmanifest",
    "/sw.js": "sw.js",
    "/reader-model.js": "reader-model.js",
    "/performance-settings.js": "performance-settings.js",
    "/reader-end.js": "reader-end.js",
    "/reader-end.css": "reader-end.css",
    "/gravelle-logo.png": "gravelle-logo.png",
    "/gravelle-wordmark.svg": "gravelle-wordmark.svg",
    "/gravelle-icon.png": "gravelle-icon.png",
    "/default-avatar.png": "default-avatar.png",
    "/avatar-ui.js": "avatar-ui.js",
    "/fonts/LibreCaslonText.ttf": "fonts/LibreCaslonText.ttf",
}
LOG = logging.getLogger("sumi")


def release_version(value):
    """Accept stable three-part tags only; never coerce prerelease/text versions."""
    if not isinstance(value, str):
        raise ValueError("版本号无效。")
    match = re.fullmatch(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})", value)
    if not match:
        raise ValueError("版本号不是受支持的稳定版本。")
    return tuple(int(part) for part in match.groups())


def release_repository(value):
    """A repository identifier is configuration, never a URL or request input."""
    if not isinstance(value, str):
        raise ValueError("发布仓库配置无效，请使用 owner/repo 格式。")
    value = value.strip()
    owner, separator, repository = value.partition("/")
    if (separator != "/" or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", owner)
            or len(owner) > 39 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
            or repository in (".", "..")):
        raise ValueError("发布仓库配置无效，请使用 owner/repo 格式。")
    return value


def release_configuration():
    """Read only operator-controlled environment or the bundled release.json."""
    configured = os.environ.get("GRAVELLE_RELEASE_REPOSITORY", "").strip()
    if not configured:
        try:
            with (Path(__file__).resolve().parent / "release.json").open("rb") as handle:
                contents = handle.read(4097)
            if len(contents) > 4096:
                raise ValueError("发布配置文件过大。")
            document = json.loads(contents)
            if not isinstance(document, dict):
                raise ValueError("发布配置文件无效。")
            configured = document.get("repository", "")
        except FileNotFoundError:
            return None, ""
        except (OSError, ValueError, UnicodeError):
            return None, "发布配置文件无效，请检查服务端 release.json。"
    if configured == "":
        return None, ""
    try:
        return release_repository(configured), ""
    except ValueError as error:
        return None, str(error)


class UpdateCheckError(Exception):
    pass


def latest_github_release(repository, timeout=8):
    """Use a fixed TLS host and API path. HTTPSConnection never follows redirects."""
    repository = release_repository(repository)
    connection = http.client.HTTPSConnection("api.github.com", timeout=timeout)
    try:
        connection.request("GET", f"/repos/{repository}/releases/latest", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "Gravelle-Update-Check/" + VERSION,
        })
        response = connection.getresponse()
        if response.status == 404:
            raise UpdateCheckError("暂未找到公开的稳定发行版，请确认发布仓库和 GitHub Releases。")
        if response.status in (403, 429):
            raise UpdateCheckError("GitHub 暂时限制了检查请求，请稍后重试。")
        if response.status != 200:
            raise UpdateCheckError("GitHub 暂时无法提供发行信息，请稍后重试。")
        maximum = 1024 * 1024
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > maximum):
            raise UpdateCheckError("GitHub 返回的发行信息过大或无效。")
        raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise UpdateCheckError("GitHub 返回的发行信息过大。")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise UpdateCheckError("GitHub 返回的发行信息无效。")
        if document.get("draft") is not False or document.get("prerelease") is not False:
            raise UpdateCheckError("发行信息不是正式稳定版本，已忽略。")
        tag = document.get("tag_name")
        try:
            release_version(tag)
        except ValueError as error:
            raise UpdateCheckError("发行标签不是有效的稳定版本号，已忽略。") from error
        # Do not trust html_url/assets URLs returned by an external JSON document.
        return tag, f"https://github.com/{repository}/releases/tag/{tag}"
    except UpdateCheckError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise UpdateCheckError("无法连接 GitHub 或读取发行信息，请稍后重试。") from error
    finally:
        connection.close()


class UpdateChecker:
    """One read-only check at a time; cache success for 10 minutes, errors for 30s."""
    def __init__(self, current, repository=None, configuration_error=""):
        self.current = current
        self.repository = repository
        self.configuration_error = configuration_error
        self.lock = threading.Lock()
        self.cached = None
        self.expires_at = 0

    def check(self):
        # Holding this lock across the bounded request coalesces simultaneous clicks
        # from different clients, without creating an additional worker per click.
        with self.lock:
            if self.cached is not None and time.monotonic() < self.expires_at:
                return {**self.cached, "cached": True}
            result = {"current": self.current, "latest": None, "status": "unconfigured",
                      "release_url": None, "repository": self.repository,
                      "message": "尚未配置发布仓库，请在服务端配置后再检查。"}
            if self.configuration_error:
                result.update(status="error", message=self.configuration_error)
            elif self.repository:
                try:
                    current = release_version(self.current)
                    tag, release_url = latest_github_release(self.repository)
                    latest = release_version(tag)
                    available = latest > current
                    message = (f"发现新版本 {tag}。请查看发布页面，备份后手动升级。" if available
                               else "当前已是最新版本。" if latest == current
                               else "当前版本高于公开发行版，无需降级。")
                    result.update(latest=".".join(map(str, latest)),
                                  status="available" if available else "up_to_date",
                                  release_url=release_url, message=message)
                except (UpdateCheckError, ValueError) as error:
                    result.update(status="error", message=str(error))
            result["checked_at"] = time.time()
            self.cached = result
            self.expires_at = time.monotonic() + (30 if result["status"] == "error" else 600)
            return {**result, "cached": False}


class ReaderError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def natural_key(value: str) -> tuple:
    # Comparing digit strings avoids both lexical 10-before-2 ordering and huge
    # integer conversions for unusual filenames.
    result = []
    for part in re.split(r"([0-9]+)", value.casefold()):
        if part.isascii() and part.isdigit():
            digits = part.lstrip("0") or "0"
            result.append((1, len(digits), digits, len(part)))
        else:
            result.append((0, part))
    return tuple(result)


def photo_date_prefix(title: str) -> str:
    """Recognize the valid source-folder date used for photo metadata/titles."""
    prefix = re.match(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(?!\d)", title)
    if prefix:
        try:
            return date.fromisoformat(prefix.group(1)).isoformat()
        except ValueError:
            pass
    return ""


def is_hidden(name: str) -> bool:
    return name.startswith(".") or name == "__MACOSX" or name.startswith("@")


def is_link_node(attributes) -> bool:
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def safe_scan_directory(path: Path) -> bool:
    try:
        return not is_link_node(path.lstat())
    except OSError:
        return False


def safe_relative(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or any(p in ("..", "") for p in path.parts):
        raise ReaderError("文件路径无效。", 400)
    if any(is_hidden(p) for p in path.parts):
        raise ReaderError("无法读取隐藏文件。", 404)
    return path.parts


def open_library_file(root: Path, relative: str):
    """Open a regular file without following library symlinks.

    Linux (the Docker runtime) resolves every component with O_NOFOLLOW and
    directory-relative file descriptors, so a concurrent symlink replacement
    cannot redirect a media read outside the mounted library.
    """
    parts = safe_relative(relative)
    if not parts:
        raise ReaderError("图片文件不存在。", 404)
    if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReaderError("只能读取普通图片文件。", 404)
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        attributes = current.lstat()
        if is_link_node(attributes):
            raise ReaderError("出于安全考虑，写真库中的符号链接不会被读取。", 404)
    # All components were checked for Windows symlinks/junctions above. Strict
    # realpath calls are unavailable in some Windows sandbox environments even
    # for readable files, so use the already validated lexical descendant here.
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ReaderError("图片文件不在写真库中。", 404)
    handle = candidate.open("rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise ReaderError("只能读取普通图片文件。", 404)
    return handle


def check_zip_directory(handle) -> None:
    """Bound central-directory allocations before zipfile parses an archive."""
    handle.seek(0, os.SEEK_END)
    length = handle.tell()
    handle.seek(max(0, length - 65557))
    tail = handle.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 22 > len(tail):
        raise ReaderError("压缩包已损坏，找不到 ZIP 目录。", 422)
    _, disk, directory_disk, disk_count, count, directory_size, _, comment_size = (
        struct.unpack_from("<4s4H2LH", tail, offset)
    )
    if offset + 22 + comment_size != len(tail):
        raise ReaderError("压缩包的 ZIP 目录无效。", 422)
    if disk or directory_disk or disk_count != count:
        raise ReaderError("暂不支持分卷 ZIP 压缩包。", 422)
    if count == 65535 or directory_size == 0xFFFFFFFF:
        # ZIP64 locator directly precedes the ordinary end record.
        end_offset = length - len(tail) + offset
        if end_offset < 20:
            raise ReaderError("ZIP64 压缩包目录无效。", 422)
        handle.seek(end_offset - 20)
        locator = handle.read(20)
        if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
            raise ReaderError("ZIP64 压缩包目录无效。", 422)
        _, zip64_disk, zip64_offset, disks = struct.unpack("<4sLQL", locator)
        if zip64_disk or disks != 1:
            raise ReaderError("暂不支持分卷 ZIP64 压缩包。", 422)
        handle.seek(zip64_offset)
        record = handle.read(56)
        if len(record) != 56 or record[:4] != b"PK\x06\x06":
            raise ReaderError("ZIP64 压缩包目录无效。", 422)
        values = struct.unpack("<4sQ2H2L4Q", record)
        if values[4] or values[5] or values[6] != values[7]:
            raise ReaderError("暂不支持分卷 ZIP64 压缩包。", 422)
        count, directory_size = values[7], values[8]
    if count > MAX_ZIP_ENTRIES or directory_size > MAX_ZIP_DIRECTORY_BYTES:
        raise ReaderError("压缩包目录过大，请将写真拆成较小的分卷。", 422)
    handle.seek(0)


def archive_pages(handle) -> list[str]:
    check_zip_directory(handle)
    with zipfile.ZipFile(handle) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ReaderError("压缩包中的文件过多。", 422)
        pages, seen, total = [], set(), 0
        for info in entries:
            if info.is_dir():
                continue
            name = info.filename
            path = PurePosixPath(name.replace("\\", "/"))
            if any(is_hidden(part) for part in path.parts):
                continue
            if path.suffix.lower() not in IMAGE_TYPES:
                continue
            if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in path.parts[0]:
                # Ignore extraction-style paths instead of discarding otherwise
                # readable pages. No ZIP entry is ever written to disk.
                continue
            if name in seen:
                raise ReaderError("压缩包中存在重名图片，请整理后重新扫描。", 422)
            seen.add(name)
            validate_archive_image(info)
            if info.file_size <= 0:
                continue
            total += info.file_size
            if total > MAX_BOOK_BYTES:
                raise ReaderError("压缩包解压后的图片超过 8 GiB，请拆成分卷。", 422)
            pages.append(name)
            if len(pages) > MAX_PAGES:
                raise ReaderError("单本写真超过 10,000 页，请拆成分卷。", 422)
        return sorted(pages, key=natural_key)


def validate_archive_image(info):
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ReaderError("压缩包中的图片不能是符号链接。", 422)
    if info.flag_bits & 1:
        raise ReaderError("暂不支持加密的写真压缩包。", 422)
    # ZIP-LZMA dictionary sizes are controlled by the input and can allocate far
    # more memory than the page itself. Common CBZ STORE/DEFLATE remain supported.
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2):
        raise ReaderError("压缩方式不支持，请将写真重新打包为普通 ZIP 或 CBZ。", 422)
    if info.file_size > MAX_PAGE_BYTES:
        raise ReaderError("单张图片超过 64 MiB，请缩小图片后重新扫描。", 422)
    if info.file_size > 1024 * 1024 and info.file_size / max(1, info.compress_size) > 200:
        raise ReaderError("图片压缩比例异常，已跳过此压缩包。", 422)


class Library:
    def __init__(self, root: Path, data: Path, password: str, scan_interval: float):
        # LIBRARY_PATH is the administrator-controlled browsing boundary. Keep
        # its lexical path so symlinks/junctions can be rejected explicitly.
        self.root = Path(os.path.abspath(root))
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.database = self.data / "library.sqlite3"
        self.password = password
        self.scan_interval = scan_interval
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        self.scanning = False
        self.scan_thread = None
        self.last_scan = None
        self.errors = []
        self.pending_scans = set()
        self.current_scan = None
        self.attempt_lock = threading.Lock()
        self.auth_lock = threading.RLock()
        self.attempts: dict[str, deque] = {}
        self.cover_lock = threading.RLock()
        self.covers = self.data / "covers"
        self.covers.mkdir(exist_ok=True)
        self.avatar_lock = threading.RLock()
        self.avatars = self.data / "avatars"
        self.avatars.mkdir(exist_ok=True)
        self._initialize_database()
        self.signing_key = self._load_secret()

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_database(self):
        with closing(self.connect()) as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, series TEXT NOT NULL,
                    path TEXT NOT NULL, format TEXT NOT NULL, page_count INTEGER NOT NULL,
                    size INTEGER NOT NULL, modified REAL NOT NULL, added_at REAL NOT NULL,
                    pages TEXT NOT NULL, signature TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS progress (
                    book_id TEXT PRIMARY KEY REFERENCES books(id), page INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    book_id TEXT PRIMARY KEY REFERENCES books(id), favorite INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS books_active ON books(active);
                CREATE TABLE IF NOT EXISTS media_libraries (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                    reading_mode TEXT NOT NULL DEFAULT 'single',
                    reading_direction TEXT NOT NULL DEFAULT 'ltr',
                    exclude_rules TEXT NOT NULL DEFAULT '[]',
                    default_author TEXT NOT NULL DEFAULT '',
                    hide_date_prefix INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                    last_scan REAL, errors TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS book_metadata (
                    book_id TEXT PRIMARY KEY REFERENCES books(id),
                    title TEXT, release_date TEXT NOT NULL DEFAULT '',
                    cover_page INTEGER NOT NULL DEFAULT 0,
                    reading_direction_override TEXT,
                    reading_mode_override TEXT,
                    release_date_manual INTEGER NOT NULL DEFAULT 0,
                    models_manual INTEGER NOT NULL DEFAULT 0,
                    folder_model_id TEXT REFERENCES models(id)
                );
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS book_models (
                    book_id TEXT NOT NULL REFERENCES books(id),
                    model_id TEXT NOT NULL REFERENCES models(id),
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(book_id,model_id)
                );
                CREATE INDEX IF NOT EXISTS book_models_model ON book_models(model_id);
                CREATE TABLE IF NOT EXISTS model_folder_names (
                    normalized_name TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL REFERENCES models(id)
                );
                CREATE TABLE IF NOT EXISTS suppressed_model_names (
                    normalized_name TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS bookmarks (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
                    page INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, UNIQUE(book_id,page)
                );
                CREATE TABLE IF NOT EXISTS owner_account (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    username TEXT NOT NULL, normalized_username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, avatar_color TEXT NOT NULL DEFAULT '#ea5098',
                    password_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER REFERENCES owner_account(id),
                    created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at);
            """)
            library_columns = {row["name"] for row in connection.execute("PRAGMA table_info(media_libraries)")}
            if "media_type" not in library_columns:
                connection.execute("ALTER TABLE media_libraries ADD COLUMN media_type TEXT NOT NULL DEFAULT 'manga_jp'")
            if "hide_date_prefix" not in library_columns:
                connection.execute("ALTER TABLE media_libraries ADD COLUMN hide_date_prefix INTEGER NOT NULL DEFAULT 0")
            if "paths" not in library_columns:
                connection.execute("ALTER TABLE media_libraries ADD COLUMN paths TEXT NOT NULL DEFAULT '[]'")
            if "default_author_id" not in library_columns:
                connection.execute("ALTER TABLE media_libraries ADD COLUMN default_author_id TEXT REFERENCES models(id)")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)")}
            if "library_id" not in columns:
                connection.execute("ALTER TABLE books ADD COLUMN library_id TEXT")
                if connection.execute("SELECT 1 FROM books LIMIT 1").fetchone():
                    # Preserve v1 identifiers: progress and favorites reference
                    # these IDs. The namespace applies only to newly added books.
                    legacy_id = secrets.token_hex(12)
                    connection.execute("""INSERT INTO media_libraries
                        (id,name,path,created_at) VALUES(?,?,?,?)""",
                        (legacy_id, "我的漫画", str(self.root), time.time()))
                    connection.execute("UPDATE books SET library_id=?", (legacy_id,))
                    old_scan = connection.execute("SELECT value FROM metadata WHERE key='last_scan'").fetchone()
                    old_errors = connection.execute("SELECT value FROM metadata WHERE key='scan_errors'").fetchone()
                    connection.execute("UPDATE media_libraries SET last_scan=?,errors=? WHERE id=?",
                        (float(old_scan["value"]) if old_scan else None,
                         old_errors["value"] if old_errors else "[]", legacy_id))
            if "scan_revision" not in columns:
                connection.execute("ALTER TABLE books ADD COLUMN scan_revision INTEGER NOT NULL DEFAULT 0")
            if "source_root" not in columns:
                connection.execute("ALTER TABLE books ADD COLUMN source_root TEXT NOT NULL DEFAULT ''")
                connection.execute("""UPDATE books SET source_root=COALESCE(
                    (SELECT path FROM media_libraries WHERE id=books.library_id),'')""")
            if "removed" not in columns:
                connection.execute("ALTER TABLE books ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")
            for table in ("models", "owner_account"):
                avatar_columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "avatar_file" not in avatar_columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN avatar_file TEXT NOT NULL DEFAULT ''")
                if "avatar_revision" not in avatar_columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN avatar_revision INTEGER NOT NULL DEFAULT 0")
            metadata_columns = {row["name"] for row in connection.execute("PRAGMA table_info(book_metadata)")}
            if "release_date_manual" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN release_date_manual INTEGER NOT NULL DEFAULT 0")
                # The old editor submitted even an empty date on every save.
                # Preserve those values because an intentional clear cannot be
                # distinguished from an unset date in the previous schema.
                connection.execute("UPDATE book_metadata SET release_date_manual=1")
            if "models_manual" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN models_manual INTEGER NOT NULL DEFAULT 0")
                connection.execute("INSERT OR IGNORE INTO book_metadata(book_id) SELECT DISTINCT book_id FROM book_models")
                # Old metadata dialogs also submitted empty model selections;
                # keep them as deliberate choices when upgrading.
                connection.execute("UPDATE book_metadata SET models_manual=1")
            if "folder_model_id" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN folder_model_id TEXT REFERENCES models(id)")
            if "models_auto_suppressed" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN models_auto_suppressed INTEGER NOT NULL DEFAULT 0")
            if "reading_mode_override" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN reading_mode_override TEXT")
            if "cover_source" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN cover_source TEXT NOT NULL DEFAULT 'page'")
            if "cover_file" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN cover_file TEXT NOT NULL DEFAULT ''")
            if "cover_revision" not in metadata_columns:
                connection.execute("ALTER TABLE book_metadata ADD COLUMN cover_revision INTEGER NOT NULL DEFAULT 0")
            for row in connection.execute("SELECT id,path,paths,default_author,default_author_id FROM media_libraries").fetchall():
                if not json.loads(row["paths"]):
                    connection.execute("UPDATE media_libraries SET paths=? WHERE id=?",
                                       (json.dumps([row["path"]], ensure_ascii=False), row["id"]))
                if row["default_author"] and not row["default_author_id"]:
                    # Old free-text defaults become one reusable registry entry.
                    author_id = self._folder_model(connection, row["default_author"])
                    connection.execute("UPDATE media_libraries SET default_author_id=? WHERE id=?",
                                       (author_id, row["id"]))
            connection.execute("CREATE INDEX IF NOT EXISTS books_library ON books(library_id,active)")
            for row in connection.execute("SELECT key, value FROM metadata"):
                if row["key"] == "last_scan":
                    self.last_scan = float(row["value"])
                elif row["key"] == "scan_errors":
                    self.errors = json.loads(row["value"])
            connection.commit()

    def _directory_path(self, value):
        if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
            raise ReaderError("请选择有效的写真目录。")
        candidate = Path(value)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ReaderError("目录需要使用挂载范围内的绝对路径，不能包含 ..。")
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise ReaderError("只能选择 LIBRARY_PATH 挂载范围内的目录。", 403) from error
        if candidate == self.data or candidate.is_relative_to(self.data):
            raise ReaderError("不能将阅读器的数据目录设为写真库。", 403)
        current = self.root
        try:
            if is_link_node(current.lstat()) or not current.is_dir():
                raise ReaderError("写真挂载根目录无效，请检查 LIBRARY_PATH。", 422)
            for part in relative.parts:
                if is_hidden(part):
                    raise ReaderError("不能选择隐藏目录。", 403)
                current = current / part
                info = current.lstat()
                if is_link_node(info) or not stat.S_ISDIR(info.st_mode):
                    raise ReaderError("目录不能包含符号链接、联接或非目录项目。", 403)
            # Check permission without creating anything in a read-only mount.
            with os.scandir(candidate):
                pass
        except OSError as error:
            raise ReaderError("目录不存在或无法读取，请检查目录路径和访问权限。", 422) from error
        return candidate

    def directories(self, value=None):
        path = self._directory_path(str(self.root) if value is None else value)
        directories = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if is_hidden(entry.name):
                        continue
                    try:
                        child = self._directory_path(str(path / entry.name))
                    except ReaderError:
                        continue
                    directories.append({"name": entry.name, "path": str(child)})
        except OSError as error:
            raise ReaderError("无法列出此目录，请检查目录访问权限。", 422) from error
        directories.sort(key=lambda item: natural_key(item["name"]))
        return {"root_path": str(self.root), "path": str(path),
                "parent": None if path == self.root else str(path.parent),
                "directories": directories}

    @staticmethod
    def _library_export(row, scanning=False):
        result = {key: row[key] for key in ("id", "name", "path", "media_type", "reading_mode",
                  "reading_direction", "default_author", "default_author_id", "last_scan")}
        result["default_author"] = row["default_author_name"] or row["default_author"]
        result["paths"] = json.loads(row["paths"]) or [row["path"]]
        result["exclude_rules"] = json.loads(row["exclude_rules"])
        result["hide_date_prefix"] = bool(row["hide_date_prefix"])
        result["errors"] = json.loads(row["errors"])
        result["book_count"] = row["book_count"]
        result["scanning"] = scanning
        return result

    def libraries(self):
        with self.state_lock, closing(self.connect()) as connection:
            rows = connection.execute("""SELECT l.*, author.name AS default_author_name, COUNT(b.id) AS book_count
                FROM media_libraries l LEFT JOIN books b ON b.library_id=l.id AND b.active=1
                LEFT JOIN models author ON author.id=l.default_author_id
                GROUP BY l.id ORDER BY l.created_at,l.id""").fetchall()
            libraries = [self._library_export(row, row["id"] == self.current_scan or
                          row["id"] in self.pending_scans) for row in rows]
        return {"libraries": libraries, "root_path": str(self.root)}

    def _library_config(self, library_id, connection=None):
        if not isinstance(library_id, str):
            raise ReaderError("写真库 ID 无效。")
        if connection is None:
            with closing(self.connect()) as own:
                return self._library_config(library_id, own)
        row = connection.execute("""SELECT l.*, author.name AS default_author_name
            FROM media_libraries l LEFT JOIN models author ON author.id=l.default_author_id
            WHERE l.id=?""", (library_id,)).fetchone()
        if row is None:
            raise ReaderError("写真库不存在，请刷新后重试。", 404)
        result = dict(row)
        result["default_author"] = row["default_author_name"] or row["default_author"]
        return result

    def _library_paths(self, value):
        if not isinstance(value, list) or not 1 <= len(value) <= 20:
            raise ReaderError("写真库目录需要是 1 至 20 个目录组成的数组。")
        roots = [self._directory_path(item) for item in value]
        for index, root in enumerate(roots):
            if any(root == other or root.is_relative_to(other) or other.is_relative_to(root)
                   for other in roots[:index]):
                raise ReaderError("同一写真库的目录不能重复，也不能互相包含。")
        return [str(root) for root in roots]

    def save_library(self, body, library_id=None):
        with self.state_lock, closing(self.connect()) as connection, connection:
            previous = self._library_config(library_id, connection) if library_id else {}
            name = body.get("name", previous.get("name", ""))
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
                raise ReaderError("写真库名称需要 1 至 100 个字符。")
            roots = body.get("paths") if "paths" in body else (
                [body["path"]] if "path" in body else json.loads(previous.get("paths", "[]")))
            paths = self._library_paths(roots)
            path = paths[0]
            mode = body.get("reading_mode", previous.get("reading_mode", "single"))
            direction = body.get("reading_direction", previous.get("reading_direction", "ltr"))
            media_type = body.get("media_type", previous.get("media_type", "manga_jp"))
            if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
                raise ReaderError("写真库类型需要是 manga_jp、manga_kr、comic_us 或 photo。")
            if mode not in ("single", "double", "webtoon"):
                raise ReaderError("阅读方式需要是 single、double 或 webtoon。")
            if direction not in ("ltr", "rtl"):
                raise ReaderError("阅读方向需要是 ltr 或 rtl。")
            hide_date_prefix = body.get("hide_date_prefix", bool(previous.get("hide_date_prefix", 0)))
            apply_reading_defaults = body.get("apply_reading_defaults", False)
            if type(hide_date_prefix) is not bool:
                raise ReaderError("隐藏作品名称中的日期需要是布尔值。")
            if type(apply_reading_defaults) is not bool:
                raise ReaderError("将阅读设置应用于全部作品需要是布尔值。")
            author_id = body.get("default_author_id", previous.get("default_author_id"))
            author = previous.get("default_author", "")
            if "default_author_id" in body:
                if author_id is not None and (not isinstance(author_id, str)
                        or not re.fullmatch(r"[a-f0-9]{24}", author_id)):
                    raise ReaderError("默认模特需要选择一位已有模特，或 null 不设置。")
                row = connection.execute("SELECT name FROM models WHERE id=?", (author_id,)).fetchone() if author_id else None
                if author_id and row is None:
                    raise ReaderError("默认模特不存在，请刷新模特列表。")
                author = row["name"] if row else ""
            elif "default_author" in body:
                # Compatibility with earlier clients: one string creates or
                # reuses one model; new clients always send the selected ID.
                author = self._text(body["default_author"], "默认模特", 200, blank=True)
                author_id = self._folder_model(connection, author) if author else None
            rules = body.get("exclude_rules", json.loads(previous.get("exclude_rules", "[]")))
            if (not isinstance(rules, list) or len(rules) > 100
                    or any(not isinstance(rule, str) or len(rule) > 300 or "\0" in rule for rule in rules)):
                raise ReaderError("扫描排除规则需要是文字数组，最多 100 条，每条最多 300 个字符。")
            rules = [rule.strip().replace("\\", "/") for rule in rules
                     if rule.strip() and not rule.strip().startswith("#")]
            if library_id is None:
                library_id = secrets.token_hex(12)
                connection.execute("""INSERT INTO media_libraries
                    (id,name,path,paths,media_type,reading_mode,reading_direction,exclude_rules,default_author,
                     default_author_id,hide_date_prefix,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (library_id, name.strip(), path,
                    json.dumps(paths, ensure_ascii=False), media_type, mode,
                    direction, json.dumps(rules, ensure_ascii=False), author, author_id, hide_date_prefix, time.time()))
            else:
                connection.execute("""UPDATE media_libraries SET name=?,path=?,paths=?,media_type=?,reading_mode=?,
                    reading_direction=?,exclude_rules=?,default_author=?,default_author_id=?,hide_date_prefix=?,
                    revision=revision+1 WHERE id=?""",
                    (name.strip(), path, json.dumps(paths, ensure_ascii=False), media_type, mode, direction,
                     json.dumps(rules, ensure_ascii=False), author, author_id, hide_date_prefix, library_id))
                marks = ",".join("?" for _ in paths)
                # A root removal cannot redirect an old relative page manifest
                # into a different selected root while the rescan is running.
                connection.execute(f"UPDATE books SET active=0 WHERE library_id=? AND source_root NOT IN ({marks})",
                                   [library_id, *paths])
            if apply_reading_defaults:
                # Include temporarily excluded or missing books so they still
                # inherit these defaults when a future scan restores them.
                connection.execute("""UPDATE book_metadata SET reading_mode_override=NULL,
                    reading_direction_override=NULL WHERE book_id IN
                    (SELECT id FROM books WHERE library_id=?)""", (library_id,))
            # Publish the edit and its pending scan together under state_lock;
            # a listing must not observe removed roots before polling is active.
            connection.commit()
            self.start_scan(library_id)
        return {"library": next(item for item in self.libraries()["libraries"] if item["id"] == library_id)}

    @staticmethod
    def _batch_ids(value):
        if (not isinstance(value, list) or not 1 <= len(value) <= 500
                or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{24}", item) for item in value)):
            raise ReaderError("请选择 1 至 500 个有效条目。")
        return list(dict.fromkeys(value))

    @staticmethod
    def _validate_targets(connection, table, ids):
        marks = ",".join("?" for _ in ids)
        active = " AND active=1 AND removed=0" if table == "books" else ""
        rows = connection.execute(f"SELECT id FROM {table} WHERE id IN ({marks}){active}", ids).fetchall()
        if len(rows) != len(ids):
            raise ReaderError("部分所选条目已不存在，请刷新后重试。", 404)

    @staticmethod
    def _clear_book_records(connection, ids):
        # These are reader-owned catalogue/state tables only. Source image
        # files are deliberately never passed to any write/delete function.
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            marks = ",".join("?" for _ in batch)
            for table in ("progress", "favorites", "bookmarks", "book_models", "book_metadata"):
                connection.execute(f"DELETE FROM {table} WHERE book_id IN ({marks})", batch)

    def batch_libraries(self, body):
        ids = self._batch_ids(body.get("ids"))
        action = body.get("action")
        if action not in ("remove", "scan"):
            raise ReaderError("写真库批量操作需要是 remove 或 scan。")
        with self.state_lock, self.cover_lock, closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_targets(connection, "media_libraries", ids)
            if action == "scan":
                connection.commit()
                # Queue all targets while holding state_lock, after validating
                # the complete batch. No missing ID can cause a partial scan.
                scanning = False
                for library_id in ids:
                    scanning = self.start_scan(library_id) or scanning
                return {"scanning": scanning, "ids": ids}
            marks = ",".join("?" for _ in ids)
            book_ids = [row["id"] for row in connection.execute(
                f"SELECT id FROM books WHERE library_id IN ({marks})", ids)]
            self._clear_book_records(connection, book_ids)
            connection.execute(f"DELETE FROM books WHERE library_id IN ({marks})", ids)
            connection.execute(f"DELETE FROM media_libraries WHERE id IN ({marks})", ids)
            connection.commit()
            self.pending_scans.difference_update(ids)
            for book_id in book_ids:
                self._prune_covers(book_id, set())
        return {"removed": ids}

    def batch_books(self, body):
        ids = self._batch_ids(body.get("ids"))
        action = body.get("action")
        if action not in ("remove", "favorite"):
            raise ReaderError("作品批量操作需要是 remove 或 favorite。")
        favorite = body.get("favorite", True)
        if action == "favorite" and type(favorite) is not bool:
            raise ReaderError("收藏状态需要 true 或 false。")
        with self.state_lock, self.cover_lock, closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_targets(connection, "books", ids)
            if action == "favorite":
                connection.executemany("""INSERT INTO favorites(book_id,favorite) VALUES(?,?)
                    ON CONFLICT(book_id) DO UPDATE SET favorite=excluded.favorite""",
                    [(book_id, int(favorite)) for book_id in ids])
                return {"updated": ids, "favorite": favorite}
            self._clear_book_records(connection, ids)
            marks = ",".join("?" for _ in ids)
            # The retained ID/path is a persistent scan exclusion, including
            # while an already running scan is preparing its old snapshot.
            connection.execute(f"UPDATE books SET active=0,removed=1 WHERE id IN ({marks})", ids)
            connection.commit()
            for book_id in ids:
                self._prune_covers(book_id, set())
        return {"removed": ids}

    @staticmethod
    def _excluded(relative, rules, directory=False):
        value = relative.replace("\\", "/").strip("/").casefold()
        basename = value.rsplit("/", 1)[-1]
        for raw in rules:
            pattern = raw.casefold().lstrip("/")
            directories_only = pattern.endswith("/")
            if directories_only and not directory:
                continue
            pattern = pattern.rstrip("/")
            if pattern and (fnmatch.fnmatchcase(value, pattern) or fnmatch.fnmatchcase(basename, pattern)):
                return True
        return False

    def _media_path(self, library_root, relative):
        prefix = library_root.relative_to(self.root)
        return (prefix / Path(*safe_relative(relative))).as_posix()

    def _load_secret(self):
        path = self.data / "session.secret"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            secret = path.read_bytes()
        else:
            secret = secrets.token_bytes(48)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secret)
                handle.flush()
                os.fsync(handle.fileno())
        if len(secret) < 32:
            raise RuntimeError("session.secret 文件无效，请删除该文件后重新启动。")
        # A password change invalidates sessions issued under the old password.
        return hmac.new(secret, self.password.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _public_user(row):
        if row is None:
            return None
        result = {key: row[key] for key in ("id", "username", "display_name", "avatar_color", "avatar_revision")}
        result["avatar_url"] = (f"/api/avatars/user/1?v={row['avatar_revision']}" if row["avatar_file"]
                                else "/default-avatar.png?v=1.8")
        return result

    def owner(self, connection=None):
        if connection is not None:
            return connection.execute("SELECT * FROM owner_account WHERE id=1").fetchone()
        with closing(self.connect()) as connection:
            return self.owner(connection)

    @staticmethod
    def _username(value):
        if not isinstance(value, str):
            raise ReaderError("用户名需要是 2 至 40 个字，可使用文字、数字及 _ . - @。")
        value = unicodedata.normalize("NFKC", value.strip())
        if not 2 <= len(value) <= 40 or not all(
                character.isalnum() or character in "_.-@" for character in value):
            raise ReaderError("用户名需要是 2 至 40 个字，可使用文字、数字及 _ . - @。")
        return value, value.casefold()

    @staticmethod
    def _display_name(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60 or any(
                unicodedata.category(character).startswith("C") for character in value):
            raise ReaderError("显示名称需要是 1 至 60 个字，不能包含控制字符。")
        return value.strip()

    @staticmethod
    def _password(value):
        if not isinstance(value, str) or not 8 <= len(value) <= 256 or any(
                unicodedata.category(character) == "Cs" for character in value):
            raise ReaderError("密码需要为 8 至 256 个字符。")
        return value

    @staticmethod
    def _same_secret(value, expected):
        if not isinstance(value, str) or len(value) > 4096:
            return False
        try:
            return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))
        except UnicodeError:
            return False

    @staticmethod
    def _hash_password(value):
        salt = secrets.token_bytes(24)
        digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, PASSWORD_ITERATIONS)
        return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"

    @staticmethod
    def _check_password(value, encoded):
        if not isinstance(value, str) or len(value) > 256:
            return False
        try:
            algorithm, iterations, salt, digest = encoded.split("$")
            if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
                return False
            expected = bytes.fromhex(digest)
            salt_bytes = bytes.fromhex(salt)
            if len(salt_bytes) != 24 or len(expected) != 32:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt_bytes, int(iterations))
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, UnicodeError):
            return False

    def _session_hash(self, value):
        if not isinstance(value, str) or not 20 <= len(value) <= 256:
            return None
        try:
            return hmac.new(self.signing_key, value.encode("utf-8"), hashlib.sha256).hexdigest()
        except UnicodeError:
            return None

    def make_session(self, user_id=None):
        value = secrets.token_urlsafe(32)
        now = time.time()
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                               (self._session_hash(value), user_id, now, now + SESSION_SECONDS))
            connection.commit()
        return value

    def revoke_session(self, value):
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._session_hash(value),))
            connection.commit()

    def session(self, value):
        with closing(self.connect()) as connection:
            owner = self.owner(connection)
            session = connection.execute("SELECT user_id FROM auth_sessions WHERE token_hash=? AND expires_at>?",
                                         (self._session_hash(value), time.time())).fetchone()
            valid = bool(session and ((owner and session["user_id"] == owner["id"]) or
                                     (not owner and self.password and session["user_id"] is None)))
            return {"authenticated": valid, "registration_required": owner is None,
                    "password_required": owner is None and bool(self.password), "version": VERSION,
                    "user": self._public_user(owner) if valid else None}

    def valid_session(self, value):
        return self.session(value)["authenticated"]

    def register(self, body, address):
        # Serialize the rate-limit check and expensive hash work. The singleton
        # database constraint also protects simultaneous registration processes.
        with self.auth_lock:
            if self.owner() is not None:
                raise ReaderError("已创建个人账户，请使用已有账户登录。", 409)
            self._begin_auth_attempt(address)
            if self.password:
                supplied = body.get("setup_password", "")
                if not self._same_secret(supplied, self.password):
                    raise ReaderError("原访问密码不正确，无法创建账户。", 401)
            username, normalized = self._username(body.get("username"))
            password = self._password(body.get("password"))
            display_name = self._display_name(body.get("display_name", username))
            encoded = self._hash_password(password)
            now = time.time()
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.owner(connection) is not None:
                    raise ReaderError("已创建个人账户，请使用已有账户登录。", 409)
                connection.execute("""INSERT INTO owner_account
                    (id,username,normalized_username,display_name,avatar_color,password_hash,created_at,updated_at)
                    VALUES(1,?,?,?,'#ea5098',?,?,?)""", (username, normalized, display_name, encoded, now, now))
                connection.execute("DELETE FROM auth_sessions")
                connection.commit()
            self.record_login(address, True)
            return 1

    def _begin_auth_attempt(self, address):
        retry = self.login_retry(address)
        if retry:
            raise ReaderError("登录尝试次数过多，请稍后再试。", 429)
        # Count before processing so concurrent failed requests cannot pass the
        # same unrecorded attempt window. A successful attempt clears its window.
        self.record_login(address, False)

    def login(self, body, address):
        with self.auth_lock:
            self._begin_auth_attempt(address)
            owner = self.owner()
            password = body.get("password", "")
            if owner is None:
                if not self.password:
                    raise ReaderError("请先注册个人账户。", 401)
                success = self._same_secret(password, self.password)
            else:
                try:
                    _, normalized = self._username(body.get("username"))
                except ReaderError:
                    normalized = ""
                # Verify the hash even for an unknown username to avoid exposing
                # which username was registered through the cost of the response.
                password_matches = self._check_password(password, owner["password_hash"])
                success = password_matches and hmac.compare_digest(
                    normalized.encode("utf-8"), owner["normalized_username"].encode("utf-8"))
            if not success:
                raise ReaderError("用户名或密码不正确。" if owner else "访问密码不正确。", 401)
            self.record_login(address, True)
            return owner["id"] if owner is not None else None

    def save_profile(self, body, address):
        with self.auth_lock, self.avatar_lock, closing(self.connect()) as connection:
            owner = self.owner(connection)
            if owner is None:
                raise ReaderError("请先注册个人账户后编辑用户信息。", 409)
            username, normalized = self._username(body.get("username", owner["username"]))
            display_name = self._display_name(body.get("display_name", owner["display_name"]))
            color = body.get("avatar_color", owner["avatar_color"])
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ReaderError("请选择有效的头像颜色。")
            password_changed = "password" in body
            encoded = owner["password_hash"]
            if password_changed:
                password = self._password(body["password"])
                self._begin_auth_attempt(address)
                if not self._check_password(body.get("current_password", ""), owner["password_hash"]):
                    raise ReaderError("当前密码不正确。", 401)
                encoded = self._hash_password(password)
                self.record_login(address, True)
            avatar = self._avatar_change(body, "user-1", owner)
            try:
                connection.execute("""UPDATE owner_account SET username=?,normalized_username=?,display_name=?,
                    avatar_color=?,password_hash=?,updated_at=?,avatar_file=?,avatar_revision=? WHERE id=1""",
                    (username, normalized, display_name, color.lower(), encoded, time.time(),
                     avatar[0], avatar[1]))
                if password_changed:
                    connection.execute("DELETE FROM auth_sessions")
                connection.commit()
            except BaseException:
                self._discard_avatar(avatar[2])
                raise
            if avatar[0] != owner["avatar_file"]:
                self._discard_avatar(owner["avatar_file"])
            return password_changed

    def login_retry(self, address):
        now = time.monotonic()
        with self.attempt_lock:
            if len(self.attempts) > 2000:
                self.attempts = {key: value for key, value in self.attempts.items()
                                 if value and now - value[-1] < 300}
            failures = self.attempts.get(address, deque())
            while failures and now - failures[0] >= 300:
                failures.popleft()
            if len(failures) >= 8:
                return max(1, int(301 - (now - failures[0])))
        return 0

    def record_login(self, address, success):
        with self.attempt_lock:
            if success:
                self.attempts.pop(address, None)
            else:
                self.attempts.setdefault(address, deque(maxlen=16)).append(time.monotonic())

    def start_scan(self, library_id=None):
        with self.state_lock, closing(self.connect()) as connection:
            if self.stop_event.is_set():
                return False
            if library_id is not None:
                self._library_config(library_id, connection)
                targets = [library_id]
            else:
                targets = [row["id"] for row in connection.execute("SELECT id FROM media_libraries")]
            self.pending_scans.update(targets)
            if not targets:
                return False
            if not self.scanning:
                self.scanning = True
                self.scan_thread = threading.Thread(target=self._scan, name="library-scan", daemon=True)
                self.scan_thread.start()
            return True

    def start(self):
        self.start_scan()
        self.periodic_thread = threading.Thread(target=self._periodic, name="scan-timer", daemon=True)
        self.periodic_thread.start()

    def _periodic(self):
        if self.scan_interval <= 0:
            return
        while not self.stop_event.wait(self.scan_interval):
            self.start_scan()

    def close(self):
        self.stop_event.set()
        if self.scan_thread:
            self.scan_thread.join(timeout=10)

    def _scan(self):
        try:
            while not self.stop_event.is_set():
                with self.state_lock:
                    if not self.pending_scans:
                        self.current_scan = None
                        self.scanning = False
                        return
                    library_id = sorted(self.pending_scans)[0]
                    self.pending_scans.remove(library_id)
                    self.current_scan = library_id
                try:
                    config = self._library_config(library_id)
                    self._scan_library(config)
                except ReaderError as error:
                    if error.status != 404:
                        LOG.exception("Media library scan failed")
                except Exception:
                    LOG.exception("Media library scan failed")
        finally:
            with self.state_lock:
                if self.scan_thread is threading.current_thread():
                    self.current_scan = None
                    self.scanning = False

    def _scan_library(self, config):
        library_id, revision = config["id"], config["revision"]
        errors, found, unreadable = [], [], []
        rules = json.loads(config["exclude_rules"])
        roots = json.loads(config["paths"]) or [config["path"]]

        def report(path, message):
            if len(errors) < 200:
                errors.append({"path": path, "message": str(message)})

        def on_walk_error(error):
            try:
                path = Path(error.filename).relative_to(root).as_posix()
            except (TypeError, ValueError):
                path = "."
            unreadable.append((str(root), path))
            report(path, "无法读取此目录，请检查目录访问权限。")

        def entry_excluded(name):
            parts = PurePosixPath(name).parts
            return self._excluded(name, rules) or any(
                self._excluded("/".join(parts[:index]), rules, directory=True)
                for index in range(1, len(parts)))

        try:
            with closing(self.connect()) as connection:
                cached = {row["id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM books WHERE library_id=?", (library_id,))}
            previous_ids = {(self._source_identity(Path(row["source_root"]), row["path"]),
                             "folder" if row["format"] == "folder" else "archive"): row["id"]
                            for row in cached.values()}
            for root_value in roots:
                try:
                    root = self._directory_path(root_value)
                except ReaderError as error:
                    unreadable.append((root_value, "."))
                    report(root_value, str(error))
                    continue
                for directory, children, filenames in os.walk(root, topdown=True,
                                                             onerror=on_walk_error, followlinks=False):
                    if self.stop_event.is_set():
                        return
                    folder = Path(directory)
                    # A selected root may contain DATA_PATH; never catalogue our
                    # own database or anything below that directory.
                    children[:] = sorted((name for name in children if not is_hidden(name)
                        and safe_scan_directory(folder / name)
                        and not (folder / name == self.data or (folder / name).is_relative_to(self.data))
                        and not self._excluded((folder / name).relative_to(root).as_posix(), rules, directory=True)),
                        key=natural_key)
                    image_files = []
                    for name in sorted(filenames, key=natural_key):
                        if is_hidden(name):
                            continue
                        candidate = folder / name
                        suffix = candidate.suffix.lower()
                        if suffix not in IMAGE_TYPES and suffix not in ARCHIVE_TYPES:
                            continue
                        relative = candidate.relative_to(root).as_posix()
                        if self._excluded(relative, rules):
                            continue
                        try:
                            info = candidate.lstat()
                            if not stat.S_ISREG(info.st_mode) or is_link_node(info):
                                continue
                            if suffix in IMAGE_TYPES:
                                if 0 < info.st_size <= MAX_PAGE_BYTES:
                                    image_files.append((relative, info))
                                else:
                                    report(relative, "图片为空或超过 64 MiB，已跳过。")
                            else:
                                book_id = previous_ids.get((self._source_identity(root, relative), "archive")) or self._book_id("source\0" + self._media_path(root, relative), "archive", library_id)
                                signature = f"{info.st_size}:{info.st_mtime_ns}"
                                previous = cached.get(book_id)
                                if (previous and previous["signature"] == signature
                                        and previous["scan_revision"] == revision):
                                    pages = json.loads(previous["pages"])
                                else:
                                    with open_library_file(self.root, self._media_path(root, relative)) as handle:
                                        pages = archive_pages(handle)
                                pages = [page for page in pages if not entry_excluded(page)]
                                if pages:
                                    found.append(self._book(config, root, book_id, candidate.stem, folder, relative,
                                                            suffix[1:], pages, info.st_size,
                                                            info.st_mtime, signature))
                                else:
                                    report(relative, "压缩包中没有支持的图片，或图片已被排除规则过滤。")
                        except (OSError, ValueError, RuntimeError, NotImplementedError,
                                zipfile.BadZipFile, ReaderError) as error:
                            message = str(error) if isinstance(error, ReaderError) else "无法读取写真，请检查文件是否损坏或权限是否正确。"
                            report(relative, message)
                    if image_files:
                        relative = folder.relative_to(root).as_posix()
                        if len(image_files) > MAX_PAGES:
                            report(relative, "此目录超过 10,000 张图片，请按章节拆分目录。")
                            continue
                        pages = [entry[0] for entry in image_files]
                        signature = hashlib.sha256(json.dumps(
                            [(name, info.st_size, info.st_mtime_ns) for name, info in image_files],
                            ensure_ascii=False).encode()).hexdigest()
                        book_id = previous_ids.get((self._source_identity(root, relative), "folder")) or self._book_id("source\0" + self._media_path(root, relative), "folder", library_id)
                        found.append(self._book(config, root, book_id, folder.name,
                                                folder.parent, relative, "folder", pages,
                                                sum(entry[1].st_size for entry in image_files),
                                                max(entry[1].st_mtime for entry in image_files), signature))
            if self.stop_event.is_set():
                return
            scanned_at = time.time()
            with self.state_lock, closing(self.connect()) as connection, connection:
                if connection.execute("SELECT 1 FROM media_libraries WHERE id=?", (library_id,)).fetchone() is None:
                    return
                current = self._library_config(library_id, connection)
                if current["revision"] != revision:
                    self.pending_scans.add(library_id)
                    return
                connection.execute("UPDATE books SET active=0 WHERE library_id=?", (library_id,))
                for row in cached.values():
                    if row["active"] and any(
                            row["source_root"] == source_root and (
                            prefix == "." or row["path"] == prefix or row["path"].startswith(prefix + "/"))
                            for source_root, prefix in unreadable):
                        connection.execute("UPDATE books SET active=1 WHERE id=? AND removed=0", (row["id"],))
                removed = {row["id"] for row in connection.execute(
                    "SELECT id FROM books WHERE library_id=? AND removed=1", (library_id,))}
                for book in found:
                    if book["id"] in removed:
                        continue
                    connection.execute("""
                        INSERT INTO books
                        (id,title,series,path,format,page_count,size,modified,added_at,pages,signature,active,
                         library_id,scan_revision,source_root)
                        VALUES (:id,:title,:series,:path,:format,:page_count,:size,:modified,:added_at,:pages,
                                :signature,1,:library_id,:scan_revision,:source_root)
                        ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,series=excluded.series,path=excluded.path,
                        format=excluded.format,page_count=excluded.page_count,size=excluded.size,
                        modified=excluded.modified,pages=excluded.pages,signature=excluded.signature,
                        source_root=excluded.source_root,scan_revision=excluded.scan_revision,active=1
                    """, book)
                    self._apply_photo_defaults(connection, config, Path(book["source_root"]), book)
                    connection.execute("UPDATE progress SET page=MIN(page, ?) WHERE book_id=?",
                                       (book["page_count"] - 1, book["id"]))
                connection.execute("UPDATE media_libraries SET last_scan=?,errors=? WHERE id=?",
                    (scanned_at, json.dumps(errors, ensure_ascii=False), library_id))
            LOG.info("Media library %s scan finished: %d books, %d warnings", config["name"], len(found), len(errors))
        except Exception as error:
            LOG.exception("Media library %s scan failed", config["name"])
            message = str(error) if isinstance(error, ReaderError) else "扫描失败，请检查写真库挂载、目录权限和存储空间。"
            with self.state_lock, closing(self.connect()) as connection, connection:
                if connection.execute("SELECT 1 FROM media_libraries WHERE id=?", (library_id,)).fetchone() is None:
                    return
                if self._library_config(library_id, connection)["revision"] == revision:
                    connection.execute("UPDATE media_libraries SET errors=? WHERE id=?",
                        (json.dumps([{"path": ".", "message": message}], ensure_ascii=False), library_id))

    @staticmethod
    def _source_identity(root, relative):
        return os.path.normcase(os.path.abspath(root.joinpath(*PurePosixPath(relative).parts)))

    @staticmethod
    def _book_id(path, kind, library_id=None):
        namespace = "" if library_id is None else library_id + "\0"
        return hashlib.sha256((namespace + kind + "\0" + path).encode("utf-8")).hexdigest()[:24]

    def _book(self, config, root, book_id, title, parent, path, format_name, pages, size, modified, signature):
        series = "未分类" if parent == root or not parent.is_relative_to(root) else parent.name
        return dict(id=book_id, title=title, series=series, path=path, format=format_name,
                    page_count=len(pages), size=size, modified=modified, added_at=time.time(),
                    pages=json.dumps(pages, ensure_ascii=False), signature=signature,
                    library_id=config["id"], scan_revision=config["revision"], source_root=str(root))

    @staticmethod
    def _photo_folder_values(root, book):
        """Infer only the supported model/work and selected-model/work layouts."""
        release_date = photo_date_prefix(book["title"])
        parts = PurePosixPath(book["path"]).parts
        model_name = root.name if len(parts) == 1 else parts[0] if len(parts) == 2 else ""
        # Some collections decorate just one side of the model directory,
        # such as "【模特甲". Strip only edge ornaments, never interior text.
        while True:
            cleaned = model_name.strip().strip("【】[]（）()「」『』").strip()
            if cleaned == model_name:
                break
            model_name = cleaned
        if (not model_name or len(model_name) > 100
                or any(unicodedata.category(char) == "Cc" for char in model_name)):
            model_name = ""
        return release_date, model_name

    @staticmethod
    def _folder_model(connection, name, *, automatic=False):
        normalized = unicodedata.normalize("NFKC", name).casefold()
        if automatic:
            if connection.execute("SELECT 1 FROM suppressed_model_names WHERE normalized_name=?", (normalized,)).fetchone():
                return None
        else:
            # Explicitly creating/selecting a name is an intentional way to
            # restore it; ordinary scans must respect an earlier deletion.
            connection.execute("DELETE FROM suppressed_model_names WHERE normalized_name=?", (normalized,))
        # A directory keeps pointing to its model after the display name is
        # edited. Resolving aliases first also covers new works in that folder.
        existing = connection.execute("SELECT model_id AS id FROM model_folder_names WHERE normalized_name=?",
                                      (normalized,)).fetchone()
        if existing:
            return existing["id"]
        existing = connection.execute("SELECT id FROM models WHERE normalized_name=?", (normalized,)).fetchone()
        model_id = existing["id"] if existing else secrets.token_hex(12)
        if existing is None:
            connection.execute("INSERT INTO models(id,name,normalized_name) VALUES(?,?,?)",
                               (model_id, name, normalized))
        connection.execute("INSERT INTO model_folder_names(normalized_name,model_id) VALUES(?,?)",
                           (normalized, model_id))
        return model_id

    def _apply_photo_defaults(self, connection, config, root, book):
        previous = connection.execute("SELECT * FROM book_metadata WHERE book_id=?", (book["id"],)).fetchone()
        if config["media_type"] != "photo" and previous is None:
            return
        release_date, model_name = self._photo_folder_values(root, book) if config["media_type"] == "photo" else ("", "")
        manual = bool(previous and previous["models_manual"])
        suppressed = bool(previous and previous["models_auto_suppressed"])
        default_model = config.get("default_author_id")
        # A chosen default and manual work bindings take precedence. In those
        # cases even creating a folder-derived registry entry would be wrong.
        model_id = (self._folder_model(connection, model_name, automatic=True)
                    if model_name and not default_model and not manual and not suppressed else None)
        if manual and not default_model and not suppressed:
            # Keep an already resolved folder label for compatibility without
            # creating any new registry entry behind a manual work selection.
            model_id = previous["folder_model_id"]
        connection.execute("INSERT OR IGNORE INTO book_metadata(book_id) VALUES(?)", (book["id"],))
        connection.execute("UPDATE book_metadata SET folder_model_id=? WHERE book_id=?", (model_id, book["id"]))
        connection.execute("UPDATE book_metadata SET release_date=? WHERE book_id=? AND release_date_manual=0",
                           (release_date, book["id"]))
        metadata = connection.execute("SELECT models_manual FROM book_metadata WHERE book_id=?", (book["id"],)).fetchone()
        if not metadata["models_manual"]:
            connection.execute("DELETE FROM book_models WHERE book_id=?", (book["id"],))
            selected_model = default_model or model_id
            if selected_model is not None:
                connection.execute("INSERT INTO book_models(book_id,model_id,position) VALUES(?,?,0)",
                                   (book["id"], selected_model))

    @staticmethod
    def _export(row, models=None):
        book = {key: row[key] for key in ("id", "title", "series", "path", "format",
                                          "page_count", "size", "modified", "added_at",
                                          "library_id", "library_name", "author",
                                          "media_type", "reading_mode", "reading_direction",
                                          "release_date", "reading_direction_override",
                                          "reading_mode_override", "source_root", "cover_source")}
        book["title"] = row["title_override"] or row["title"]
        book["source_title"] = row["title"]
        book["title_override"] = row["title_override"]
        if not row["title_override"] and row["media_type"] == "photo" and row["hide_date_prefix"]:
            prefix = photo_date_prefix(row["title"])
            if prefix:
                book["title"] = row["title"][len(prefix):].lstrip() or row["title"]
        book["cover_page"] = min(max(row["cover_page"], 0), max(row["page_count"] - 1, 0))
        book["cover_version"] = Library._cover_version(row)
        if book["reading_mode"] == "webtoon":
            book["reading_direction"] = "ttb"
        book["models"] = models or []
        book["favorite"] = bool(row["favorite"])
        book["progress"] = None if row["page"] is None else {
            "page": row["page"], "completed": bool(row["completed"]), "updated_at": row["updated_at"]}
        return book

    def _select(self, connection, book_id=None, library_id=None):
        sql = """SELECT b.*, p.page, p.completed, p.updated_at, COALESCE(f.favorite,0) AS favorite,
                 l.name AS library_name, l.path AS library_path,
                 CASE WHEN l.media_type='photo' THEN COALESCE(library_author.name,folder_model.name,l.default_author)
                      ELSE COALESCE(library_author.name,l.default_author) END AS author,
                 l.media_type, COALESCE(m.reading_mode_override,l.reading_mode) AS reading_mode,
                 COALESCE(m.reading_direction_override,l.reading_direction) AS reading_direction,
                 m.title AS title_override, COALESCE(m.release_date,'') AS release_date,
                 COALESCE(m.cover_page,0) AS cover_page, m.reading_direction_override,
                 m.reading_mode_override, l.hide_date_prefix,
                 COALESCE(m.cover_source,'page') AS cover_source,
                 COALESCE(m.cover_file,'') AS cover_file, COALESCE(m.cover_revision,0) AS cover_revision
                 FROM books b LEFT JOIN progress p ON p.book_id=b.id
                 LEFT JOIN favorites f ON f.book_id=b.id
                 LEFT JOIN book_metadata m ON m.book_id=b.id
                 LEFT JOIN models folder_model ON folder_model.id=m.folder_model_id
                 JOIN media_libraries l ON l.id=b.library_id
                 LEFT JOIN models library_author ON library_author.id=l.default_author_id WHERE b.active=1"""
        if book_id is not None:
            return connection.execute(sql + " AND b.id=?", (book_id,)).fetchone()
        if library_id is not None:
            return connection.execute(sql + " AND b.library_id=?", (library_id,)).fetchall()
        return connection.execute(sql).fetchall()

    def book(self, book_id, internal=False):
        with closing(self.connect()) as connection:
            row = self._select(connection, book_id)
            if row is None:
                raise ReaderError("作品不存在，可能已被移动，请重新扫描写真库。", 404)
            if internal:
                return dict(row)
            book = self._export(row, self._models_for_books(connection, [book_id]).get(book_id))
            book["bookmarks"] = self._bookmarks(connection, book_id)
            return book

    @staticmethod
    def _models_for_books(connection, book_ids):
        result = {}
        # Keep statement parameters below SQLite builds with a 999-variable
        # limit, including installations with a large personal collection.
        for offset in range(0, len(book_ids), 500):
            batch = book_ids[offset:offset + 500]
            marks = ",".join("?" for _ in batch)
            rows = connection.execute(f"""SELECT bm.book_id,m.id,m.name,m.avatar_file,m.avatar_revision,
                (SELECT MIN(other.id) FROM books other JOIN book_models obm ON obm.book_id=other.id
                 WHERE obm.model_id=m.id AND other.active=1) AS cover_book_id FROM book_models bm
                JOIN models m ON m.id=bm.model_id WHERE bm.book_id IN ({marks})
                ORDER BY bm.book_id,bm.position,m.id""", batch)
            for row in rows:
                model = {key: row[key] for key in ("id", "name", "avatar_revision")}
                model["avatar_url"] = Library._model_avatar_url(row)
                result.setdefault(row["book_id"], []).append(model)
        return result

    def listing(self, library_id=None):
        # The catalogue and its scanning flag are one snapshot. Otherwise a
        # scan can publish between the row query and the libraries query,
        # returning old rows with scanning=False and ending client polling.
        with self.state_lock, closing(self.connect()) as connection:
            if library_id is not None:
                self._library_config(library_id, connection)
            rows = self._select(connection, library_id=library_id)
            models = self._models_for_books(connection, [row["id"] for row in rows])
            books = [self._export(row, models.get(row["id"])) for row in rows]
            libraries = self.libraries()["libraries"]
        books.sort(key=lambda book: (natural_key(book["title"]), natural_key(book["series"]), book["path"]))
        if library_id is not None:
            libraries = [item for item in libraries if item["id"] == library_id]
        scans = [item["last_scan"] for item in libraries if item["last_scan"] is not None]
        errors = [{**error, "library_id": item["id"], "library_name": item["name"]}
                  for item in libraries for error in item["errors"]]
        return dict(books=books, scanning=any(item["scanning"] for item in libraries),
                    last_scan=max(scans) if scans else None, errors=errors)

    @staticmethod
    def _text(value, label, maximum, blank=False, multiline=False):
        if not isinstance(value, str):
            raise ReaderError(f"{label}需要是文字。")
        value = value.strip()
        if len(value) > maximum or (not blank and not value):
            raise ReaderError(f"{label}需要{'不超过' if blank else ' 1 至 '}{maximum}个字符。")
        if any(unicodedata.category(char) == "Cc" and not (multiline and char in "\n\r\t")
               for char in value):
            raise ReaderError(f"{label}不能包含控制字符。")
        return value

    @staticmethod
    def _cover_version(book):
        value = f"{book['signature']}:{book['cover_source']}:{book['cover_page']}:{book['cover_revision']}"
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    @staticmethod
    def _thumbnail(data):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    if image.format not in ("JPEG", "PNG", "WEBP", "GIF", "AVIF"):
                        raise ReaderError("封面只支持 JPG、PNG、WebP、GIF 或 AVIF 图片。", 422)
                    if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_COVER_PIXELS:
                        raise ReaderError("封面图片不能超过 4000 万像素。", 422)
                    # JPEG can decode at a reduced resolution; readers still
                    # receive their untouched original page from /pages/{n}.
                    image.draft("RGB", (800, 1200))
                    image.seek(0)
                    image.load()
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((400, 600), Image.Resampling.LANCZOS)
                    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        rgb = Image.new("RGB", image.size, "white")
                        rgb.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        rgb = image.convert("RGB")
                    output = BytesIO()
                    rgb.save(output, "JPEG", quality=82, optimize=True)
                    return output.getvalue()
        except ReaderError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ReaderError("封面图片损坏、不受支持或像素过大，请选择另一张图片。", 422) from error

    def _decode_cover_upload(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("data"), str):
            raise ReaderError("上传封面需要包含 base64 编码的 data。")
        encoded = value["data"]
        if not encoded or len(encoded) > 4 * ((MAX_COVER_UPLOAD_BYTES + 2) // 3):
            raise ReaderError("上传封面不能超过 6 MiB。", 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ReaderError("上传封面的 base64 数据无效。") from error
        if not data or len(data) > MAX_COVER_UPLOAD_BYTES:
            raise ReaderError("上传封面不能为空，且不能超过 6 MiB。", 413)
        return self._thumbnail(data)

    @staticmethod
    def _decode_avatar_upload(value):
        mime = None
        if isinstance(value, str):
            match = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", value)
            if match is None:
                raise ReaderError("头像需要是 JPG、PNG 或 WebP 图片的 data URL。")
            mime, encoded = match.groups()
        elif isinstance(value, dict) and isinstance(value.get("data"), str):
            encoded = value["data"]
            mime = value.get("mime")
            if mime is not None and mime not in ("image/jpeg", "image/png", "image/webp"):
                raise ReaderError("头像只支持 JPG、PNG 或 WebP 图片。", 422)
        else:
            raise ReaderError("上传头像需要包含 base64 编码的图片。")
        if not encoded or len(encoded) > 4 * ((MAX_COVER_UPLOAD_BYTES + 2) // 3):
            raise ReaderError("上传头像不能为空，且不能超过 6 MiB。", 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ReaderError("上传头像的 base64 数据无效。") from error
        if not data or len(data) > MAX_COVER_UPLOAD_BYTES:
            raise ReaderError("上传头像不能为空，且不能超过 6 MiB。", 413)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as source:
                    formats = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
                    if source.format not in formats or (mime and formats[source.format] != mime):
                        raise ReaderError("头像的格式无效，只支持 JPG、PNG 或 WebP 图片。", 422)
                    if source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_COVER_PIXELS:
                        raise ReaderError("头像图片不能超过 4000 万像素。", 422)
                    source.draft("RGB", (512, 512))
                    source.seek(0)
                    source.load()
                    image = ImageOps.exif_transpose(source)
                    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        image = image.convert("RGB")
                    image = ImageOps.fit(image, (256, 256), Image.Resampling.LANCZOS)
                    output = BytesIO()
                    image.save(output, "JPEG", quality=85, optimize=True)
                    return output.getvalue()
        except ReaderError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ReaderError("头像图片损坏、不受支持或像素过大，请选择另一张图片。", 422) from error

    def _avatar_change(self, body, prefix, previous, decoded=None):
        reset = body.get("avatar_reset", False)
        if type(reset) is not bool:
            raise ReaderError("恢复默认头像需要 true 或 false。")
        if reset and "avatar_upload" in body:
            raise ReaderError("上传头像和恢复默认头像不能同时提交。")
        if "avatar_upload" not in body and not reset:
            return previous["avatar_file"], previous["avatar_revision"], None
        revision = previous["avatar_revision"] + 1
        if reset:
            return "", revision, None
        data = decoded if decoded is not None else self._decode_avatar_upload(body["avatar_upload"])
        name = prefix + "-" + secrets.token_hex(12) + ".jpg"
        temporary = self.avatars / (".tmp-" + secrets.token_hex(12))
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.avatars / name)
        finally:
            temporary.unlink(missing_ok=True)
        return name, revision, name

    def _discard_avatar(self, name):
        if name and re.fullmatch(r"(?:user-1|model-[a-f0-9]{24})-[a-f0-9]{24}\.jpg", name):
            try:
                (self.avatars / name).unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not remove obsolete avatar %s", name)

    def avatar(self, kind, identifier):
        with self.avatar_lock, closing(self.connect()) as connection:
            if kind == "user" and identifier == "1":
                row = self.owner(connection)
                prefix = "user-1"
            elif kind == "models":
                row = connection.execute("SELECT * FROM models WHERE id=?", (identifier,)).fetchone()
                prefix = "model-" + identifier
            else:
                row = None
            if row is None or not row["avatar_file"]:
                raise ReaderError("自定义头像不存在。", 404)
            name = row["avatar_file"]
            if not re.fullmatch(re.escape(prefix) + r"-[a-f0-9]{24}\.jpg", name):
                raise ReaderError("头像文件无效，请重新上传。", 422)
            try:
                path = self.avatars / name
                attributes = path.lstat()
                if not stat.S_ISREG(attributes.st_mode) or is_link_node(attributes) or attributes.st_size > MAX_COVER_UPLOAD_BYTES:
                    raise ReaderError("头像文件无效，请重新上传。", 422)
                data = path.read_bytes()
            except OSError as error:
                raise ReaderError("头像文件不存在，请重新上传。", 404) from error
            return data, "image/jpeg", '"' + hashlib.sha256(data).hexdigest() + '"'

    def _write_cover(self, name, data):
        temporary = self.covers / (".tmp-" + secrets.token_hex(12))
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.covers / name)
        finally:
            temporary.unlink(missing_ok=True)

    def _prune_covers(self, book_id, keep):
        # Only remove files created by this reader, never arbitrary /data files.
        for candidate in self.covers.glob(book_id + "-*.jpg"):
            if candidate.name not in keep and re.fullmatch(
                    re.escape(book_id) + r"-(?:page|upload)-[a-f0-9]{24}\.jpg", candidate.name):
                try:
                    candidate.unlink()
                except OSError:
                    LOG.warning("Could not remove obsolete cover %s", candidate.name)

    def save_book_metadata(self, book_id, body):
        with self.cover_lock, closing(self.connect()) as connection, connection:
            book = self._select(connection, book_id)
            if book is None:
                raise ReaderError("作品不存在，请重新扫描写真库。", 404)
            updates = {}
            if "title" in body:
                value = body["title"]
                updates["title"] = None if value is None else self._text(value, "作品名称", 200, blank=True) or None
            if "release_date" in body:
                value = body["release_date"]
                if not isinstance(value, str):
                    raise ReaderError("发布日期需要是 YYYY-MM-DD 或空字符串。")
                if value:
                    try:
                        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                            raise ValueError()
                        date.fromisoformat(value)
                    except ValueError as error:
                        raise ReaderError("发布日期需要是有效的 YYYY-MM-DD 日期。") from error
                updates["release_date"] = value
                updates["release_date_manual"] = 1
            if "cover_page" in body:
                value = body["cover_page"]
                if type(value) is not int or not 0 <= value < book["page_count"]:
                    raise ReaderError("封面页码需要在作品的页码范围内。")
                updates["cover_page"] = value
            cover_source = body.get("cover_source")
            if "cover_source" in body and cover_source not in ("page", "upload"):
                raise ReaderError("封面来源需要是 page 或 upload。")
            if "cover_upload" in body and cover_source == "page":
                raise ReaderError("上传封面时不能同时选择作品内页封面。")
            if cover_source == "upload" and "cover_upload" not in body and book["cover_source"] != "upload":
                raise ReaderError("请先上传封面图片。")
            if "reading_direction" in body:
                value = body["reading_direction"]
                if value not in (None, "ltr", "rtl"):
                    raise ReaderError("阅读方向需要是 ltr、rtl，或 null 跟随写真库。")
                updates["reading_direction_override"] = value
            if "reading_mode" in body:
                value = body["reading_mode"]
                if value not in (None, "", "single", "double", "webtoon"):
                    raise ReaderError("阅读方式需要是 single、double、webtoon，或 null 跟随写真库。")
                updates["reading_mode_override"] = value or None
            model_ids = None
            if "model_ids" in body:
                value = body["model_ids"]
                if (not isinstance(value, list) or len(value) > 100
                        or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{24}", item) for item in value)):
                    raise ReaderError("模特需要是最多 100 个有效模特 ID 组成的数组。")
                model_ids = list(dict.fromkeys(value))
                if model_ids:
                    marks = ",".join("?" for _ in model_ids)
                    count = connection.execute(f"SELECT COUNT(*) FROM models WHERE id IN ({marks})", model_ids).fetchone()[0]
                    if count != len(model_ids):
                        raise ReaderError("所选模特不存在，请刷新模特列表。")
                updates["models_manual"] = 1
            upload_name = None
            if "cover_upload" in body:
                thumbnail = self._decode_cover_upload(body["cover_upload"])
                upload_name = book_id + "-upload-" + secrets.token_hex(12) + ".jpg"
                self._write_cover(upload_name, thumbnail)
                updates.update(cover_source="upload", cover_file=upload_name,
                               cover_revision=book["cover_revision"] + 1)
            elif cover_source == "page" or ("cover_page" in body and cover_source is None):
                if (book["cover_source"] != "page" or book["cover_file"]
                        or updates.get("cover_page", book["cover_page"]) != book["cover_page"]):
                    updates.update(cover_source="page", cover_file="",
                                   cover_revision=book["cover_revision"] + 1)
            if updates:
                try:
                    connection.execute("INSERT OR IGNORE INTO book_metadata(book_id) VALUES(?)", (book_id,))
                    fields = ",".join(key + "=?" for key in updates)
                    connection.execute(f"UPDATE book_metadata SET {fields} WHERE book_id=?", [*updates.values(), book_id])
                    if model_ids is not None:
                        connection.execute("DELETE FROM book_models WHERE book_id=?", (book_id,))
                        connection.executemany("INSERT INTO book_models(book_id,model_id,position) VALUES(?,?,?)",
                            [(book_id, model_id, index) for index, model_id in enumerate(model_ids)])
                    connection.commit()
                except BaseException:
                    if upload_name:
                        (self.covers / upload_name).unlink(missing_ok=True)
                    raise
                if "cover_source" in updates:
                    self._prune_covers(book_id, {updates["cover_file"]})
        return {"book": self.book(book_id)}

    @staticmethod
    def _model_avatar_url(row):
        if row["avatar_file"]:
            return f"/api/avatars/models/{row['id']}?v={row['avatar_revision']}"
        if row["cover_book_id"]:
            return f"/api/books/{row['cover_book_id']}/cover"
        return None

    @staticmethod
    def _models(connection, model_id=None):
        sql = """SELECT m.id,m.name,m.avatar_file,m.avatar_revision,COUNT(b.id) AS book_count,MIN(b.id) AS cover_book_id
                 FROM models m LEFT JOIN book_models bm ON bm.model_id=m.id
                 LEFT JOIN books b ON b.id=bm.book_id AND b.active=1"""
        rows = connection.execute(sql + (" WHERE m.id=?" if model_id is not None else "")
                                  + " GROUP BY m.id", (model_id,) if model_id is not None else ()).fetchall()
        result = []
        for row in rows:
            model = {key: row[key] for key in ("id", "name", "book_count", "cover_book_id", "avatar_revision")}
            model["avatar_url"] = Library._model_avatar_url(row)
            result.append(model)
        return result

    def models(self):
        with closing(self.connect()) as connection:
            models = self._models(connection)
        models.sort(key=lambda model: natural_key(model["name"]))
        return {"models": models}

    def save_model(self, body, model_id=None):
        with self.avatar_lock, closing(self.connect()) as connection, connection:
            previous = connection.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone() if model_id is not None else None
            if model_id is not None and previous is None:
                raise ReaderError("模特不存在，请刷新后重试。", 404)
            name = self._text(body.get("name", previous["name"] if previous else None), "模特名称", 100)
            normalized = unicodedata.normalize("NFKC", name).casefold()
            # Decode before any changes so a malformed avatar cannot partly
            # create or rename a model. Reusing an existing name does not edit it.
            decoded = self._decode_avatar_upload(body["avatar_upload"]) if "avatar_upload" in body else None
            if "avatar_reset" in body and type(body["avatar_reset"]) is not bool:
                raise ReaderError("恢复默认头像需要 true 或 false。")
            if body.get("avatar_reset") and "avatar_upload" in body:
                raise ReaderError("上传头像和恢复默认头像不能同时提交。")
            existing = connection.execute("SELECT id FROM models WHERE normalized_name=?", (normalized,)).fetchone()
            if existing and existing["id"] != model_id:
                if model_id is not None:
                    raise ReaderError("已有同名模特，请使用不同名称。", 409)
                connection.execute("DELETE FROM suppressed_model_names WHERE normalized_name=?", (normalized,))
                return {"model": self._models(connection, existing["id"])[0]}, 200
            if model_id is None:
                model_id = secrets.token_hex(12)
                try:
                    connection.execute("INSERT INTO models(id,name,normalized_name) VALUES(?,?,?)", (model_id, name, normalized))
                except sqlite3.IntegrityError:
                    # Concurrent creation of the same name reuses the winning
                    # model rather than introducing duplicate relationships.
                    existing = connection.execute("SELECT id FROM models WHERE normalized_name=?", (normalized,)).fetchone()
                    if existing is None:
                        raise
                    return {"model": self._models(connection, existing["id"])[0]}, 200
                status = 201
            else:
                try:
                    connection.execute("UPDATE models SET name=?,normalized_name=? WHERE id=?", (name, normalized, model_id))
                    connection.execute("UPDATE media_libraries SET default_author=? WHERE default_author_id=?",
                                       (name, model_id))
                    connection.execute("INSERT OR IGNORE INTO model_folder_names(normalized_name,model_id) VALUES(?,?)",
                                       (previous["normalized_name"], model_id))
                except sqlite3.IntegrityError as error:
                    raise ReaderError("已有同名模特，请使用不同名称。", 409) from error
                status = 200
            connection.execute("DELETE FROM suppressed_model_names WHERE normalized_name=?", (normalized,))
            before = previous or {"avatar_file": "", "avatar_revision": 0}
            avatar = self._avatar_change(body, "model-" + model_id, before, decoded)
            try:
                connection.execute("UPDATE models SET avatar_file=?,avatar_revision=? WHERE id=?",
                                   (avatar[0], avatar[1], model_id))
                connection.commit()
            except BaseException:
                self._discard_avatar(avatar[2])
                raise
            if avatar[0] != before["avatar_file"]:
                self._discard_avatar(before["avatar_file"])
            return {"model": self._models(connection, model_id)[0]}, status

    def remove_model(self, model_id):
        with self.state_lock, self.avatar_lock, closing(self.connect()) as connection, connection:
            model = connection.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
            if model is None:
                raise ReaderError("模特不存在，请刷新后重试。", 404)
            names = {model["normalized_name"]}
            names.update(row["normalized_name"] for row in connection.execute(
                "SELECT normalized_name FROM model_folder_names WHERE model_id=?", (model_id,)))
            connection.executemany("INSERT OR IGNORE INTO suppressed_model_names(normalized_name) VALUES(?)",
                                   [(name,) for name in names])
            affected = [row["id"] for row in connection.execute("""SELECT b.id FROM books b
                LEFT JOIN book_metadata m ON m.book_id=b.id
                WHERE m.folder_model_id=? OR EXISTS
                (SELECT 1 FROM book_models bm WHERE bm.book_id=b.id AND bm.model_id=?)
                OR b.library_id IN (SELECT id FROM media_libraries
                    WHERE default_author_id=? AND media_type='photo')""", (model_id, model_id, model_id))]
            connection.executemany("INSERT OR IGNORE INTO book_metadata(book_id) VALUES(?)",
                                   [(book_id,) for book_id in affected])
            connection.executemany("UPDATE book_metadata SET models_auto_suppressed=1 WHERE book_id=?",
                                   [(book_id,) for book_id in affected])
            connection.execute("UPDATE book_metadata SET folder_model_id=NULL WHERE folder_model_id=?", (model_id,))
            connection.execute("DELETE FROM book_models WHERE model_id=?", (model_id,))
            # A running scan with this former default must discard its stale
            # configuration rather than restore a removed foreign key.
            connection.execute("""UPDATE media_libraries SET default_author_id=NULL,
                default_author='',revision=revision+1 WHERE default_author_id=?""", (model_id,))
            connection.execute("DELETE FROM model_folder_names WHERE model_id=?", (model_id,))
            connection.execute("DELETE FROM models WHERE id=?", (model_id,))
            connection.commit()
            # Only generated avatar files for this model are eligible. Source
            # media, work covers, and reading state are left in place.
            for candidate in self.avatars.glob("model-" + model_id + "-*.jpg"):
                self._discard_avatar(candidate.name)
        return {"removed": [model_id]}

    @staticmethod
    def _bookmarks(connection, book_id=None):
        rows = connection.execute("""SELECT k.*, b.page_count FROM bookmarks k
            JOIN books b ON b.id=k.book_id WHERE b.active=1"""
            + (" AND k.book_id=?" if book_id is not None else "")
            + " ORDER BY k.created_at DESC,k.id", (book_id,) if book_id is not None else ()).fetchall()
        return [{"id": row["id"], "book_id": row["book_id"],
                 "page": min(row["page"], max(0, row["page_count"] - 1)),
                 "note": row["note"], "created_at": row["created_at"]} for row in rows]

    def bookmarks(self):
        with closing(self.connect()) as connection:
            bookmarks = self._bookmarks(connection)
            rows = self._select(connection)
            models = self._models_for_books(connection, [row["id"] for row in rows])
            books = {row["id"]: self._export(row, models.get(row["id"])) for row in rows}
            for bookmark in bookmarks:
                bookmark["book"] = books[bookmark["book_id"]]
        return {"bookmarks": bookmarks}

    def save_bookmark(self, book_id, body):
        page = body.get("page")
        note = self._text(body.get("note", ""), "书签备注", 1000, blank=True, multiline=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            book = self._select(connection, book_id)
            if book is None:
                raise ReaderError("作品不存在，请重新扫描写真库。", 404)
            if type(page) is not int or not 0 <= page < book["page_count"]:
                raise ReaderError("书签页码需要在作品的页码范围内。")
            existing = connection.execute("SELECT id FROM bookmarks WHERE book_id=? AND page=?", (book_id, page)).fetchone()
            bookmark_id = existing["id"] if existing else secrets.token_hex(12)
            connection.execute("""INSERT INTO bookmarks(id,book_id,page,note,created_at) VALUES(?,?,?,?,?)
                ON CONFLICT(book_id,page) DO UPDATE SET note=excluded.note""", (bookmark_id, book_id, page, note, time.time()))
            bookmark = next(item for item in self._bookmarks(connection, book_id) if item["id"] == bookmark_id)
        return {"bookmark": bookmark}, 200 if existing else 201

    def delete_bookmark(self, bookmark_id):
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM bookmarks WHERE id=?", (bookmark_id,))
            if not cursor.rowcount:
                raise ReaderError("书签不存在，请刷新后重试。", 404)
        return {"ok": True}

    def set_progress(self, book_id, body):
        page, completed = body.get("page"), body.get("completed")
        if type(page) is not int or type(completed) is not bool:
            raise ReaderError("阅读进度需要整数页码和完成状态。")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, book_id)
            if row is None:
                raise ReaderError("写真不存在，请重新扫描写真库。", 404)
            if not 0 <= page < row["page_count"]:
                raise ReaderError("页码超出写真范围。")
            updated_at = time.time()
            connection.execute("""INSERT INTO progress(book_id,page,completed,updated_at) VALUES(?,?,?,?)
                                  ON CONFLICT(book_id) DO UPDATE SET page=excluded.page,
                                  completed=excluded.completed,updated_at=excluded.updated_at""",
                               (book_id, page, int(completed), updated_at))
        return {"progress": {"page": page, "completed": completed, "updated_at": updated_at}}

    def set_favorite(self, book_id, body):
        favorite = body.get("favorite")
        if type(favorite) is not bool:
            raise ReaderError("收藏状态需要 true 或 false。")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._select(connection, book_id) is None:
                raise ReaderError("写真不存在，请重新扫描写真库。", 404)
            connection.execute("""INSERT INTO favorites(book_id,favorite) VALUES(?,?)
                                  ON CONFLICT(book_id) DO UPDATE SET favorite=excluded.favorite""",
                               (book_id, int(favorite)))
        return {"favorite": favorite}

    def image(self, book_id, index):
        book = self.book(book_id, internal=True)
        if index is None:
            index = min(max(book["cover_page"], 0), max(0, book["page_count"] - 1))
        if index < 0 or index >= book["page_count"]:
            raise ReaderError("图片页码不存在。", 404)
        page = json.loads(book["pages"])[index]
        try:
            root = self._directory_path(book["source_root"])
            if book["format"] == "folder":
                with open_library_file(self.root, self._media_path(root, page)) as handle:
                    if os.fstat(handle.fileno()).st_size > MAX_PAGE_BYTES:
                        raise ReaderError("图片超过 64 MiB，请调整图片后重新扫描。", 422)
                    data = handle.read(MAX_PAGE_BYTES + 1)
            else:
                with open_library_file(self.root, self._media_path(root, book["path"])) as handle:
                    check_zip_directory(handle)
                    with zipfile.ZipFile(handle) as archive:
                        if len(archive.infolist()) > MAX_ZIP_ENTRIES:
                            raise ReaderError("压缩包中的文件过多，请重新整理写真。", 422)
                        info = archive.getinfo(page)
                        validate_archive_image(info)
                        with archive.open(info) as source:
                            data = source.read(MAX_PAGE_BYTES + 1)
            if len(data) > MAX_PAGE_BYTES:
                raise ReaderError("图片超过 64 MiB，已停止读取。", 422)
            if not data:
                raise ReaderError("图片文件为空，请检查文件。", 422)
        except (OSError, KeyError, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as error:
            raise ReaderError("无法读取这一页，文件可能已移动或损坏，请重新扫描。", 422) from error
        mime = IMAGE_TYPES.get(PurePosixPath(page).suffix.lower(), "application/octet-stream")
        # The digest reflects the actual bytes, including changes made between scans.
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        return data, mime, etag

    def cover(self, book_id):
        with self.cover_lock:
            book = self.book(book_id, internal=True)
            if book["cover_source"] == "upload":
                name = book["cover_file"]
                if not re.fullmatch(re.escape(book_id) + r"-upload-[a-f0-9]{24}\.jpg", name):
                    raise ReaderError("上传的封面文件无效，请重新上传。", 422)
                try:
                    data = (self.covers / name).read_bytes()
                except OSError as error:
                    raise ReaderError("上传的封面不存在，请重新上传或选择作品内页。", 422) from error
            else:
                page_index = min(max(book["cover_page"], 0), max(0, book["page_count"] - 1))
                page = json.loads(book["pages"])[page_index]
                root = self._directory_path(book["source_root"])
                relative = page if book["format"] == "folder" else book["path"]
                try:
                    with open_library_file(self.root, self._media_path(root, relative)) as handle:
                        info = os.fstat(handle.fileno())
                        stamp = f"{info.st_size}:{info.st_mtime_ns}"
                except OSError as error:
                    raise ReaderError("无法读取封面来源，请重新扫描写真库。", 422) from error
                version = hashlib.sha256((self._cover_version(book) + stamp).encode()).hexdigest()[:24]
                name = book_id + "-page-" + version + ".jpg"
                candidate = self.covers / name
                try:
                    data = candidate.read_bytes()
                except FileNotFoundError:
                    original, _, _ = self.image(book_id, page_index)
                    data = self._thumbnail(original)
                    self._write_cover(name, data)
                    self._prune_covers(book_id, {name})
            etag = '"' + hashlib.sha256(data).hexdigest() + '"'
            return data, "image/jpeg", etag


def _private_address(value):
    """Only addresses usable by another device on a private network."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version == 4:
        return any(address in network for network in (
            ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16")))
    return address in ipaddress.ip_network("fc00::/7")


def _windows_default_ipv4():
    """Read the default-route interface using IP Helper, without opening sockets."""
    import ctypes

    helper = ctypes.WinDLL("iphlpapi")

    def table(name):
        size = ctypes.c_ulong(0)
        function = getattr(helper, name)
        function.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int)
        function.restype = ctypes.c_ulong
        if function(None, ctypes.byref(size), 0) != 122:  # ERROR_INSUFFICIENT_BUFFER
            return b""
        buffer = ctypes.create_string_buffer(size.value)
        if function(buffer, ctypes.byref(size), 0):
            return b""
        return buffer.raw[:size.value]

    routes, addresses = table("GetIpForwardTable"), table("GetIpAddrTable")
    if len(routes) < 4 or len(addresses) < 4:
        return []
    # MIB_IPFORWARDROW contains fourteen DWORDs; MIB_IPADDRROW has five
    # DWORDs and two WORDs. Both tables start with a DWORD row count.
    route_rows = [struct.unpack_from("=14I", routes, offset)
                  for offset in range(4, min(len(routes), 4 + struct.unpack_from("=I", routes)[0] * 56), 56)
                  if offset + 56 <= len(routes)]
    address_rows = [struct.unpack_from("=5I2H", addresses, offset)
                    for offset in range(4, min(len(addresses), 4 + struct.unpack_from("=I", addresses)[0] * 24), 24)
                    if offset + 24 <= len(addresses)]
    default_routes = sorted((row for row in route_rows if row[0] == row[1] == 0 and row[5] == 4),
                            key=lambda row: row[9])
    result = []
    for route in default_routes:
        for row in address_rows:
            value = socket.inet_ntoa(struct.pack("=I", row[0]))
            if row[1] == route[4] and _private_address(value) and value not in result:
                result.append(value)
    return result


def _linux_default_ipv4():
    import fcntl

    routes = []
    for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == fields[7] == "00000000" and int(fields[3], 16) & 3 == 3:
            routes.append((int(fields[6]), fields[0]))
    result = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as descriptor:
        for _, interface in sorted(routes):
            try:
                raw = fcntl.ioctl(descriptor.fileno(), 0x8915, struct.pack("256s", interface.encode()[:15]))
                value = socket.inet_ntoa(raw[20:24])
                if _private_address(value) and value not in result:
                    result.append(value)
            except OSError:
                continue
    return result


def _lan_ipv4_addresses():
    try:
        return _windows_default_ipv4() if os.name == "nt" else _linux_default_ipv4()
    except (OSError, ValueError, ImportError, AttributeError):
        return []


def _container_runtime():
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def access_urls(host, secure, server_address):
    """Prefer the browser's address: it preserves Docker mappings and proxy TLS."""
    empty = {"lan_url": None, "lan_urls": []}
    if not host or any(character.isspace() for character in host) or any(character in host for character in "/\\?#@"):
        return empty
    try:
        parsed = urlsplit("//" + host)
        hostname = parsed.hostname
        port = parsed.port
        if not hostname or port == 0:
            return empty
    except ValueError:
        return empty
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # A DNS name can be a NAS hostname or the name of its reverse proxy.
        if len(hostname) > 253 or not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                          for label in hostname.rstrip(".").split(".")):
            return empty
        address = None
    scheme = "https" if secure else "http"
    local = (address is not None and (address.is_loopback or address.is_unspecified)
             or hostname.casefold().rstrip(".") == "localhost"
             or hostname.casefold().rstrip(".").endswith(".localhost"))
    if not local:
        if address is not None and not _private_address(hostname):
            return empty
        url = f"{scheme}://{host}"
        return {"lan_url": url, "lan_urls": [url]}
    # A loopback-only listener is not reachable from other devices. Container
    # loopback provides no reliable information about the NAS's published port.
    binding = server_address[0]
    if ipaddress.ip_address(binding).is_loopback or _container_runtime():
        return empty
    addresses = [binding] if _private_address(binding) else _lan_ipv4_addresses()
    port = port or (443 if secure else server_address[1])
    suffix = "" if port == (443 if secure else 80) else f":{port}"
    urls = [f"{scheme}://{'[' + value + ']' if ':' in value else value}{suffix}"
            for value in addresses if _private_address(value)]
    return {"lan_url": urls[0] if urls else None, "lan_urls": urls}


class ReaderServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, library):
        self.library = library
        self.static = Path(__file__).resolve().parent / "static"
        repository, configuration_error = release_configuration()
        self.updates = UpdateChecker(VERSION, repository, configuration_error)
        super().__init__(address, ReaderHandler)


class ReaderHandler(BaseHTTPRequestHandler):
    server_version = "Gravelle/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format_string, *args):
        LOG.info("%s %s", self.client_address[0], (format_string % args).translate(
            {code: "?" for code in range(32)}))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    @property
    def library(self):
        return self.server.library

    def respond(self, payload, status=200, headers=None):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def session_token(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            cookie = cookies.get(COOKIE_NAME)
            return cookie.value if cookie else None
        except Exception:
            return None

    def authenticated(self):
        return self.library.valid_session(self.session_token())

    def require_auth(self):
        if not self.authenticated():
            raise ReaderError("请先登录个人账户。", 401)

    def read_body(self, maximum=MAX_BODY_BYTES):
        if self.headers.get_content_type() != "application/json":
            raise ReaderError("请求需要使用 application/json。", 415)
        if self.headers.get("Transfer-Encoding"):
            raise ReaderError("不支持分块请求。")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError as error:
            raise ReaderError("请求长度无效。") from error
        if length < 0:
            raise ReaderError("请求缺少 Content-Length。", 411)
        if length > maximum:
            raise ReaderError("请求内容过大。", 413)
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("incomplete body")
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise ReaderError("请求中的 JSON 无效。") from error
        if not isinstance(payload, dict):
            raise ReaderError("请求内容需要是 JSON 对象。")
        return payload

    def check_origin(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return
        try:
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "")
            valid = parsed.scheme in ("http", "https") and parsed.netloc.casefold() == host.casefold()
            valid = valid and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username
        except ValueError:
            valid = False
        if not valid:
            raise ReaderError("请求来源不匹配，请从阅读器页面重试。", 403)

    def cookie_header(self, value, clear=False):
        cookie = SimpleCookie()
        cookie[COOKIE_NAME] = value
        cookie[COOKIE_NAME]["path"] = "/"
        cookie[COOKIE_NAME]["httponly"] = True
        cookie[COOKIE_NAME]["samesite"] = "Strict"
        cookie[COOKIE_NAME]["max-age"] = 0 if clear else SESSION_SECONDS
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            cookie[COOKIE_NAME]["secure"] = True
        return cookie.output(header="").strip()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._handle(self.get_route)

    def do_POST(self):
        self._handle(self.post_route)

    def do_OPTIONS(self):
        self.respond({"error": "不允许跨站请求。"}, 405, {"Allow": "GET, HEAD, POST"})

    def _handle(self, callback):
        try:
            callback()
        except ReaderError as error:
            if self.command == "POST":
                # An early rejection may leave unread bytes on this connection.
                self.close_connection = True
            headers = {"Retry-After": str(self.library.login_retry(self.client_address[0]))} if error.status == 429 else None
            self.respond({"error": str(error)}, error.status, headers)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            LOG.exception("Request failed")
            self.close_connection = True
            try:
                self.respond({"error": "服务器处理失败，请稍后重试。"}, 500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def get_route(self):
        request = urlsplit(self.path)
        path = request.path
        query = parse_qs(request.query, keep_blank_values=True)
        if path == "/api/health":
            self.respond({"status": "ok", "version": VERSION})
            return
        if path == "/api/session":
            self.respond(self.library.session(self.session_token()))
            return
        if path.startswith("/api/"):
            self.require_auth()
            if path == "/api/updates":
                if request.query:
                    raise ReaderError("版本检查不接受外部地址或查询参数。")
                self.respond(self.server.updates.check())
                return
            if path == "/api/access":
                secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
                self.respond(access_urls(self.headers.get("Host", ""), secure, self.server.server_address))
                return
            if path == "/api/libraries":
                self.respond(self.library.libraries())
                return
            if path == "/api/directories":
                self.respond(self.library.directories(query.get("path", [None])[0]))
                return
            if path == "/api/library":
                self.respond(self.library.listing(query.get("library_id", [None])[0]))
                return
            if path == "/api/models":
                self.respond(self.library.models())
                return
            if path == "/api/bookmarks":
                self.respond(self.library.bookmarks())
                return
            match = re.fullmatch(r"/api/avatars/(user|models)/(1|[a-f0-9]{24})", path)
            if match:
                self.respond_image(*self.library.avatar(*match.groups()))
                return
            match = re.fullmatch(r"/api/books/([a-f0-9]{24})(?:/(cover|pages/([0-9]+)))?", path)
            if match:
                book_id, media, index = match.groups()
                if media is None:
                    self.respond(self.library.book(book_id))
                else:
                    if index is not None and len(index) > 6:
                        raise ReaderError("图片页码不存在。", 404)
                    data, mime, etag = (self.library.cover(book_id) if media == "cover" else
                                       self.library.image(book_id, int(index)))
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("ETag", etag)
                        self.send_header("Cache-Control", "private, no-cache")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "private, no-cache")
                    self.send_header("ETag", etag)
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)
                return
            raise ReaderError("接口不存在。", 404)
        name = STATIC_FILES.get(path)
        if name is None:
            raise ReaderError("页面不存在。", 404)
        candidate = self.server.static / name
        try:
            data = candidate.read_bytes()
        except OSError as error:
            raise ReaderError("页面文件不存在，请检查部署文件是否完整。", 404) from error
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            mime = "text/javascript"
        elif name.endswith(".ttf"):
            mime = "font/ttf"
        if mime.startswith("text/") or name.endswith(".webmanifest"):
            mime += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def respond_image(self, data, mime, etag):
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def post_route(self):
        self.check_origin()
        path = urlsplit(self.path).path
        if path not in ("/api/register", "/api/login", "/api/logout"):
            self.require_auth()
        upload_request = (re.fullmatch(r"/api/books/[a-f0-9]{24}/metadata", path)
                          or re.fullmatch(r"/api/models(?:/[a-f0-9]{24})?", path) or path == "/api/profile")
        body = self.read_body(MAX_COVER_BODY_BYTES if upload_request else MAX_BODY_BYTES)
        if path in ("/api/register", "/api/login"):
            user_id = (self.library.register(body, self.client_address[0]) if path == "/api/register" else
                       self.library.login(body, self.client_address[0]))
            token = self.library.make_session(user_id)
            self.respond(self.library.session(token), 201 if path == "/api/register" else 200,
                         headers={"Set-Cookie": self.cookie_header(token)})
            return
        if path == "/api/logout":
            self.library.revoke_session(self.session_token())
            self.respond(self.library.session(None),
                         headers={"Set-Cookie": self.cookie_header("", clear=True)})
            return
        if path == "/api/profile":
            password_changed = self.library.save_profile(body, self.client_address[0])
            token = self.library.make_session(1) if password_changed else self.session_token()
            self.respond(self.library.session(token), headers={"Set-Cookie": self.cookie_header(token)} if password_changed else None)
            return
        if path == "/api/scan":
            scanning = self.library.start_scan(body.get("library_id"))
            self.respond({"scanning": scanning}, 202)
            return
        if path == "/api/libraries":
            self.respond(self.library.save_library(body), 201)
            return
        if path == "/api/libraries/batch":
            result = self.library.batch_libraries(body)
            self.respond(result, 202 if body.get("action") == "scan" else 200)
            return
        if path == "/api/books/batch":
            self.respond(self.library.batch_books(body))
            return
        match = re.fullmatch(r"/api/(libraries|books)/([a-f0-9]{24})/remove", path)
        if match:
            kind, identifier = match.groups()
            operation = self.library.batch_libraries if kind == "libraries" else self.library.batch_books
            self.respond(operation({"action": "remove", "ids": [identifier]}))
            return
        if path == "/api/models":
            payload, status = self.library.save_model(body)
            self.respond(payload, status)
            return
        match = re.fullmatch(r"/api/models/([a-f0-9]{24})/remove", path)
        if match:
            self.respond(self.library.remove_model(match[1]))
            return
        match = re.fullmatch(r"/api/models/([a-f0-9]{24})", path)
        if match:
            payload, status = self.library.save_model(body, match[1])
            self.respond(payload, status)
            return
        match = re.fullmatch(r"/api/bookmarks/([a-f0-9]{24})/delete", path)
        if match:
            self.respond(self.library.delete_bookmark(match[1]))
            return
        match = re.fullmatch(r"/api/libraries/([a-f0-9]{24})(/scan)?", path)
        if match:
            library_id, scan = match.groups()
            if scan:
                scanning = self.library.start_scan(library_id)
                self.respond({"scanning": scanning}, 202)
            else:
                self.respond(self.library.save_library(body, library_id))
            return
        match = re.fullmatch(r"/api/books/([a-f0-9]{24})/(progress|favorite|metadata|bookmarks)", path)
        if match:
            book_id, action = match.groups()
            if action == "metadata":
                self.respond(self.library.save_book_metadata(book_id, body))
            elif action == "bookmarks":
                payload, status = self.library.save_bookmark(book_id, body)
                self.respond(payload, status)
            else:
                self.respond(self.library.set_progress(book_id, body) if action == "progress"
                             else self.library.set_favorite(book_id, body))
            return
        raise ReaderError("接口不存在。", 404)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        port = int(os.environ.get("PORT", "8080"))
        interval = float(os.environ.get("SCAN_INTERVAL", "300"))
        if not 0 <= port <= 65535 or not 0 <= interval <= 31 * 24 * 60 * 60:
            raise ValueError("PORT 或 SCAN_INTERVAL 超出有效范围。")
        library = Library(Path(os.environ.get("LIBRARY_PATH", "./library")),
                          Path(os.environ.get("DATA_PATH", "./data")),
                          os.environ.get("READER_PASSWORD", ""), interval)
        server = ReaderServer((os.environ.get("HOST", "0.0.0.0"), port), library)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        LOG.error("无法启动阅读器：%s", error)
        return 1

    def stop(signum, frame):
        library.stop_event.set()
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    library.start()
    LOG.info("Gravelle %s listening on port %s", VERSION, server.server_address[1])
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        library.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

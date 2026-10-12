"""Account schema and password helpers shared by the server and offline tools.

Importing this module never opens a database, starts a reader, or scans media.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
import unicodedata

PASSWORD_ITERATIONS = 600_000
PERMISSION_OPTIONS = (
    ("edit_metadata", "编辑作品资料与封面"),
    ("manage_models", "创建、编辑与删除模特"),
    ("scan_libraries", "扫描已授权写真库"),
    ("remove_books", "移除已授权作品"),
)
PERMISSION_KEYS = tuple(key for key, _ in PERMISSION_OPTIONS)


def validate_password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 256 or any(
            unicodedata.category(character) == "Cs" for character in value):
        raise ValueError("密码需要为 8 至 256 个字符。")
    return value


def hash_password(value):
    value = validate_password(value)
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(value, encoded):
    if not isinstance(value, str) or len(value) > 256:
        return False
    try:
        algorithm, iterations, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return False
        salt_bytes, expected = bytes.fromhex(salt), bytes.fromhex(digest)
        if len(salt_bytes) != 24 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt_bytes, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def prepare_account_migration(database: Path):
    """Back up an existing single-user DB before any startup schema changes."""
    if not database.exists():
        return None
    attributes = database.lstat()
    if not stat.S_ISREG(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
        raise RuntimeError("数据库必须是普通文件，不能通过链接迁移。")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            return None
        target_dir = database.parent / "backups"
        if target_dir.exists() or target_dir.is_symlink():
            attributes = target_dir.lstat()
            if (not stat.S_ISDIR(attributes.st_mode) or stat.S_ISLNK(attributes.st_mode)
                    or getattr(attributes, "st_file_attributes", 0) & 0x400):
                raise RuntimeError("备份目录不能是符号链接或目录联接。")
        target_dir.mkdir(mode=0o700, exist_ok=True)
        backup = target_dir / ("before-multiuser-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                               + "-" + secrets.token_hex(6) + ".sqlite3")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(backup)) as destination:
                deadline = time.monotonic() + 30
                def progress(status, remaining, total):
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > deadline:
                        raise RuntimeError("数据库正忙，无法安全备份，未执行账户迁移。")
                source.backup(destination, pages=256, progress=progress, sleep=0.05)
            with backup.open("r+b") as handle:
                os.fsync(handle.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    return backup


def _ensure_hidden_libraries(connection):
    # Visibility is an account preference, including for the administrator
    # whose access covers all libraries without user_libraries grant rows.
    definition = """CREATE TABLE IF NOT EXISTS user_hidden_libraries (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        library_id TEXT NOT NULL REFERENCES media_libraries(id) ON DELETE CASCADE,
        PRIMARY KEY(user_id,library_id))"""
    old_grant_foreign_key = any(row[2] == "user_libraries" for row in connection.execute(
        "PRAGMA foreign_key_list(user_hidden_libraries)"))
    if old_grant_foreign_key:
        connection.execute("ALTER TABLE user_hidden_libraries RENAME TO __grant_hidden_libraries")
        connection.execute(definition)
        connection.execute("""INSERT INTO user_hidden_libraries SELECT h.user_id,h.library_id
            FROM __grant_hidden_libraries h JOIN users u ON u.id=h.user_id
            JOIN media_libraries l ON l.id=h.library_id""")
        connection.execute("DROP TABLE __grant_hidden_libraries")
    else:
        connection.execute(definition)


def migrate_accounts(database: Path):
    """Atomically convert released single-user records without changing IDs.

    Personal rows use an explicit user_id. Old records belong to administrator
    1 even for a legacy installation that has not completed registration yet.
    The owner_account table is retained as a compatibility mirror, never used
    as a login authority after migration.
    """
    with closing(sqlite3.connect(database, timeout=20)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                _ensure_hidden_libraries(connection)
            return False
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL, normalized_username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, avatar_color TEXT NOT NULL DEFAULT '#ea5098',
                password_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                avatar_file TEXT NOT NULL DEFAULT '', avatar_revision INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL CHECK(role IN ('admin','user')),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                permissions TEXT NOT NULL DEFAULT '{}',
                CHECK((id=1 AND role='admin') OR (id<>1 AND role='user'))
            )""")
            old = connection.execute("SELECT * FROM owner_account WHERE id=1").fetchone()
            if old is not None:
                fields = ("id", "username", "normalized_username", "display_name", "avatar_color",
                          "password_hash", "created_at", "updated_at", "avatar_file", "avatar_revision")
                connection.execute("INSERT INTO users(" + ",".join(fields)
                    + ",role,enabled,permissions) VALUES(" + ",".join("?" for _ in fields) + ",'admin',1,?)",
                    (*[old[field] for field in fields], json.dumps(dict.fromkeys(PERMISSION_KEYS, True))))
            for table in ("progress", "favorites", "bookmarks"):
                connection.execute(f"ALTER TABLE {table} RENAME TO __single_user_{table}")
            connection.execute("""CREATE TABLE progress (
                user_id INTEGER NOT NULL DEFAULT 1, book_id TEXT NOT NULL REFERENCES books(id),
                page INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
                PRIMARY KEY(user_id,book_id))""")
            connection.execute("""CREATE TABLE favorites (
                user_id INTEGER NOT NULL DEFAULT 1, book_id TEXT NOT NULL REFERENCES books(id),
                favorite INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,book_id))""")
            connection.execute("""CREATE TABLE bookmarks (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 1,
                book_id TEXT NOT NULL REFERENCES books(id), page INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                UNIQUE(user_id,book_id,page))""")
            connection.execute("INSERT INTO progress SELECT 1,book_id,page,completed,updated_at FROM __single_user_progress")
            connection.execute("INSERT INTO favorites SELECT 1,book_id,favorite FROM __single_user_favorites")
            connection.execute("INSERT INTO bookmarks SELECT id,1,book_id,page,note,created_at FROM __single_user_bookmarks")
            for table in ("progress", "favorites", "bookmarks"):
                connection.execute(f"DROP TABLE __single_user_{table}")
            connection.execute("DROP TABLE auth_sessions")
            connection.execute("""CREATE TABLE auth_sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
            connection.execute("CREATE INDEX auth_sessions_expiry ON auth_sessions(expires_at)")
            connection.execute("""CREATE TABLE user_libraries (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                library_id TEXT NOT NULL REFERENCES media_libraries(id) ON DELETE CASCADE,
                PRIMARY KEY(user_id,library_id))""")
            connection.execute("""CREATE TABLE reading_preferences (
                user_id INTEGER NOT NULL, book_id TEXT NOT NULL REFERENCES books(id),
                reading_mode TEXT, reading_direction TEXT,
                PRIMARY KEY(user_id,book_id))""")
            _ensure_hidden_libraries(connection)
            connection.execute("ALTER TABLE models ADD COLUMN created_by INTEGER NOT NULL DEFAULT 1")
            connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('accounts_schema','1')")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return True

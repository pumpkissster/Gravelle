"""SQLite snapshots for startup migrations and explicit offline restoration."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time


# Bump this for every on-disk schema migration, including managers initialized by Library.
# Mark the version only after all startup migrations have finished.
DATABASE_SCHEMA_VERSION = 1
_MARKER = ".schema-upgrade.json"
_BACKUP_NAME = re.compile(r"before-(?:schema-upgrade-v\d+-to-v\d+|multiuser|database-restore)-[\w.-]+\.sqlite3")
_LOG = logging.getLogger("sumi")


class DatabaseBackupError(RuntimeError):
    pass


def _is_reparse_point(info):
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _regular_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
        raise DatabaseBackupError(f"数据库和备份必须使用普通文件，不能使用链接：{path}")
    return True


def _database_exists(path):
    exists = _regular_file(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if _regular_file(sidecar) and not exists:
            raise DatabaseBackupError(f"数据库文件缺失，但仍有 SQLite 日志文件：{sidecar}")
    return exists


def _backup_directory(data, *, create=False):
    directory = data / "backups"
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return directory
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
        raise DatabaseBackupError(f"备份目录不能使用符号链接或目录联接：{directory}")
    return directory


def _open_database(path, mode):
    return sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=2)


def _sqlite_copy(source, destination):
    deadline = time.monotonic() + 30

    def progress(status, _remaining, _total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
            raise DatabaseBackupError("数据库正忙，请停止所有使用此资料目录的进程后重试。")

    source.backup(destination, pages=256, progress=progress, sleep=.05)


def _check_database(connection):
    if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
        raise DatabaseBackupError("SQLite 快速完整性检查失败。")


def _sync_directory(directory):
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(descriptor)


def _backup_path(directory, prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return directory / f"{prefix}-{stamp}-{secrets.token_hex(6)}.sqlite3"


def _snapshot_to(source, backup):
    descriptor, name = tempfile.mkstemp(dir=backup.parent, prefix=".database-snapshot-", suffix=".tmp")
    os.close(descriptor)
    temporary = Path(name)
    try:
        with closing(_open_database(temporary, "rw")) as destination:
            _sqlite_copy(source, destination)
            # Produce a standalone snapshot, including committed source WAL changes.
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise DatabaseBackupError("无法生成独立的 SQLite 备份文件。")
            _check_database(destination)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, backup)
        _sync_directory(backup.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def _resolve_backup(directory, name):
    if not isinstance(name, str) or not _BACKUP_NAME.fullmatch(name):
        raise DatabaseBackupError("请填写 backups 目录中的升级或恢复备份文件名。")
    backup = directory / name
    if not _database_exists(backup):
        raise DatabaseBackupError(f"找不到数据库备份：{backup}")
    return backup


def _read_marker(marker):
    if not _regular_file(marker):
        return None
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        version, name = record["from_version"], record["backup"]
        if type(version) is not int or version < 0 or not isinstance(name, str):
            raise ValueError("Invalid upgrade record")
    except (ValueError, TypeError, KeyError) as error:
        raise DatabaseBackupError(f"无法读取升级记录，请检查备份目录：{marker}") from error
    return version, name


def _write_marker(marker, version, backup):
    descriptor, name = tempfile.mkstemp(dir=marker.parent, prefix=".schema-upgrade-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"from_version": version, "backup": backup.name}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        _sync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _clear_marker(marker):
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        _LOG.warning("无法清理数据库升级记录：%s", marker)


def prepare_legacy_account_backup(database):
    """Compatibility helper for offline callers of the old account migration."""
    database = Path(database).absolute()
    if not _database_exists(database):
        return None
    with closing(_open_database(database, "ro")) as source:
        if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            return None
        folder = _backup_directory(database.parent, create=True)
        return _snapshot_to(source, _backup_path(folder, "before-multiuser"))


@contextmanager
def database_upgrade(database, on_upgrade=None):
    """Guard all startup migrations while the caller owns the data directory."""
    backup = None
    marker = None
    pending = None
    version = 0
    try:
        if _database_exists(database):
            with closing(_open_database(database, "ro")) as source:
                version = source.execute("PRAGMA user_version").fetchone()[0]
                if version > DATABASE_SCHEMA_VERSION:
                    raise DatabaseBackupError("数据库结构高于此版本，请使用相应版本的 Gravelle。")
                if version < DATABASE_SCHEMA_VERSION:
                    if on_upgrade:
                        on_upgrade()
                    directory = _backup_directory(database.parent)
                    marker = directory / _MARKER
                    pending = _read_marker(marker)
                    if pending and pending[0] == version:
                        backup = _resolve_backup(directory, pending[1])
                        with closing(_open_database(backup, "ro")) as saved:
                            _check_database(saved)
                            if saved.execute("PRAGMA user_version").fetchone()[0] != version:
                                raise DatabaseBackupError("原升级备份与待升级数据库的结构版本不一致。")
                    else:
                        directory = _backup_directory(database.parent, create=True)
                        prefix = f"before-schema-upgrade-v{version}-to-v{DATABASE_SCHEMA_VERSION}"
                        _LOG.info("正在创建数据库升级前备份：%s", directory)
                        backup = _snapshot_to(source, _backup_path(directory, prefix))
                        _write_marker(marker, version, backup)
                    _LOG.info("数据库升级前备份：%s", backup)
        else:
            directory = _backup_directory(database.parent)
            pending = _read_marker(directory / _MARKER)
            if pending:
                path = _resolve_backup(directory, pending[1])
                raise DatabaseBackupError(f"资料库文件缺失，请先从升级备份恢复：{path}")
    except (OSError, sqlite3.Error, DatabaseBackupError) as error:
        message = f"无法准备数据库升级：{error}"
        if backup:
            message += f"。已保存的升级前备份：{backup}"
        _LOG.error("%s", message)
        raise DatabaseBackupError(message) from error

    try:
        yield backup
        if version < DATABASE_SCHEMA_VERSION:
            with closing(_open_database(database, "rw")) as connection, connection:
                connection.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION:d}")
    except Exception as error:
        if backup is None:
            raise
        message = f"数据库升级失败。升级前备份：{backup}。原因：{error}"
        _LOG.error("%s", message)
        raise DatabaseBackupError(message) from error
    else:
        if marker is not None:
            _clear_marker(marker)


def restore_database_backup(data, name):
    """Replace the database through SQLite with the service stopped."""
    from password_reset import DataDirectoryLock

    data = Path(data).resolve()
    if not data.is_dir():
        raise DatabaseBackupError("资料目录不存在。")
    lock = DataDirectoryLock(data)
    if not lock.acquire():
        raise DatabaseBackupError("资料目录正在使用，请先停止阅读器。")
    previous = None
    try:
        directory = _backup_directory(data)
        backup = _resolve_backup(directory, name)
        database = data / "library.sqlite3"
        with closing(_open_database(backup, "ro")) as source:
            _check_database(source)
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"metadata", "books"} <= tables:
                raise DatabaseBackupError("指定文件不是 Gravelle 资料库备份。")
            if _database_exists(database):
                with closing(_open_database(database, "rw")) as destination:
                    previous = _snapshot_to(destination, _backup_path(directory, "before-database-restore"))
                    _sqlite_copy(source, destination)
            else:
                _snapshot_to(source, database)
        _clear_marker(directory / _MARKER)
        return previous
    except (OSError, sqlite3.Error, DatabaseBackupError) as error:
        if previous:
            raise DatabaseBackupError(f"恢复失败。恢复前资料库已备份到：{previous}。原因：{error}") from error
        raise
    finally:
        lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线恢复 Gravelle 的数据库快照")
    parser.add_argument("--data", type=Path, required=True, help="原资料目录")
    parser.add_argument("--backup", required=True, help="backups 目录中的备份文件名")
    options = parser.parse_args(argv)
    try:
        previous = restore_database_backup(options.data, options.backup)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"无法恢复资料库：{error}", file=sys.stderr)
        return 1
    print("资料库已恢复。请使用创建该备份时对应的版本启动。")
    if previous:
        print(f"恢复前资料库已备份到：{previous}")
    return 0

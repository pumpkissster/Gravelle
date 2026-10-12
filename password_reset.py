#!/usr/bin/env python3
"""Offline administrator recovery; never creates or migrates a library."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import errno
import getpass
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time
import warnings

from accounts import hash_password, validate_password


class PasswordResetError(RuntimeError):
    pass


class DataDirectoryLock:
    """One live server or maintenance operation owns a data directory.

    The file remains after release. Only the OS lock, held by this open handle,
    indicates ownership; a crashed process cannot leave a stale PID lock.
    """
    def __init__(self, data: Path):
        self.data = Path(data).resolve()
        self.path = self.data / ".gravelle-service.lock"
        self.stream = None

    def acquire(self) -> bool:
        if self.stream is not None:
            return True
        if not self.data.is_dir():
            raise PasswordResetError("资料目录不存在。")
        if self.path.is_symlink():
            raise PasswordResetError("资料目录锁不能是符号链接。")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        stream = os.fdopen(descriptor, "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self.stream = stream
        return True

    def release(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        if not self.acquire():
            raise PasswordResetError("服务正在使用此资料目录，请先停止服务后再重置密码。")
        return self

    def __exit__(self, *args):
        self.release()


def validate_confirmation(password: str, confirmation: str) -> str:
    try:
        value = validate_password(password)
        validate_password(confirmation)
    except ValueError as error:
        raise PasswordResetError(str(error)) from error
    if value != confirmation:
        raise PasswordResetError("两次输入的密码不一致。")
    return value


def _existing_connection(database: Path):
    # URI mode=rw rejects a missing database instead of silently creating one.
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    return connection


def _backup(database: Path, data: Path) -> Path:
    directory = data / "backups"
    if directory.is_symlink() or not directory.resolve().is_relative_to(data):
        raise PasswordResetError("备份目录必须位于资料目录内。")
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = directory / f"before-password-reset-{stamp}-{secrets.token_hex(6)}.sqlite3"
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        # The main connection holds BEGIN IMMEDIATE, reserving the writer slot.
        # A separate read connection avoids backing up a connection with its own
        # write transaction, which SQLite would repeatedly report as busy.
        with closing(_existing_connection(database)) as source, \
                closing(_existing_connection(backup)) as destination:
            deadline = time.monotonic() + 30
            def progress(status, remaining, total):
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > deadline:
                    raise PasswordResetError("资料库正忙，未修改密码，请稍后重试。")
            source.backup(destination, pages=256, progress=progress, sleep=0.05)
        with backup.open("r+b") as handle:
            os.fsync(handle.fileno())
        return backup
    except BaseException:
        backup.unlink(missing_ok=True)
        raise


def reset_admin_password(data: Path, password: str, confirmation: str) -> dict:
    """Back up an existing database, reset only id 1, revoke its sessions."""
    password = validate_confirmation(password, confirmation)
    try:
        data = Path(data).resolve()
        database = data / "library.sqlite3"
        if not data.is_dir() or not database.is_file():
            raise PasswordResetError("资料库不存在；请指定已有账户的资料目录。")
        attributes = database.lstat()
        if not stat.S_ISREG(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
            raise PasswordResetError("资料库必须是普通文件，不能通过链接重置。")
        with DataDirectoryLock(data), closing(_existing_connection(database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "users" in tables:
                owner = connection.execute("SELECT username,role,password_hash,updated_at FROM users WHERE id=1").fetchone()
                if owner is None or owner["role"] != "admin":
                    raise PasswordResetError("资料库内没有主管理员账户，无法重置密码。")
                account_table = "users"
            elif "owner_account" in tables:
                owner = connection.execute("SELECT username,password_hash,updated_at FROM owner_account WHERE id=1").fetchone()
                if owner is None:
                    raise PasswordResetError("资料库内没有主管理员账户，无法重置密码。")
                account_table = "owner_account"
            else:
                raise PasswordResetError("资料库内没有主管理员账户，无法重置密码。")
            if "auth_sessions" not in tables:
                raise PasswordResetError("账户资料结构不完整，未修改密码。")
            encoded = hash_password(password)
            backup = _backup(database, data)
            updated_at = time.time()
            connection.execute(f"UPDATE {account_table} SET password_hash=?,updated_at=? WHERE id=1",
                               (encoded, updated_at))
            if account_table == "users" and "owner_account" in tables:
                connection.execute("UPDATE owner_account SET password_hash=?,updated_at=? WHERE id=1",
                                   (encoded, updated_at))
            connection.execute("DELETE FROM auth_sessions WHERE user_id=1")
            connection.commit()
            return {"username": owner["username"], "backup": str(backup)}
    except (OSError, sqlite3.Error) as error:
        raise PasswordResetError("无法重置管理员密码，未完成的更改已回滚：" + str(error)) from error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线重置 Gravelle 主管理员密码（需先停止服务）")
    parser.add_argument("--data", type=Path, required=True, help="已有资料目录，例如 /data")
    arguments = parser.parse_args(argv)
    try:
        # Refuse getpass's fallback to echoed stdin when no terminal is present.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("新管理员密码：")
            confirmation = getpass.getpass("再次输入新密码：")
        result = reset_admin_password(arguments.data, password, confirmation)
    except (PasswordResetError, getpass.GetPassWarning, EOFError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, PasswordResetError) else "需要交互终端隐藏输入密码；Docker 请先停止原服务，再使用 run --rm -it 运行一次性容器。"
        print("重置失败：" + message, file=sys.stderr)
        return 1
    print("管理员密码已重置，请使用新密码重新登录。")
    print("资料备份：" + result["backup"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

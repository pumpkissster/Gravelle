"""Network boundaries and local, offline recovery for the reader.

The entry capability is separate from account authentication. No network
operation can reset it, and no secret entry path is written to request logs.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

from password_reset import DataDirectoryLock, PasswordResetError

ENTRY_COOKIE = "gravelle_entry"
ENTRY_SECONDS = 30 * 24 * 60 * 60
RECOVERY_WARNINGS = {
    "share_isolation_preserved": "原分享端口设置无法确认，已保持独立分享端口；请在网络设置中核对，分享不会回退到阅读器端口。",
    "public_share_url_cleared": "原公网分享地址无法确认，已清空；请在网络设置中重新填写正确地址。",
}
SHARE_STATIC = frozenset({"/share-reader.js", "/share-reader.css", "/share-crypto.js",
                          "/styles.css", "/reader-model.js", "/vendor/noble-gcm.js",
                          "/gravelle-icon.png", "/gravelle-wordmark.svg"})
SHARE_PAGE = re.compile(r"/share/[A-Za-z0-9_-]{43}")
SHARE_GET = re.compile(r"/api/share/[A-Za-z0-9_-]{43}(?:/(?:manifest|pages/[0-9]{1,6}))?")
SHARE_POST = re.compile(r"/api/share/[A-Za-z0-9_-]{43}/(?:unlock|logout)")


class NetworkSettingsError(RuntimeError):
    pass


def validate_entry_code(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value)
            or value.casefold() in {"api", "share", "index.html", "settings", "favicon.ico"}):
        raise ValueError("安全入口码需要为 8～64 位英文字母、数字、下划线或短横线。")
    return value


def validate_public_share_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("公网分享地址格式无效。")
    if not value:
        return ""
    if (value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value)
            or any(c in value for c in "\\%")):
        raise ValueError("公网分享地址仅支持 HTTP 或 HTTPS 域名和端口。")
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"http", "https"} or not hostname or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or "?" in value or "#" in value or port == 0):
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if not hostname.isascii() or len(hostname) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                    for part in hostname.rstrip(".").split(".")):
                raise ValueError
        if parsed.netloc.endswith(":"):
            raise ValueError
    except ValueError as error:
        raise ValueError("公网分享地址仅支持 HTTP 或 HTTPS 域名和端口。") from error
    return f"{parsed.scheme}://{parsed.netloc}"


def _defaults():
    return {"security_entry_enabled": False, "security_entry_code": "",
            "share_port_enabled": False, "public_share_url": "",
            "entry_epoch": secrets.token_hex(32)}


def _decode(raw):
    try:
        value = json.loads(raw)
        expected = {"security_entry_enabled", "security_entry_code", "share_port_enabled",
                    "public_share_url", "entry_epoch"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError
        for key in ("security_entry_enabled", "share_port_enabled"):
            if type(value[key]) is not bool:
                raise ValueError
        code = value["security_entry_code"]
        if code or value["security_entry_enabled"]:
            validate_entry_code(code)
        elif not isinstance(code, str):
            raise ValueError
        validate_public_share_url(value["public_share_url"])
        if not isinstance(value["entry_epoch"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["entry_epoch"]):
            raise ValueError
        return value
    except (TypeError, ValueError, KeyError) as error:
        raise NetworkSettingsError("网络设置已损坏，请先停止服务，再在本机重置安全入口。") from error


def _existing(data, *, readonly=False):
    data = Path(data).resolve()
    database = data / "library.sqlite3"
    if not data.is_dir() or not database.exists():
        return data, database, None
    attributes = database.lstat()
    if not stat.S_ISREG(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
        raise NetworkSettingsError("资料库必须是普通文件，不能通过链接访问。")
    connection = sqlite3.connect(database.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=2)
    return data, database, connection


def read_security_entry_path(data_path):
    """Read only: new/legacy libraries use root; malformed settings fail closed."""
    try:
        _, _, connection = _existing(data_path, readonly=True)
        if connection is None:
            return "/"
        with closing(connection):
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_settings'").fetchone()
            if not exists:
                return "/"
            row = connection.execute("SELECT settings FROM network_settings WHERE id=1").fetchone()
            if row is None:
                raise NetworkSettingsError("网络设置不完整，请在本机重置安全入口。")
            value = _decode(row[0])
            return "/" + value["security_entry_code"] if value["security_entry_enabled"] else "/"
    except (OSError, sqlite3.Error) as error:
        raise NetworkSettingsError("无法读取安全入口，请检查已有资料目录。") from error


def _recovery_settings(raw):
    """Recover the entry without silently weakening the sharing boundary.

    Each independent policy is validated on its own. An unreadable sharing
    switch means isolation must stay enabled until the administrator reviews
    it, rather than publishing guests on the management listener by default.
    """
    warnings = []
    try:
        return _decode(raw), warnings
    except NetworkSettingsError:
        pass
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            document = {}
    except (TypeError, ValueError):
        document = {}
    value = _defaults()
    sharing = document.get("share_port_enabled")
    if type(sharing) is bool:
        value["share_port_enabled"] = sharing
    else:
        value["share_port_enabled"] = True
        warnings.append("share_isolation_preserved")
    try:
        value["public_share_url"] = validate_public_share_url(document.get("public_share_url"))
    except ValueError:
        value["public_share_url"] = ""
        warnings.append("public_share_url_cleared")
    return value, warnings


def reset_security_entry(data_path):
    """Disable the entry offline, backing up existing data before any change."""
    try:
        data = Path(data_path).resolve()
        if not data.is_dir():
            raise NetworkSettingsError("资料目录不存在。")
        with DataDirectoryLock(data):
            _, database, connection = _existing(data)
            if connection is None:
                raise NetworkSettingsError("资料库不存在，请选择已有资料目录。")
            with closing(connection):
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_settings'").fetchone()
                if not exists:
                    return {"disabled": True, "changed": False, "backup": None}
                row = connection.execute("SELECT settings FROM network_settings WHERE id=1").fetchone()
                current, warnings = _recovery_settings(row[0] if row is not None else None)
                backup_dir = data / "backups"
                if backup_dir.is_symlink() or not backup_dir.resolve().is_relative_to(data):
                    raise NetworkSettingsError("备份目录必须位于资料目录内。")
                backup_dir.mkdir(mode=0o700, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                backup = backup_dir / f"before-security-entry-reset-{stamp}-{secrets.token_hex(6)}.sqlite3"
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                try:
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as source, closing(sqlite3.connect(backup)) as destination:
                        deadline = time.monotonic() + 30
                        def progress(status, remaining, total):
                            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > deadline:
                                raise NetworkSettingsError("资料库正忙，未修改安全入口。")
                        source.backup(destination, pages=256, progress=progress, sleep=.05)
                    with backup.open("r+b") as handle:
                        os.fsync(handle.fileno())
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
                current.update(security_entry_enabled=False, security_entry_code="", entry_epoch=secrets.token_hex(32))
                connection.execute("INSERT OR REPLACE INTO network_settings(id,settings) VALUES(1,?)", (json.dumps(current),))
                connection.commit()
                return {"disabled": True, "changed": True, "backup": str(backup),
                        "warnings": [RECOVERY_WARNINGS[code] for code in warnings], "warning_codes": warnings}
    except (OSError, sqlite3.Error, PasswordResetError) as error:
        raise NetworkSettingsError("无法重置安全入口，请停止服务并检查资料目录：" + str(error)) from error


class NetworkSettings:
    def __init__(self, library):
        self.library = library
        self.lock = threading.RLock()
        with closing(library.connect()) as connection, connection:
            existed = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_settings'").fetchone()
            connection.execute("CREATE TABLE IF NOT EXISTS network_settings(id INTEGER PRIMARY KEY CHECK(id=1),settings TEXT NOT NULL)")
            row = connection.execute("SELECT settings FROM network_settings WHERE id=1").fetchone()
            if existed and row is None:
                raise NetworkSettingsError("网络设置不完整，请先停止服务，在本机重置安全入口。")
            self.value = _decode(row[0]) if row is not None else _defaults()
            if row is None:
                connection.execute("INSERT INTO network_settings(id,settings) VALUES(1,?)", (json.dumps(self.value),))

    def current(self):
        with self.lock:
            return dict(self.value)

    def prepare(self, body):
        if not isinstance(body, dict) or not body or set(body) - {
                "security_entry_enabled", "security_entry_code", "share_port_enabled", "public_share_url"}:
            raise ValueError("网络设置包含不支持的字段，端口只能在启动器或部署配置中设置。")
        value = self.current()
        for key in ("security_entry_enabled", "share_port_enabled"):
            if key in body:
                if type(body[key]) is not bool:
                    raise ValueError("网络设置开关需要为布尔值。")
                value[key] = body[key]
        if "security_entry_code" in body:
            code = body["security_entry_code"]
            if code or value["security_entry_enabled"]:
                validate_entry_code(code)
            elif not isinstance(code, str):
                raise ValueError("安全入口码格式无效。")
            value["security_entry_code"] = code
        if value["security_entry_enabled"]:
            validate_entry_code(value["security_entry_code"])
        if "public_share_url" in body:
            value["public_share_url"] = validate_public_share_url(body["public_share_url"])
        old = self.current()
        if any(old[key] != value[key] for key in ("security_entry_enabled", "security_entry_code")):
            value["entry_epoch"] = secrets.token_hex(32)
        return value

    def persist(self, value):
        with self.lock, closing(self.library.connect()) as connection, connection:
            connection.execute("UPDATE network_settings SET settings=? WHERE id=1", (json.dumps(value),))
            # Commit before exposing the new epoch to concurrent requests.
            connection.commit()
            self.value = dict(value)

    def issue_cookie(self):
        with self.lock:
            payload = f"{self.value['entry_epoch']}.{int(time.time()) + ENTRY_SECONDS}"
            signature = hmac.new(self.library.signing_key, ("entry-v1:" + payload).encode(), hashlib.sha256).hexdigest()
            return payload + "." + signature

    def valid_cookie(self, token):
        with self.lock:
            if not isinstance(token, str) or len(token) > 180:
                return False
            match = re.fullmatch(r"([a-f0-9]{64})\.([0-9]{1,12})\.([a-f0-9]{64})", token)
            if not match or not hmac.compare_digest(match[1], self.value["entry_epoch"]):
                return False
            expiry = int(match[2])
            if not time.time() < expiry <= time.time() + ENTRY_SECONDS + 1:
                return False
            payload = match[1] + "." + match[2]
            expected = hmac.new(self.library.signing_key, ("entry-v1:" + payload).encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, match[3])


def main(argv=None):
    parser = argparse.ArgumentParser(description="停止服务后，在本机关闭 Gravelle 安全入口")
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = reset_security_entry(args.data)
    except NetworkSettingsError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("安全入口已关闭；重新启动后可通过原访问地址登录。")
    if result["backup"]:
        print("资料备份：" + result["backup"])
    for warning in result.get("warnings", []):
        print("提醒：" + warning)
    return 0

"""Bounded PKCS#12 loading and a local vault for the certificate password."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
import secrets
import re
import ssl
import stat
import tempfile

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import pkcs12

KEY_NAME = "https-password.key"
MAX_CERTIFICATE_BYTES = 16 * 1024 * 1024
MAX_CERTIFICATE_BASE64 = 4 * ((MAX_CERTIFICATE_BYTES + 2) // 3)
MAX_CERTIFICATE_BODY_BYTES = MAX_CERTIFICATE_BASE64 + 64 * 1024
CERTIFICATE_DIRECTORY = "https-certificates"
MAX_PASSWORD_BYTES = 4096
PASSWORD_AAD = b"gravelle-https-certificate-password-v1"


class HTTPSSettingsError(ValueError):
    pass


def _plain(info):
    return (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400 and info.st_nlink == 1)


def _read_file(path, maximum):
    """Read an ordinary, unchanged local file without following its final link."""
    before = path.lstat()
    if not _plain(before) or before.st_size > maximum:
        raise OSError("not a bounded ordinary file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not _plain(opened) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("file changed before opening")
        data = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        if (len(data) > maximum or len(data) != opened.st_size
                or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise OSError("file changed while reading")
        return data


def validate_password(password):
    if not isinstance(password, str) or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES or "\0" in password:
        raise HTTPSSettingsError("证书密码格式无效或过长。")
    return password


def validate_certificate_path(value):
    if (not isinstance(value, str) or len(value) > 4096 or "\0" in value
            or any(ord(c) < 32 for c in value)):
        raise HTTPSSettingsError("证书路径格式无效。")
    if value and (not Path(value).is_absolute() or Path(value).suffix.lower() not in {".pfx", ".p12"}):
        raise HTTPSSettingsError("请选择服务器本地的 PFX 或 P12 证书文件绝对路径。")
    return value


def decode_certificate_upload(value):
    """The supplied basename is metadata only, never a storage destination."""
    if not isinstance(value, dict) or set(value) != {"name", "data"}:
        raise HTTPSSettingsError("上传证书需要文件名与文件内容。")
    name, encoded = value["name"], value["data"]
    if (not isinstance(name, str) or not 1 <= len(name) <= 255
            or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)
            or Path(name).suffix.lower() not in {".pfx", ".p12"}):
        raise HTTPSSettingsError("请选择 PFX 或 P12 证书文件。")
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_CERTIFICATE_BASE64:
        raise HTTPSSettingsError("证书不能为空，且最多为 16 MiB。")
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if not decoded or len(decoded) > MAX_CERTIFICATE_BYTES or base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError
    except (ValueError, UnicodeError):
        raise HTTPSSettingsError("上传的证书内容无效或超过 16 MiB。") from None
    return decoded, Path(name).suffix.lower()


class CertificateUploadStore:
    """Private managed certificates; all writes and cleanup stay in one directory."""
    def __init__(self, data):
        self.data = Path(data).resolve(strict=True)
        self.path = self.data / CERTIFICATE_DIRECTORY

    @contextmanager
    def _directory(self, create=False):
        descriptor = windows_handle = None
        try:
            if create:
                self.path.mkdir(mode=0o700, exist_ok=True)
            before = self.path.lstat()
            if (not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode)
                    or getattr(before, "st_file_attributes", 0) & 0x400
                    or self.path.resolve(strict=True).parent != self.data):
                raise OSError("invalid managed certificate directory")
            if os.name == "nt":
                # Keep the directory open without FILE_SHARE_DELETE: a reparse
                # replacement cannot redirect the later ordinary-file open.
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
                kernel.CreateFileW.restype = wintypes.HANDLE
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel.CloseHandle.restype = wintypes.BOOL
                handle = kernel.CreateFileW(str(self.path), 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                windows_handle = (kernel, handle)
                after = self.path.lstat()
                if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                        or getattr(after, "st_file_attributes", 0) & 0x400):
                    raise OSError("managed directory changed")
            else:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
                opened = os.fstat(descriptor)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OSError("managed directory changed")
                os.fchmod(descriptor, 0o700)
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if windows_handle is not None:
                windows_handle[0].CloseHandle(windows_handle[1])

    def stage(self, upload):
        data, suffix = decode_certificate_upload(upload)
        name = secrets.token_hex(16) + suffix
        target = self.path / name
        try:
            with self._directory(create=True) as directory:
                options = {"dir_fd": directory} if directory is not None else {}
                source = name if directory is not None else target
                descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600, **options)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                        if not _plain(os.fstat(handle.fileno())):
                            raise OSError("managed certificate is not an ordinary file")
                except BaseException:
                    os.unlink(source, **options)
                    raise
            return target
        except OSError:
            raise HTTPSSettingsError("无法保存上传的证书，请检查资料目录权限。") from None

    def owns_path(self, value):
        path = Path(value)
        return path.parent == self.path and bool(re.fullmatch(r"[a-f0-9]{32}\.(pfx|p12)", path.name))

    def validate_reference(self, value):
        """A stale form must not restore a managed file already replaced/deleted."""
        if not self.owns_path(value):
            return
        path = Path(value)
        try:
            with self._directory() as directory:
                options = {"dir_fd": directory} if directory is not None else {}
                source = path.name if directory is not None else path
                before = os.stat(source, follow_symlinks=False, **options)
                if not _plain(before) or not 0 < before.st_size <= MAX_CERTIFICATE_BYTES:
                    raise OSError("invalid managed certificate reference")
                descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                                     | getattr(os, "O_NOFOLLOW", 0), **options)
                try:
                    opened = os.fstat(descriptor)
                    if not _plain(opened) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise OSError("managed certificate changed")
                finally:
                    os.close(descriptor)
        except OSError:
            raise HTTPSSettingsError("所选上传证书已被替换或不可用，请刷新网络设置后重新选择。") from None

    def discard(self, value):
        """Never unlink an external path, a link, or an unrecognized basename."""
        path = Path(value)
        if not self.owns_path(value):
            return False
        try:
            with self._directory() as directory:
                options = {"dir_fd": directory} if directory is not None else {}
                source = path.name if directory is not None else path
                info = os.stat(source, follow_symlinks=False, **options)
                if not _plain(info):
                    return False
                os.unlink(source, **options)
                return True
        except OSError:
            return False


class CertificatePasswordVault:
    def __init__(self, data):
        self.path = Path(data) / KEY_NAME

    def _key(self, create=False):
        try:
            data = _read_file(self.path, 32)
        except FileNotFoundError:
            if not create:
                raise HTTPSSettingsError("证书密码密钥不存在，请重新填写证书密码后保存。") from None
            key = secrets.token_bytes(32)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise
            return key
        if len(data) != 32:
            raise HTTPSSettingsError("证书密码密钥无效，请检查完整资料备份。")
        return data

    def seal(self, password):
        validate_password(password)
        if not password:
            return ""
        try:
            key = self._key(create=True)
            nonce = secrets.token_bytes(12)
            return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, password.encode("utf-8"), PASSWORD_AAD)).decode("ascii")
        except (OSError, ValueError) as error:
            if isinstance(error, HTTPSSettingsError):
                raise
            raise HTTPSSettingsError("无法安全保存证书密码，请检查资料目录。") from None

    def reveal(self, encrypted):
        if not encrypted:
            return ""
        try:
            key = self._key()
            blob = base64.b64decode(encrypted, validate=True)
            if not 28 <= len(blob) <= MAX_PASSWORD_BYTES + 28:
                raise ValueError
            return AESGCM(key).decrypt(blob[:12], blob[12:], PASSWORD_AAD).decode("utf-8")
        except (OSError, ValueError, InvalidTag, UnicodeError):
            raise HTTPSSettingsError("无法读取证书密码，请重新填写证书密码后保存。") from None


def load_context(certificate_path, password):
    """Return a ready context; private PEM files exist only while loading it."""
    validate_certificate_path(certificate_path)
    validate_password(password)
    if not certificate_path:
        raise HTTPSSettingsError("启用 HTTPS 前请选择 PFX 或 P12 证书文件。")
    try:
        data = _read_file(Path(certificate_path), MAX_CERTIFICATE_BYTES)
    except OSError:
        raise HTTPSSettingsError("无法读取证书文件，请检查路径、权限及文件大小（最多 16 MiB）。") from None
    try:
        key, certificate, chain = pkcs12.load_key_and_certificates(data, password.encode("utf-8") if password else None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise HTTPSSettingsError("无法打开 PFX/P12 证书，请检查文件及证书密码。") from None
    if key is None or certificate is None:
        raise HTTPSSettingsError("证书文件必须同时包含证书和对应的私钥。")
    if len(chain or ()) > 16:
        raise HTTPSSettingsError("证书链过长，请使用有效的服务器证书链。")
    now = datetime.now(timezone.utc)
    if any(not item.not_valid_before_utc <= now < item.not_valid_after_utc for item in (certificate, *(chain or ()))):
        raise HTTPSSettingsError("证书尚未生效或已经过期，请更换有效证书。")
    public = lambda item: item.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if not hmac.compare_digest(public(key), public(certificate)):
        raise HTTPSSettingsError("证书与私钥不匹配。")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with tempfile.TemporaryDirectory(prefix="gravelle-tls-") as directory:
            cert_path, key_path = Path(directory) / "chain.pem", Path(directory) / "private.pem"
            cert_bytes = b"".join(item.public_bytes(serialization.Encoding.PEM) for item in (certificate, *(chain or ())))
            key_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            for path, content in ((cert_path, cert_bytes), (key_path, key_bytes)):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
            context.load_cert_chain(cert_path, key_path)
    except (OSError, ValueError, ssl.SSLError):
        raise HTTPSSettingsError("证书无法用于 HTTPS，请检查证书链及私钥。") from None
    return context

"""Small, recoverable share-password vault with a separate service key.

Password verification continues to use salted PBKDF2 hashes. This vault exists
only for an authenticated creator's explicit password reveal action. Losing
the vault key never changes an existing share's password or guest access.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_NAME = "share-passwords.key"
MAGIC = b"SPW1"
HEADER_BYTES = 32  # magic4 + SHA256(key)[:16] + nonce12


def _plain(info, directory=False):
    return ((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400
            and (directory or info.st_nlink == 1))


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _identity(info):
    return info.st_dev, info.st_ino


class SharePasswordVault:
    def __init__(self, data, allow_create=False):
        self.data = Path(data)
        self.path = self.data / KEY_NAME
        # Creation happens once, only when the DB contains no encrypted
        # passwords. Every later operation only reads the existing key.
        if allow_create:
            try:
                self._create()
            except (OSError, ValueError, NotImplementedError):
                pass

    def _directory(self):
        info = self.data.lstat()
        if not _plain(info, True):
            raise ValueError("The share-password key directory is not a plain directory")
        return info

    def _create(self):
        directory = self._directory()
        # lexists semantics: never replace a symlink, malformed key, or any
        # other existing object, even when it cannot be read.
        try:
            self.path.lstat()
            return
        except FileNotFoundError:
            pass
        temporary = self.data / (".share-passwords-" + secrets.token_hex(16) + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        owned = None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                owned = os.fstat(handle.fileno())
                if not _plain(owned):
                    raise ValueError("The key temporary file is not owned")
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
                written = os.fstat(handle.fileno())
                if not _plain(written) or written.st_size != 32:
                    raise ValueError("The key temporary file changed")
            if (_identity(self._directory()) != _identity(directory)
                    or _stamp(temporary.lstat()) != _stamp(written)):
                raise ValueError("The key directory or temporary file changed")
            try:
                # Windows rename fails when the destination exists. POSIX
                # rename would overwrite it, so publish there with link.
                # Both paths expose only the complete, fsynced key file.
                if os.name == "nt":
                    os.rename(temporary, self.path)
                else:
                    os.link(temporary, self.path, follow_symlinks=False)
            except FileExistsError:
                return
        finally:
            if owned is not None:
                try:
                    current = temporary.lstat()
                    if (_identity(current) == _identity(owned) and stat.S_ISREG(current.st_mode)
                            and not stat.S_ISLNK(current.st_mode)
                            and not getattr(current, "st_file_attributes", 0) & 0x400):
                        temporary.unlink()
                except OSError:
                    pass

    def key(self):
        """Read an existing, plain 32-byte key; absence is not an exception."""
        try:
            directory = self._directory()
            before = self.path.lstat()
            if not _plain(before) or before.st_size != 32:
                return None
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not _plain(opened) or _stamp(opened) != _stamp(before):
                    return None
                # POSIX enforces owner-only mode. Windows uses the private
                # data directory's inherited ACL and best-effort create mode.
                if hasattr(os, "fchmod"):
                    try:
                        os.fchmod(handle.fileno(), 0o600)
                    except OSError:
                        pass
                value = handle.read(33)
                after = os.fstat(handle.fileno())
            named = self.path.lstat()
            if (not _plain(after) or not _plain(named) or len(value) != 32
                    or _stamp(opened) != _stamp(after) or _stamp(named) != _stamp(after)
                    or _identity(self._directory()) != _identity(directory)):
                return None
            return value
        except (OSError, ValueError, NotImplementedError):
            return None

    @staticmethod
    def _aad(identifier, creator_id):
        return ("gravelle-share-password-v1:" + identifier + ":" + str(creator_id)).encode("ascii")

    @staticmethod
    def available(blob, key):
        return (isinstance(blob, bytes) and len(blob) >= HEADER_BYTES + 16
                and isinstance(key, bytes) and len(key) == 32 and blob[:4] == MAGIC
                and hmac.compare_digest(blob[4:20], hashlib.sha256(key).digest()[:16]))

    def seal(self, password, identifier, creator_id):
        key = self.key()
        if key is None:
            return None
        nonce = secrets.token_bytes(12)
        header = MAGIC + hashlib.sha256(key).digest()[:16] + nonce
        return header + AESGCM(key).encrypt(nonce, password.encode("utf-8"), header + self._aad(identifier, creator_id))

    def reveal(self, blob, identifier, creator_id):
        key = self.key()
        if not self.available(blob, key):
            return None
        try:
            value = AESGCM(key).decrypt(blob[20:32], blob[32:], blob[:32] + self._aad(identifier, creator_id)).decode("utf-8")
            return value if 8 <= len(value) <= 64 else None
        except (InvalidTag, ValueError, UnicodeError):
            return None

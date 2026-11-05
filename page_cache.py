"""Bounded private disk snapshots for already-authorized source-page reads.

Cache entries are anonymous temporary files, never paths into a media library.
Every caller still opens and checks its live source before asking for a lease.
Reservations include leased files and files being built, so a slow reader cannot
make the cache grow beyond its disk/descriptor budget.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import IOBase
import os
import tempfile
import threading
import time


DEFAULT_BYTES = 256 * 1024 * 1024
DEFAULT_ENTRIES = 64
DEFAULT_BUILDERS = 2


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [('creation', ctypes.c_longlong), ('access', ctypes.c_longlong),
                    ('write', ctypes.c_longlong), ('change', ctypes.c_longlong),
                    ('attributes', wintypes.DWORD)]

    _get_file_info = ctypes.WinDLL('kernel32', use_last_error=True).GetFileInformationByHandleEx
    _get_file_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _get_file_info.restype = wintypes.BOOL


def source_identity(handle, info=None):
    """Return a revision for the opened file, or None if it is uncacheable."""
    info = os.fstat(handle.fileno()) if info is None else info
    if not info.st_ino:
        return None
    if os.name != 'nt':
        return file_identity(info)
    # Python's Windows st_ctime historically tracks creation, so editing a
    # file and restoring its mtime could otherwise revive a stale snapshot.
    basic = _FileBasicInfo()
    if not _get_file_info(msvcrt.get_osfhandle(handle.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)) or not basic.change:
        return None
    return (*file_identity(info), basic.change)


class SnapshotStorageError(OSError):
    """Writing an optional cache failed; the source itself may still be read."""


class _Target:
    def __init__(self, source):
        self.source = source

    def _call(self, operation, *args):
        try:
            return getattr(self.source, operation)(*args)
        except OSError as error:
            raise SnapshotStorageError(str(error)) from error

    def write(self, data):
        return self._call('write', data)

    def flush(self):
        return self._call('flush')

    def seek(self, offset, whence=0):
        return self._call('seek', offset, whence)


@dataclass
class _Entry:
    source: object
    size: int
    mime: str
    etag: str
    identity: tuple
    references: int = 1
    retired: bool = False
    lock: object = field(default_factory=threading.Lock)


class _Reader(IOBase):
    immutable_snapshot = True

    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self.position

    def seek(self, offset, whence=0):
        self._checkClosed()
        if whence == os.SEEK_CUR:
            offset += self.position
        elif whence == os.SEEK_END:
            offset += self.entry.size
        elif whence != os.SEEK_SET:
            raise ValueError('Invalid snapshot seek origin')
        if offset < 0:
            raise ValueError('Negative snapshot seek')
        self.position = offset
        return offset

    def read(self, size=-1):
        self._checkClosed()
        remaining = max(0, self.entry.size - self.position)
        if size is None or size < 0:
            size = remaining
        else:
            size = min(size, remaining)
        # Readers hold independent offsets even on Windows, where dup() would
        # share a seek position. No lock is held during network transmission.
        with self.entry.lock:
            self.entry.source.seek(self.position)
            data = self.entry.source.read(size)
        self.position += len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


class SourcePageCache:
    def __init__(self, directory, *, max_bytes=DEFAULT_BYTES, max_entries=DEFAULT_ENTRIES,
                 max_builders=DEFAULT_BUILDERS, cancelled=lambda: False):
        self.directory = directory
        self.max_bytes = max(0, int(max_bytes))
        self.max_entries = max(0, int(max_entries))
        self.max_builders = max(0, int(max_builders))
        self.cancelled = cancelled
        self.condition = threading.Condition()
        self.entries = OrderedDict()
        self.building = {}
        self.used_bytes = 0
        self.used_entries = 0
        self.closed = False
        self.epoch = 0
        self.storage_retry_at = 0

    def _dispose(self, entry):
        self.used_bytes -= entry.size
        self.used_entries -= 1
        try:
            entry.source.close()
        except OSError:
            pass

    def _retire(self, key, entry):
        if self.entries.get(key) is entry:
            del self.entries[key]
        entry.retired = True
        if not entry.references:
            self._dispose(entry)

    def _reserve(self, size):
        if (size <= 0 or size > self.max_bytes or not self.max_entries or not self.max_builders
                or len(self.building) >= self.max_builders or time.monotonic() < self.storage_retry_at):
            return False
        for key, entry in list(self.entries.items()):
            if self.used_bytes + size <= self.max_bytes and self.used_entries < self.max_entries:
                break
            if not entry.references:
                self._retire(key, entry)
        if self.used_bytes + size > self.max_bytes or self.used_entries >= self.max_entries:
            return False
        self.used_bytes += size
        self.used_entries += 1
        return True

    def _acquire(self, key, size, build):
        with self.condition:
            while key in self.building and not self.closed and not self.cancelled():
                self.condition.wait(.1)
            if self.closed or self.cancelled():
                return None
            entry = self.entries.get(key)
            if entry is not None:
                try:
                    valid = file_identity(os.fstat(entry.source.fileno())) == entry.identity
                except OSError:
                    valid = False
                if valid:
                    entry.references += 1
                    self.entries.move_to_end(key)
                    return entry
                self._retire(key, entry)
            if not self._reserve(size):
                return None
            self.building[key] = size
            epoch = self.epoch
        source = None
        reserved = True
        try:
            try:
                source = tempfile.TemporaryFile(mode='w+b', prefix='.page-snapshot-', dir=self.directory)
            except OSError as error:
                raise SnapshotStorageError(str(error)) from error
            mime, etag = build(_Target(source))
            source.flush()
            identity = file_identity(os.fstat(source.fileno()))
            if identity[2] != size:
                raise ValueError('Snapshot length differs from its reservation')
            entry = _Entry(source, size, mime, etag, identity)
            with self.condition:
                if self.closed or self.cancelled():
                    return None
                if epoch == self.epoch:
                    self.entries[key] = entry
                else:
                    entry.retired = True
                reserved = False
                source = None
            return entry
        except SnapshotStorageError:
            # A full or read-only cache must not prevent an authorized original
            # from loading. Back off rather than repeatedly copying/decompressing
            # the same source into a known-unavailable cache.
            with self.condition:
                self.storage_retry_at = time.monotonic() + 30
            return None
        finally:
            if source is not None:
                try:
                    source.close()
                except OSError:
                    pass
            with self.condition:
                self.building.pop(key, None)
                if reserved:
                    self.used_bytes -= size
                    self.used_entries -= 1
                self.condition.notify_all()

    @contextmanager
    def open(self, key, size, build):
        entry = self._acquire(key, size, build) if key is not None else None
        if entry is None:
            yield None
            return
        reader = _Reader(entry)
        try:
            yield reader, entry.size, entry.mime, entry.etag
        finally:
            reader.close()
            with self.condition:
                entry.references -= 1
                if entry.retired and not entry.references:
                    self._dispose(entry)
                self.condition.notify_all()

    def clear(self):
        with self.condition:
            self.epoch += 1
            for key, entry in list(self.entries.items()):
                self._retire(key, entry)
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.clear()
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(bytes=self.used_bytes, entries=self.used_entries,
                        building=len(self.building), closed=self.closed,
                        max_bytes=self.max_bytes, max_entries=self.max_entries)

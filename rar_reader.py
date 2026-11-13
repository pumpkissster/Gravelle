"""Read image entries from RAR archives through the platform's libarchive."""
import ctypes as C
from contextlib import contextmanager
from ctypes.util import find_library
from functools import lru_cache
import os
import stat
import sys
from typing import NamedTuple


class RarError(ValueError):
    pass


class Entry(NamedTuple):
    name: str
    size: int
    mode: int
    linked: bool
    encrypted: bool


_Read = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p))
_Skip = C.CFUNCTYPE(C.c_int64, C.c_void_p, C.c_void_p, C.c_int64)


@lru_cache(maxsize=1)
def _library():
    if os.name == "nt":
        # Search Windows System32 only, never the current media directory.
        name = "archiveint.dll"
        options = {"winmode": 0x00000800}
    elif sys.platform == "darwin":
        name = "/usr/lib/libarchive.2.dylib"
        options = {}
    else:
        name = find_library("archive") or "libarchive.so.13"
        options = {}
    try:
        lib = C.CDLL(name, **options)
        ptr = C.c_void_p
        integer = C.c_int
        functions = {
            "archive_read_new": (ptr,),
            "archive_read_free": (integer, ptr),
            "archive_read_support_format_rar": (integer, ptr),
            "archive_read_support_format_rar5": (integer, ptr),
            "archive_read_open2": (integer, ptr, ptr, ptr, _Read, _Skip, ptr),
            "archive_read_next_header": (integer, ptr, C.POINTER(ptr)),
            "archive_read_data": (C.c_ssize_t, ptr, ptr, C.c_size_t),
            "archive_entry_pathname_utf8": (C.c_char_p, ptr),
            "archive_entry_size": (C.c_int64, ptr),
            "archive_entry_filetype": (C.c_uint, ptr),
            "archive_entry_hardlink": (ptr, ptr),
            "archive_entry_is_encrypted": (integer, ptr),
        }
        for symbol, (result, *arguments) in functions.items():
            function = getattr(lib, symbol)
            function.restype = result
            function.argtypes = arguments
    except (OSError, AttributeError) as error:
        raise RarError("此系统缺少支持 RAR 的 libarchive，无法读取 RAR 杂志。") from error
    return lib


class _Archive:
    def __init__(self, lib, archive, cancelled):
        self.lib = lib
        self.archive = archive
        self.cancelled = cancelled

    def check(self, result):
        if result < 0:
            if self.cancelled():
                raise RarError("阅读器正在停止，请稍后重试。")
            raise RarError("无法读取 RAR，请检查压缩包是否损坏、加密或采用了不兼容的压缩方式。")
        return result

    def entries(self, limit):
        entry = C.c_void_p()
        for index in range(limit + 1):
            status = self.check(self.lib.archive_read_next_header(self.archive, C.byref(entry)))
            if status == 1:  # ARCHIVE_EOF
                return
            if index == limit:
                raise RarError("压缩包中的文件过多。")
            mode = self.lib.archive_entry_filetype(entry)
            if mode == stat.S_IFDIR:
                continue
            raw_name = self.lib.archive_entry_pathname_utf8(entry)
            if raw_name is None:
                raise RarError("RAR 中有无法读取的文件名。")
            try:
                name = raw_name.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RarError("RAR 中有无法读取的文件名。") from error
            yield Entry(name, self.lib.archive_entry_size(entry), mode,
                        bool(self.lib.archive_entry_hardlink(entry)),
                        bool(self.lib.archive_entry_is_encrypted(entry)))

    def read(self, size):
        output = C.create_string_buffer(size)
        length = self.check(self.lib.archive_read_data(self.archive, output, size))
        return output[:length]


@contextmanager
def open_rar(handle, *, cancelled=lambda: False):
    handle.seek(0)
    magic = handle.read(8)
    if not magic.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        raise RarError("文件不是有效的 RAR 压缩包。")
    handle.seek(0)
    length = os.fstat(handle.fileno()).st_size
    buffer = C.create_string_buffer(64 * 1024)

    @_Read
    def read(_archive, _client, output):
        if cancelled():
            return -1
        try:
            output[0] = C.addressof(buffer)
            return handle.readinto(buffer)
        except OSError:
            return -1

    @_Skip
    def skip(_archive, _client, amount):
        if cancelled():
            return -1
        try:
            current = handle.tell()
            return handle.seek(min(current + amount, length)) - current
        except OSError:
            return -1

    lib = _library()
    archive = lib.archive_read_new()
    if not archive:
        raise RarError("无法初始化 RAR 读取。")
    reader = _Archive(lib, archive, cancelled)
    try:
        reader.check(lib.archive_read_support_format_rar(archive))
        reader.check(lib.archive_read_support_format_rar5(archive))
        reader.check(lib.archive_read_open2(archive, None, None, read, skip, None))
        yield reader
    finally:
        lib.archive_read_free(archive)

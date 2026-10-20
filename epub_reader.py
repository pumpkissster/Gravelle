"""Bounded, read-only EPUB 2/3 picture-book inspection and image pages.

No document code, remote resources, publisher CSS or fonts are served. The
caller must authorize the book before calling this module's public methods.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
from html import escape
from html.parser import HTMLParser
from html.entities import name2codepoint
from io import BytesIO
import json
import os
import posixpath
import re
import stat
import struct
import threading
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

MIB = 1024 * 1024
MAX_EPUB_BYTES = 512 * MIB
MAX_MEMBERS = 20_000
MAX_DIRECTORY_BYTES = 16 * MIB
MAX_TOTAL_BYTES = 1024 * MIB
MAX_CHAPTER_BYTES = 8 * MIB
MAX_RENDERED_CHAPTER_BYTES = 16 * MIB
MAX_CHAPTER_ELEMENTS = 100_000
MAX_XML_BYTES = 2 * MIB
MAX_IMAGE_BYTES = 32 * MIB
MAX_CHAPTERS = 3000
MAX_PAGES = 10_000
METADATA_VERSION = 2
MAX_TOC_ITEMS = 5000
MAX_RATIO = 500
MAX_METADATA_CACHE = 8 * MIB
MAX_CACHED_BOOKS = 16
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp"}
_XHTML_TYPES = {"application/xhtml+xml", "text/html"}
_FONT_TYPES = {"application/vnd.ms-opentype", "application/font-sfnt", "application/font-woff",
               "application/x-font-ttf", "application/x-font-opentype", "font/otf", "font/ttf", "font/woff", "font/woff2"}
_FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}


class EpubError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def source_version(info):
    values = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def page_key(index):
    if type(index) is not int or not 0 <= index < MAX_PAGES:
        raise ValueError("EPUB 页码无效。")
    return f"epub:{index + 1:06d}"


def _local(name):
    return name.rsplit("}", 1)[-1].split(":")[-1].lower()


def _text(node, maximum=300):
    return " ".join("".join(node.itertext()).split())[:maximum] if node is not None else ""


def _decode(data):
    # These explicit decoders also expose declarations hidden behind UTF-16
    # NULs to the DTD/entity check before ElementTree is allowed to parse.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        value = data.decode("utf-16")
    elif data.startswith(b"\x00<\x00?"):
        value = data.decode("utf-16-be")
    elif data.startswith(b"<\x00?\x00"):
        value = data.decode("utf-16-le")
    else:
        value = data.decode("utf-8-sig")
    if "\x00" in value:
        # Do not let Expat reinterpret a Unicode string containing UTF-32 or
        # undeclared UTF-16 byte patterns after the declaration safety check.
        raise ValueError("EPUB 文字编码无效，只支持 UTF-8 或 UTF-16。")
    return value


def _without_dtd(value):
    # EPUB 2 commonly names an external W3C XHTML/NCX DTD. Discard the
    # declaration as inert text; never resolve it or permit an internal subset.
    if re.search(r"<!\s*ENTITY\b", value, re.I):
        raise ValueError("EPUB 不支持 DTD 内部子集或自定义实体声明。")
    quoted = r'''(?:"[^"\[\]]*"|'[^'\[\]]*')'''
    declaration = rf"<!DOCTYPE\s+[A-Za-z_:][\w:.-]*(?:\s+PUBLIC\s+{quoted}\s+{quoted}|\s+SYSTEM\s+{quoted})?\s*>"
    value = re.sub(declaration, "", value, flags=re.I)
    if re.search(r"<!\s*DOCTYPE\b", value, re.I):
        raise ValueError("EPUB 不支持 DTD 内部子集或自定义实体声明。")
    return value


def _xml(data):
    value = _without_dtd(_decode(data))
    # Known XHTML character entities need no publisher DTD or entity resolver.
    value = re.sub(r"&([A-Za-z][A-Za-z0-9]+);", lambda match: (
        f"&#{name2codepoint[match[1]]};" if match[1] in name2codepoint and match[1] not in {"amp", "lt", "gt", "quot", "apos"} else match[0]), value)
    return ET.fromstring(value)


def _reference(base, value):
    """Resolve an archive-relative URI, never a filesystem or network path."""
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.query or "\\" in value:
        return None
    try:
        path = unquote(parts.path, encoding="utf-8", errors="strict")
        fragment = unquote(parts.fragment, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        return None
    if any(ord(c) < 32 for c in path + fragment) or path.startswith("/") or "\\" in path or ":" in path:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
    if joined in ("", ".", "..") or joined.startswith("../"):
        return None
    return unicodedata.normalize("NFC", joined), fragment[:256]


class _Archive:
    def __init__(self, handle):
        # zipfile reads its directory before exposing members; bound that work
        # before constructing ZipFile, and disallow ZIP64/multi-volume EPUBs.
        handle.seek(0, os.SEEK_END)
        length = handle.tell()
        if not 0 < length <= MAX_EPUB_BYTES:
            raise ValueError("EPUB 文件为空或超过 512 MiB，请拆分文件。")
        handle.seek(max(0, length - 65557))
        tail = handle.read(65557)
        at = tail.rfind(b"PK\x05\x06")
        if at < 0 or len(tail) - at < 22:
            raise ValueError("EPUB 压缩目录无效。")
        disk, start_disk, on_disk, count, size, offset, comment = struct.unpack_from("<4H2LH", tail, at + 4)
        if (at + 22 + comment != len(tail) or disk or start_disk or count != on_disk
                or count == 65535 or offset == 0xffffffff or size == 0xffffffff
                or count > MAX_MEMBERS or size > MAX_DIRECTORY_BYTES or offset + size > length):
            raise ValueError("EPUB 压缩目录过大、无效或使用了不支持的分卷格式。")
        self.zip = zipfile.ZipFile(handle)
        try:
            infos = self.zip.infolist()
            if len(infos) != count or len(infos) > MAX_MEMBERS:
                raise ValueError("EPUB 压缩目录中的条目数量无效。")
            self.members, seen, total = {}, set(), 0
            for item in infos:
                name = unicodedata.normalize("NFC", item.filename)
                segments = name.rstrip("/").split("/")
                if (item.filename != item.orig_filename or not name or len(name) > 4096
                        or name.startswith("/") or "\\" in name or ":" in name
                        or any(ord(c) < 32 for c in name) or any(part in ("", ".", "..") for part in segments)):
                    raise ValueError("EPUB 包含不安全的文件路径。")
                folded = name.rstrip("/").casefold()
                if folded in seen:
                    raise ValueError("EPUB 包含重复或有歧义的文件名。")
                seen.add(folded)
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("EPUB 不能包含链接或特殊文件。")
                if item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("EPUB 使用了加密或不支持的压缩方式。")
                total += item.file_size
                if (item.file_size < 0 or total > MAX_TOTAL_BYTES
                        or item.file_size > MAX_IMAGE_BYTES
                        or item.file_size > MIB and item.file_size > max(1, item.compress_size) * MAX_RATIO):
                    raise ValueError("EPUB 解压后的内容过大或压缩比例异常。")
                if not item.is_dir():
                    self.members[name] = item
            if self.read("mimetype", 128).strip() != b"application/epub+zip":
                raise ValueError("文件不是受支持的 EPUB。")
        except BaseException:
            self.zip.close()
            raise

    def close(self):
        self.zip.close()

    def read(self, name, limit):
        item = self.members.get(name)
        if item is None:
            raise ValueError("EPUB 缺少必要文件或引用了不存在的内容。")
        if item.file_size > limit:
            raise ValueError("EPUB 单个章节或资源超过安全上限，请拆分文件。")
        with self.zip.open(item) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit or len(data) != item.file_size:
            raise ValueError("EPUB 内容大小无效。")
        return data


def _manifest(archive):
    container = _xml(archive.read("META-INF/container.xml", MAX_XML_BYTES))
    roots = [node for node in container.iter() if _local(node.tag) == "rootfile"]
    if not roots:
        raise ValueError("EPUB 缺少作品目录。")
    entry = next((node for node in roots if node.get("media-type") == "application/oebps-package+xml"), roots[0])
    resolved = _reference("", entry.get("full-path", ""))
    if resolved is None:
        raise ValueError("EPUB 作品目录路径无效。")
    opf_path = resolved[0]
    package = _xml(archive.read(opf_path, MAX_XML_BYTES))
    if _local(package.tag) != "package":
        raise ValueError("EPUB 作品目录格式无效。")
    manifest_node = next((node for node in package if _local(node.tag) == "manifest"), None)
    spine_node = next((node for node in package if _local(node.tag) == "spine"), None)
    if manifest_node is None or spine_node is None:
        raise ValueError("EPUB 缺少阅读顺序。")
    manifest, resources, path_types = {}, {}, {}
    for node in manifest_node:
        if _local(node.tag) != "item":
            continue
        ident, mime = node.get("id", ""), node.get("media-type", "").lower()
        reference = _reference(opf_path, node.get("href", ""))
        if not ident or ident in manifest or reference is None or reference[0] not in archive.members:
            raise ValueError("EPUB 资源目录无效或包含外部引用。")
        path = reference[0]
        if path in path_types and path_types[path] != mime:
            raise ValueError("EPUB 的资源格式声明有歧义。")
        path_types[path] = mime
        item = dict(path=path, mime=mime, properties=node.get("properties", "").split())
        manifest[ident] = item
        if mime in _IMAGE_TYPES:
            token = hashlib.sha256(path.encode()).hexdigest()[:32]
            resources[token] = dict(path=path, mime=mime)
    if "META-INF/encryption.xml" in archive.members:
        encrypted = _xml(archive.read("META-INF/encryption.xml", MAX_XML_BYTES))
        for node in encrypted.iter():
            if _local(node.tag) != "encrypteddata":
                continue
            method = next((child.get("Algorithm") for child in node.iter() if _local(child.tag) == "encryptionmethod"), None)
            target = next((child.get("URI") for child in node.iter() if _local(child.tag) == "cipherreference"), None)
            resolved = _reference("", target)
            if (method not in _FONT_OBFUSCATION or resolved is None
                    or path_types.get(resolved[0]) not in _FONT_TYPES):
                raise ValueError("暂不支持受 DRM 保护或正文加密的 EPUB。")
    chapters, path_indexes = [], {}
    # Include non-linear chapters too: cover/dedication references remain
    # navigable without guessing a fallback or following arbitrary documents.
    for node in spine_node:
        if _local(node.tag) != "itemref":
            continue
        item = manifest.get(node.get("idref"))
        if item is None or item["mime"] not in (_XHTML_TYPES | {"image/svg+xml"}):
            raise ValueError("EPUB 的阅读顺序包含不支持的章节。")
        if item["path"] in path_indexes:
            raise ValueError("EPUB 的阅读顺序包含重复章节。")
        if len(chapters) >= MAX_CHAPTERS:
            raise ValueError("单本 EPUB 超过 3,000 个章节，请拆成分卷。")
        index = len(chapters)
        path_indexes[item["path"]] = index
        chapters.append(dict(index=index, title=f"第 {index + 1} 章", path=item["path"]))
    if not chapters:
        raise ValueError("EPUB 没有可阅读的章节。")
    toc = []
    nav = next((item for item in manifest.values() if "nav" in item["properties"] and item["mime"] in _XHTML_TYPES), None)
    if nav:
        root = _xml(archive.read(nav["path"], MAX_XML_BYTES))
        nav_nodes = [node for node in root.iter() if _local(node.tag) == "nav"]
        selected = next((node for node in nav_nodes if "toc" in (node.get("{http://www.idpf.org/2007/ops}type", "").split())), None)
        if selected is not None:
            def walk(node, depth=0):
                if len(toc) >= MAX_TOC_ITEMS:
                    return
                if _local(node.tag) == "a":
                    target = _reference(nav["path"], node.get("href", ""))
                    if target and target[0] in path_indexes:
                        toc.append(dict(title=_text(node), index=path_indexes[target[0]], fragment=target[1], depth=min(depth, 8)))
                for child in node:
                    walk(child, depth + (1 if _local(child.tag) == "ol" and _local(node.tag) != "nav" else 0))
            walk(selected)
    if not toc:
        ncx = manifest.get(spine_node.get("toc")) or next((item for item in manifest.values() if item["mime"] == "application/x-dtbncx+xml"), None)
        if ncx:
            root = _xml(archive.read(ncx["path"], MAX_XML_BYTES))
            def walk_ncx(node, depth=0):
                if len(toc) >= MAX_TOC_ITEMS:
                    return
                is_point = _local(node.tag) == "navpoint"
                if is_point:
                    content = next((n for n in node if _local(n.tag) == "content"), None)
                    label = next((n for n in node if _local(n.tag) == "navlabel"), None)
                    target = _reference(ncx["path"], content.get("src", "") if content is not None else "")
                    if target and target[0] in path_indexes:
                        toc.append(dict(title=_text(label), index=path_indexes[target[0]], fragment=target[1], depth=min(depth, 8)))
                for child in node:
                    walk_ncx(child, depth + (1 if is_point else 0))
            walk_ncx(root)
    assigned = set()
    for item in toc:
        if item["title"] and item["index"] not in assigned:
            chapters[item["index"]]["title"] = item["title"]
            assigned.add(item["index"])
    cover = next((item["path"] for item in manifest.values() if "cover-image" in item["properties"] and item["mime"] in _IMAGE_TYPES), None)
    metadata = next((node for node in package if _local(node.tag) == "metadata"), None)
    if cover is None and metadata is not None:
        cover_id = next((node.get("content") for node in metadata if _local(node.tag) == "meta" and node.get("name", "").lower() == "cover"), None)
        item = manifest.get(cover_id)
        if item and item["mime"] in _IMAGE_TYPES:
            cover = item["path"]
    # Some EPUB 2 files declare a cover XHTML instead of an image metadata id.
    if cover is None:
        guide = next((node for node in package if _local(node.tag) == "guide"), None)
        target = next((_reference(opf_path, node.get("href", "")) for node in ([] if guide is None else guide)
                       if node.get("type", "").lower() == "cover"), None)
        if target and path_types.get(target[0]) in _XHTML_TYPES:
            finder = _CoverFinder(target[0], path_types)
            finder.feed(_decode(archive.read(target[0], MAX_CHAPTER_BYTES)))
            cover = finder.cover
    def metadata_text(name):
        return next((_text(node) for node in ([] if metadata is None else metadata) if _local(node.tag) == name), "")
    image_pages, chapter_ranges = [], []
    for chapter in chapters:
        finder = _PageFinder(chapter["path"], path_types)
        finder.feed(_without_dtd(_decode(archive.read(chapter["path"], MAX_CHAPTER_BYTES))))
        finder.close()
        chapter_ranges.append(dict(start=len(image_pages), count=len(finder.pages)))
        image_pages.extend(finder.pages)
        if len(image_pages) > MAX_PAGES:
            raise ValueError("单本 EPUB 超过 10,000 张图片，请拆成分卷。")
    return dict(title=metadata_text("title"), author=metadata_text("creator"), language=metadata_text("language"),
                chapters=chapters, toc=toc, resources=resources, cover=cover,
                path_indexes=path_indexes, path_types=path_types,
                image_pages=image_pages, chapter_ranges=chapter_ranges)


class _PageFinder(HTMLParser):
    """Collect visible raster references in document order, without running HTML/SVG."""
    DROP = frozenset("head script style template noscript iframe object embed foreignobject defs symbol mask pattern clippath".split())
    VOID = frozenset("area base br col embed hr img input link meta param source track wbr image".split())

    def __init__(self, base, path_types):
        super().__init__(convert_charrefs=True)
        self.base, self.path_types = base, path_types
        self.pages, self.dropped, self.elements = [], [], 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower().split(":")[-1]
        self.elements += 1
        if self.elements > MAX_CHAPTER_ELEMENTS:
            raise ValueError("EPUB 单个页面文件的元素过多，请拆分文件。")
        if self.dropped or tag in self.DROP:
            if tag not in self.VOID:
                self.dropped.append(tag)
            return
        if tag not in ("img", "image"):
            return
        values = dict(attrs)
        reference = values.get("src") if tag == "img" else values.get("href", values.get("xlink:href"))
        target = _reference(self.base, reference)
        if target and self.path_types.get(target[0]) in _IMAGE_TYPES:
            if len(self.pages) >= MAX_PAGES:
                raise ValueError("单本 EPUB 超过 10,000 张图片，请拆成分卷。")
            self.pages.append(dict(path=target[0], mime=self.path_types[target[0]]))

    def handle_endtag(self, tag):
        tag = tag.lower().split(":")[-1]
        if tag in self.dropped:
            self.dropped = self.dropped[:len(self.dropped) - 1 - self.dropped[::-1].index(tag)]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def legacy_image_page(chapter, position, ranges, total):
    """Map the former chapter/scroll location to an image without changing source files."""
    if not ranges or total <= 0:
        return 0
    chapter = min(max(int(chapter), 0), len(ranges) - 1)
    position = position if type(position) in (int, float) and 0 <= position <= 1 else 0
    item = ranges[chapter]
    offset = min(max(0, item["count"] - 1), int(position * item["count"]))
    return min(total - 1, max(0, item["start"] + offset))


class _CoverFinder(HTMLParser):
    def __init__(self, base, path_types):
        super().__init__(convert_charrefs=True)
        self.base, self.path_types, self.cover = base, path_types, None

    def handle_starttag(self, tag, attrs):
        if self.cover is not None or tag.lower() not in ("img", "image", "svg:image"):
            return
        attrs = dict(attrs)
        target = _reference(self.base, attrs.get("src", attrs.get("href", attrs.get("xlink:href", ""))))
        if target and self.path_types.get(target[0]) in _IMAGE_TYPES:
            self.cover = target[0]


class _Sanitizer(HTMLParser):
    ALLOWED = frozenset("article section div p span h1 h2 h3 h4 h5 h6 blockquote pre code br hr ul ol li dl dt dd strong b em i u s small sub sup ruby rt rp table caption thead tbody tfoot tr td th figure figcaption a img".split())
    VOID = frozenset("area base br col embed hr img input link meta param source track wbr image".split())
    DROP = frozenset("script style head title form iframe object embed math canvas video audio foreignobject template noscript textarea select button input frame frameset applet link meta base".split())

    def __init__(self, metadata, base, book_id, revision):
        super().__init__(convert_charrefs=True)
        self.metadata, self.base, self.book_id, self.revision = metadata, base, book_id, revision
        self.output, self.stack, self.skipped = [], [], []
        self.output_bytes, self.element_count = 0, 0
        self.tokens = {value["path"]: token for token, value in metadata["resources"].items()}

    def handle_starttag(self, tag, attrs):
        self.element_count += 1
        if self.element_count > MAX_CHAPTER_ELEMENTS:
            raise ValueError("EPUB 单个章节的元素过多，请拆分章节。")
        tag = tag.lower()
        if self.skipped:
            if tag not in self.VOID:
                self.skipped.append(tag)
            return
        if tag in self.DROP:
            if tag not in self.VOID:
                self.skipped.append(tag)
            return
        if tag in ("image", "svg:image"):
            tag = "img"
            attrs = [("src" if key in ("href", "xlink:href") else key, value) for key, value in attrs]
        if tag not in self.ALLOWED:
            return
        raw = dict(attrs)
        clean = []
        for key in ("id", "title", "lang"):
            value = raw.get(key)
            if value and not any(ord(c) < 32 for c in value):
                clean.append((key, value[:256]))
        if raw.get("dir") in ("rtl", "ltr", "auto"):
            clean.append(("dir", raw["dir"]))
        classes = []
        raw_style = raw.get("style", "")
        for piece in (raw_style if isinstance(raw_style, str) else "").split(";")[:20]:
            key, sep, value = piece.partition(":")
            key, value = key.strip().lower(), value.strip().lower()
            allowed = {"text-align": {"left", "right", "center", "justify", "start", "end"},
                       "font-weight": {"normal", "bold", "400", "600", "700"},
                       "font-style": {"normal", "italic"}, "text-decoration": {"none", "underline", "line-through"}}
            if sep and value in allowed.get(key, set()):
                prefixes = {"text-align": "epub-align", "font-weight": "epub-weight", "font-style": "epub-font", "text-decoration": "epub-decoration"}
                classes.append(f"{prefixes[key]}-{value}")
        if classes:
            clean.append(("class", " ".join(dict.fromkeys(classes))))
        if tag == "a":
            target = _reference(self.base, raw.get("href", ""))
            if target and target[0] in self.metadata["path_indexes"]:
                clean.extend((("href", "#"), ("data-epub-index", str(self.metadata["path_indexes"][target[0]])),
                              ("data-epub-fragment", target[1])))
        elif tag == "img":
            target = _reference(self.base, raw.get("src", ""))
            token = self.tokens.get(target[0]) if target else None
            if token is None:
                return
            alt = raw.get("alt", "")
            clean.extend((("src", f"/api/books/{self.book_id}/epub/resources/{token}?v={self.revision}"),
                          ("alt", alt[:500] if isinstance(alt, str) else ""), ("loading", "lazy"), ("decoding", "async")))
        elif tag in ("td", "th"):
            for key in ("colspan", "rowspan"):
                value = raw.get(key, "")
                if isinstance(value, str) and len(value) <= 3 and value.isascii() and value.isdigit() and 1 <= int(value) <= 100:
                    clean.append((key, value))
        rendered = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in clean)
        self._append(f"<{tag}{rendered}>")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skipped:
            if tag in self.skipped:
                self.skipped = self.skipped[:len(self.skipped) - 1 - self.skipped[::-1].index(tag)]
            return
        if tag in self.stack:
            at = len(self.stack) - 1 - self.stack[::-1].index(tag)
            for item in reversed(self.stack[at:]):
                self._append(f"</{item}>")
            del self.stack[at:]

    def handle_data(self, data):
        if not self.skipped:
            self._append(escape(data, quote=False))

    def _append(self, value):
        self.output_bytes += len(value.encode("utf-8"))
        if self.output_bytes > MAX_RENDERED_CHAPTER_BYTES:
            raise ValueError("EPUB 章节展示内容超过安全上限，请拆分章节。")
        self.output.append(value)

    def result(self):
        for tag in reversed(self.stack):
            self._append(f"</{tag}>")
        self.stack.clear()
        return "".join(self.output)


class EpubManager:
    def __init__(self, library, error_type=None):
        self.library, self.error_type = library, error_type or EpubError
        self.lock, self.cache = threading.RLock(), OrderedDict()
        self.read_slots = threading.BoundedSemaphore(2)
        self.cache_bytes = 0
        self.stopped = False

    def _error(self, message, status=422):
        return self.error_type(message, status)

    def _check(self):
        stopped = getattr(self.library, "stop_event", None)
        if self.stopped or stopped and stopped.is_set():
            raise self._error("EPUB 读取已取消。", 409)

    def _info(self, handle):
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_EPUB_BYTES:
            raise self._error("EPUB 文件为空、过大或不是普通文件。")
        return info

    @contextmanager
    def _archive(self, handle):
        self._check()
        while not self.read_slots.acquire(timeout=.1):
            self._check()
        archive = None
        try:
            self._check()
            version = source_version(self._info(handle))
            archive = _Archive(handle)
            yield archive, version
            self._check()
            if source_version(self._info(handle)) != version:
                raise self._error("EPUB 文件已变化，请重新扫描写真库。", 409)
        except self.error_type:
            raise
        except (ValueError, UnicodeError, ET.ParseError, zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError, RecursionError) as error:
            message = str(error) if isinstance(error, ValueError) and str(error).startswith(("EPUB", "暂不支持", "单本 EPUB", "文件不是")) else "EPUB 文件损坏、内容不完整或格式不受支持。"
            raise self._error(message) from error
        finally:
            if archive is not None:
                archive.close()
            self.read_slots.release()

    def _metadata(self, archive, version):
        with self.lock:
            if version in self.cache:
                item = self.cache.pop(version)
                self.cache[version] = item
                return item[0]
        value = _manifest(archive)
        size = len(json.dumps(value, ensure_ascii=True).encode())
        if size > MAX_METADATA_CACHE:
            raise self._error("EPUB 目录过大，请拆分文件。")
        with self.lock:
            old = self.cache.pop(version, None)
            if old:
                self.cache_bytes -= old[1]
            self.cache[version] = (value, size)
            self.cache_bytes += size
            while len(self.cache) > MAX_CACHED_BOOKS or self.cache_bytes > MAX_METADATA_CACHE:
                _, removed = self.cache.popitem(last=False)
                self.cache_bytes -= removed[1]
        return value

    def inspect(self, handle):
        with self._archive(handle) as (archive, version):
            value = self._metadata(archive, version)
            if not value["image_pages"]:
                raise self._error("EPUB 中没有可阅读的写真图片，暂不支持纯文字 EPUB。")
            return dict(metadata_version=METADATA_VERSION, page_count=len(value["image_pages"]),
                        chapter_ranges=value["chapter_ranges"], title=value["title"], author=value["author"], language=value["language"],
                        chapters=[{key: chapter[key] for key in ("index", "title")} for chapter in value["chapters"]],
                        toc=[dict(item) for item in value["toc"]], source_version=version)

    def _open_book(self, book):
        if book.get("format") != "epub" or not re.fullmatch(r"[a-f0-9]{24}", book.get("id", "")):
            raise self._error("此作品不是 EPUB。", 404)
        root = self.library._directory_path(book["source_root"])
        return self.library._open_media_file(root, book["path"])

    @contextmanager
    def _book_archive(self, book):
        with self._open_book(book) as handle:
            with self._archive(handle) as (archive, version):
                if version != book.get("signature"):
                    raise self._error("EPUB 文件已变化，请重新扫描写真库。", 409)
                yield archive, self._metadata(archive, version), version
                # Atomic replacements leave an old descriptor valid. Reopen
                # through the library's safe boundary before releasing bytes.
                with self._open_book(book) as current:
                    if source_version(self._info(current)) != version:
                        raise self._error("EPUB 文件已变化，请重新扫描写真库。", 409)

    def chapter(self, book, index):
        if type(index) is not int or index < 0:
            raise self._error("EPUB 章节不存在。", 404)
        with self._book_archive(book) as (archive, value, version):
            if index >= len(value["chapters"]):
                raise self._error("EPUB 章节不存在。", 404)
            item = value["chapters"][index]
            text = _without_dtd(_decode(archive.read(item["path"], MAX_CHAPTER_BYTES)))
            parser = _Sanitizer(value, item["path"], book["id"], version[:24])
            parser.feed(text)
            parser.close()
            return dict(index=index, title=item["title"], html=parser.result(), source_version=version)

    def _image(self, archive, item, version):
        data = archive.read(item["path"], MAX_IMAGE_BYTES)
        # Declared media types and filename suffixes are not security checks.
        # Pillow identifies headers only here; no expensive pixel decode.
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(BytesIO(data)) as image:
                actual = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp", "AVIF": "image/avif", "BMP": "image/bmp"}.get(image.format)
                if actual not in _IMAGE_TYPES or image.width * image.height > 100_000_000:
                    raise ValueError()
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise self._error("EPUB 插图格式无效或尺寸过大。") from error
        etag = '"' + hashlib.sha256((version + "\0" + item["path"]).encode()).hexdigest() + '"'
        return data, actual, etag

    def resource(self, book, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise self._error("EPUB 插图不存在。", 404)
        with self._book_archive(book) as (archive, value, version):
            item = value["resources"].get(token)
            if item is None:
                raise self._error("EPUB 插图不存在。", 404)
            return self._image(archive, item, version)

    def image(self, book, index):
        if type(index) is not int or index < 0:
            raise self._error("EPUB 图片页码不存在。", 404)
        with self._book_archive(book) as (archive, value, version):
            if index >= len(value["image_pages"]):
                raise self._error("EPUB 图片页码不存在。", 404)
            return self._image(archive, value["image_pages"][index], version)

    def cover(self, book):
        with self._book_archive(book) as (archive, value, version):
            path = value["cover"]
            if path is None:
                return None
            return self._image(archive, dict(path=path), version)

    def close(self):
        with self.lock:
            self.stopped = True
            self.cache.clear()
            self.cache_bytes = 0

    def is_alive(self):
        return False

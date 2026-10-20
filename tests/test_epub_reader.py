"""Synthetic EPUB contracts; never touches a real library or running server."""
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

import epub_reader as epub


def png():
    output = BytesIO()
    Image.new("RGB", (30, 40), "pink").save(output, "PNG")
    return output.getvalue()


CONTAINER = b'''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
OPF = '''<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试 EPUB</dc:title><dc:creator>作者</dc:creator><dc:language>zh</dc:language>{extra_meta}</metadata><manifest><item id="a" href="text/a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="text/b.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/><item id="cover" href="images/cover.png" media-type="image/png" {cover_property}/>{extra_manifest}</manifest><spine {spine_attrs}><itemref idref="a"/><itemref idref="b"/></spine>{guide}</package>'''
NAV = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="text/a.xhtml">开篇</a><ol><li><a href="text/b.xhtml#part">第二章</a></li></ol></li></ol></nav></body></html>'''


def fixture(*, overrides=None, extra_manifest="", extra_meta="", cover_property='properties="cover-image"', spine_attrs="", guide="", omit=()):
    files = {
        "mimetype": b"application/epub+zip", "META-INF/container.xml": CONTAINER,
        "OPS/content.opf": OPF.format(extra_meta=extra_meta, cover_property=cover_property,
            extra_manifest=extra_manifest, spine_attrs=spine_attrs, guide=guide).encode(),
        "OPS/nav.xhtml": NAV.encode(), "OPS/text/a.xhtml": b'<html><head><title>Hidden title</title></head><body><h1 id="start">Hello</h1><p>World &amp; text</p><img src="../images/cover.png"/><a href="b.xhtml#part">Next</a></body></html>',
        "OPS/text/b.xhtml": b'<html><body><h2 id="part">Second</h2><img src="../images/cover.png"/></body></html>',
        "OPS/images/cover.png": png(),
    }
    files.update(overrides or {})
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            if name not in omit:
                archive.writestr(name, data)
    return output.getvalue()


class FakeLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.stop_event = threading.Event()
        self.opens = 0

    def _directory_path(self, value):
        if Path(value) != self.root:
            raise PermissionError()
        return self.root

    @contextmanager
    def _open_media_file(self, root, path):
        if root != self.root or path != "book.epub":
            raise PermissionError()
        self.opens += 1
        with (root / path).open("rb") as handle:
            yield handle


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "book.epub"
        self.library = FakeLibrary(self.root)
        self.manager = epub.EpubManager(self.library)
        self.addCleanup(self.manager.close)
        self.write(fixture())

    def write(self, data):
        self.original = data
        self.source.write_bytes(data)
        with self.source.open("rb") as handle:
            self.version = epub.source_version(os.fstat(handle.fileno()))
        self.book = dict(id="a" * 24, format="epub", source_root=str(self.root), path="book.epub", signature=self.version)

    def inspect(self):
        with self.source.open("rb") as handle:
            return self.manager.inspect(handle)

    def test_epub3_metadata_order_navigation_and_serializable_contract(self):
        result = self.inspect()
        self.assertEqual(result["title"], "测试 EPUB")
        self.assertEqual(result["author"], "作者")
        self.assertEqual(result["language"], "zh")
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["chapters"], [dict(index=0, title="开篇"), dict(index=1, title="第二章")])
        self.assertEqual(result["toc"][1], dict(index=1, title="第二章", fragment="part", depth=1))
        self.assertEqual(result["source_version"], self.version)
        self.assertNotIn("path", json.dumps(result))
        self.assertEqual(epub.page_key(0), "epub:000001")
        for bad in (-1, True, epub.MAX_PAGES):
            with self.assertRaises(ValueError):
                epub.page_key(bad)

    def test_chapter_images_and_navigation_are_inert_local_references(self):
        result = self.manager.chapter(self.book, 0)
        self.assertIn('<h1 id="start">Hello</h1>', result["html"])
        self.assertIn("World &amp; text", result["html"])
        self.assertNotIn("Hidden title", result["html"])
        self.assertIn('href="#" data-epub-index="1" data-epub-fragment="part"', result["html"])
        self.assertIn(f'/api/books/{self.book["id"]}/epub/resources/', result["html"])
        self.assertIn(f'?v={self.version[:24]}', result["html"])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_resource_type_uses_actual_image_and_versions_etag(self):
        token = epub.hashlib.sha256(b"OPS/images/cover.png").hexdigest()[:32]
        data, mime, etag = self.manager.resource(self.book, token)
        self.assertEqual((data, mime), (png(), "image/png"))
        self.assertEqual(len(etag), 66)
        self.assertEqual(self.manager.cover(self.book), (data, mime, etag))
        self.assertGreaterEqual(self.library.opens, 4)

    def test_sanitizer_removes_active_content_remote_refs_and_dangerous_attributes(self):
        text = '''<html><head><script>bad head</script></head><body onload="evil()"><script>alert(1)</script><p id="ok" onclick="bad()" class="app" style="text-align:center;position:fixed;background:url(https://evil);font-weight:bold">ok</p><form><p>hidden form</p></form><iframe src="https://evil">hidden frame</iframe><object>hidden object</object><a href="javascript:alert(1)">bad</a><a href="https://evil">remote</a><img src="https://evil/x"><img src="data:image/svg+xml,test"><img src="file:///secret"><img srcset="https://evil" src="../images/cover.png" onerror="evil()"><svg><image xlink:href="../images/cover.png"/><path d="abc"/><foreignObject><p>bad foreign</p></foreignObject></svg><p>&lt;script&gt;safe&lt;/script&gt;</p></body></html>'''
        self.write(fixture(overrides={"OPS/text/a.xhtml": text.encode()}))
        html = self.manager.chapter(self.book, 0)["html"]
        self.assertIn('class="epub-align-center epub-weight-bold"', html)
        self.assertIn("&lt;script&gt;safe&lt;/script&gt;", html)
        self.assertEqual(html.count("<img "), 2)
        for forbidden in ("onclick", "onerror", "onload", "srcset", "https:", "javascript:", "file:", "data:", "position:", "background:", "<script", "<iframe", "<object", "<svg", "<path", "hidden", "bad foreign", 'class="app"'):
            self.assertNotIn(forbidden, html)

    def test_missing_cover_returns_none(self):
        self.write(fixture(cover_property=""))
        self.assertIsNone(self.manager.cover(self.book))

    def test_malformed_attributes_and_ipv6_urls_remain_inert(self):
        text = b'<p style>text</p><a href="http://[">bad link</a><img src="../images/cover.png" alt><table><tr><td colspan rowspan="9999999999999999999999999">cell</td></tr></table>'
        self.write(fixture(overrides={"OPS/text/a.xhtml": text}))
        html = self.manager.chapter(self.book, 0)["html"]
        self.assertIn("<p>text</p>", html)
        self.assertIn("<a>bad link</a>", html)
        self.assertIn('alt=""', html)
        self.assertIn("<td>cell</td>", html)

    def test_epub2_metadata_cover_and_ncx(self):
        ncx = b'''<ncx><navMap><navPoint><navLabel><text>NCX chapter</text></navLabel><content src="text/a.xhtml#start"/><navPoint><navLabel><text>Nested</text></navLabel><content src="text/b.xhtml"/></navPoint></navPoint></navMap></ncx>'''
        self.write(fixture(cover_property="", extra_meta='<meta name="cover" content="cover"/>',
            extra_manifest='<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            spine_attrs='toc="ncx"', overrides={"OPS/toc.ncx": ncx, "OPS/nav.xhtml": b"<html/>"}))
        result = self.inspect()
        self.assertEqual(result["chapters"][0]["title"], "NCX chapter")
        self.assertEqual(result["toc"][1]["depth"], 1)
        self.assertEqual(self.manager.cover(self.book)[1], "image/png")

    def test_epub2_guide_cover_xhtml_image(self):
        self.write(fixture(cover_property="", guide='<guide><reference type="cover" href="text/a.xhtml"/></guide>'))
        self.assertEqual(self.manager.cover(self.book)[1], "image/png")

    def test_font_obfuscation_is_ignored_and_fonts_never_served(self):
        encryption = b'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#"><EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/><CipherData><CipherReference URI="OPS/font.otf"/></CipherData></EncryptedData></encryption>'''
        self.write(fixture(extra_manifest='<item id="font" href="font.otf" media-type="font/otf"/>',
            overrides={"META-INF/encryption.xml": encryption, "OPS/font.otf": b"obfuscated"}))
        self.assertEqual(self.inspect()["page_count"], 2)
        token = epub.hashlib.sha256(b"OPS/font.otf").hexdigest()[:32]
        with self.assertRaises(epub.EpubError) as context:
            self.manager.resource(self.book, token)
        self.assertEqual(context.exception.status, 404)

    def test_drm_or_fake_font_obfuscation_on_content_is_rejected(self):
        for algorithm, target in (("http://www.w3.org/2001/04/xmlenc#aes256-cbc", "OPS/text/a.xhtml"),
                                  ("http://www.idpf.org/2008/embedding", "OPS/text/a.xhtml")):
            encryption = f'<encryption><EncryptedData><EncryptionMethod Algorithm="{algorithm}"/><CipherReference URI="{target}"/></EncryptedData></encryption>'.encode()
            self.write(fixture(overrides={"META-INF/encryption.xml": encryption}))
            with self.assertRaisesRegex(epub.EpubError, "DRM"):
                self.inspect()

    def test_svg_resource_is_never_served_even_when_declared_png(self):
        self.write(fixture(overrides={"OPS/images/cover.png": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'}))
        with self.assertRaisesRegex(epub.EpubError, "插图格式"):
            self.manager.cover(self.book)

    def test_wrong_image_suffix_is_safe_actual_raster(self):
        output = BytesIO()
        Image.new("RGB", (4, 5)).save(output, "JPEG")
        self.write(fixture(overrides={"OPS/images/cover.png": output.getvalue()}))
        self.assertEqual(self.manager.cover(self.book)[1], "image/jpeg")

    def test_external_or_escaping_manifest_references_are_rejected(self):
        for path in ("https://evil/book", "../../secret", "file:///etc/passwd", "%2e%2e/%2e%2e/secret", "C:/secret"):
            opf = OPF.format(extra_meta="", cover_property="", extra_manifest="", spine_attrs="", guide="").replace("text/a.xhtml", path)
            self.write(fixture(overrides={"OPS/content.opf": opf.encode()}))
            with self.assertRaises(epub.EpubError):
                self.inspect()

    def test_xml_entities_dtd_and_utf16_entities_rejected(self):
        unsafe = '<!DOCTYPE package [<!ENTITY x "boom">]><package>&x;</package>'
        for value in (unsafe.encode(), unsafe.encode("utf-16")):
            self.write(fixture(overrides={"OPS/content.opf": value}))
            with self.assertRaisesRegex(epub.EpubError, "DTD"):
                self.inspect()
        self.write(fixture(overrides={"OPS/text/a.xhtml": unsafe.encode()}))
        with self.assertRaisesRegex(epub.EpubError, "DTD"):
            self.manager.chapter(self.book, 0)
        self.write(fixture(overrides={"OPS/content.opf": unsafe.encode("utf-32")}))
        with self.assertRaises(epub.EpubError):
            self.inspect()

    def test_html5_doctype_is_removed(self):
        self.write(fixture(overrides={"OPS/text/a.xhtml": b'<!DOCTYPE html><html><body><p>hello</p></body></html>'}))
        self.assertEqual(self.manager.chapter(self.book, 0)["html"], "<p>hello</p>")

    def test_external_epub2_doctypes_are_discarded_without_network_resolution(self):
        chapter = b'''<?xml version="1.0"?><!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"><html xmlns="http://www.w3.org/1999/xhtml"><body><p>One&nbsp;two</p></body></html>'''
        nav = ('<!DOCTYPE html SYSTEM "https://invalid.example/unreachable.dtd">' + NAV.replace("开篇", "第一&nbsp;章")).encode()
        self.write(fixture(overrides={"OPS/text/a.xhtml": chapter, "OPS/nav.xhtml": nav}))
        self.assertEqual(self.inspect()["chapters"][0]["title"], "第一 章")
        self.assertIn("One\xa0two", self.manager.chapter(self.book, 0)["html"])

    def test_zip_paths_duplicates_and_symlinks_rejected(self):
        for name in ("../secret", "/absolute", "OPS/../bad", "OPS//bad", "C:bad", "OPS/CONTENT.opf"):
            self.write(fixture(overrides={name: b"x"}))
            with self.assertRaises(epub.EpubError):
                self.inspect()
        # ZipInfo normalizes backslashes on Windows when writing; forge the
        # on-disk directory and local-header names instead to test a real ZIP.
        self.write(fixture(overrides={"OPS/bad": b"x"}).replace(b"OPS/bad", b"OPS\\bad"))
        with self.assertRaises(epub.EpubError):
            self.inspect()
        output = BytesIO(fixture())
        with zipfile.ZipFile(output, "a") as archive:
            info = zipfile.ZipInfo("OPS/symlink")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, b"/secret")
        self.write(output.getvalue())
        with self.assertRaisesRegex(epub.EpubError, "链接"):
            self.inspect()

    def test_ratio_member_limit_and_xml_limit(self):
        self.write(fixture(overrides={"OPS/large.txt": b"x" * (2 * epub.MIB)}))
        with self.assertRaisesRegex(epub.EpubError, "压缩比例"):
            self.inspect()
        self.write(fixture())
        with patch.object(epub, "MAX_XML_BYTES", 20):
            with self.assertRaisesRegex(epub.EpubError, "安全上限"):
                self.inspect()
        with patch.object(epub, "MAX_MEMBERS", 2):
            with self.assertRaisesRegex(epub.EpubError, "目录过大"):
                self.inspect()

    def test_declared_spine_and_member_existence_validation(self):
        self.write(fixture(omit=("OPS/text/b.xhtml",)))
        with self.assertRaises(epub.EpubError):
            self.inspect()
        self.write(fixture(overrides={"OPS/content.opf": OPF.format(extra_meta="", cover_property="", extra_manifest="", spine_attrs="", guide="").replace('idref="b"', 'idref="a"').encode()}))
        with self.assertRaisesRegex(epub.EpubError, "重复章节"):
            self.inspect()

    def test_bad_token_index_and_non_epub_never_open_sources(self):
        for value in ("../secret", "abc", "a" * 32 + "?x", "A" * 32):
            with self.assertRaises(epub.EpubError) as context:
                self.manager.resource(self.book, value)
            self.assertEqual(context.exception.status, 404)
        for index in (-1, True, "0", 2):
            with self.assertRaises(epub.EpubError):
                self.manager.chapter(self.book, index)
        with self.assertRaises(epub.EpubError):
            self.manager.cover({**self.book, "format": "pdf"})

    def test_source_changed_in_place_or_replaced_requires_rescan_even_on_cache_hit(self):
        self.inspect()
        old = dict(self.book)
        self.write(fixture(overrides={"OPS/text/a.xhtml": b"<p>replacement</p>"}))
        for method in (lambda: self.manager.chapter(old, 0), lambda: self.manager.cover(old)):
            with self.assertRaises(epub.EpubError) as context:
                method()
            self.assertEqual(context.exception.status, 409)
        self.assertIn("replacement", self.manager.chapter(self.book, 0)["html"])

    def test_atomic_source_change_while_request_is_reading_is_detected(self):
        # Model an atomic replacement between two safe opens. Windows denies
        # os.replace on a file currently opened by the ordinary Python CRT.
        replacement = self.root / "new.epub"
        replacement.write_bytes(fixture(overrides={"OPS/text/a.xhtml": b"<p>new</p>"}))
        count = 0
        @contextmanager
        def reopen(book):
            nonlocal count
            count += 1
            with (self.source if count == 1 else replacement).open("rb") as handle:
                yield handle
        with patch.object(self.manager, "_open_book", reopen):
            with self.assertRaises(epub.EpubError) as context:
                self.manager.chapter(self.book, 0)
            self.assertEqual(context.exception.status, 409)

    def test_metadata_cache_is_bounded_and_close_cancelled(self):
        with patch.object(epub, "MAX_CACHED_BOOKS", 2):
            for number in range(4):
                self.write(fixture(overrides={"OPS/nav.xhtml": NAV.replace("开篇", f"章 {number}").encode()}))
                self.inspect()
            self.assertEqual(len(self.manager.cache), 2)
        self.assertLess(self.manager.cache_bytes, epub.MAX_METADATA_CACHE)
        self.manager.close()
        self.assertFalse(self.manager.is_alive())
        self.assertEqual(self.manager.cache_bytes, 0)
        with self.assertRaises(epub.EpubError) as context:
            self.inspect()
        self.assertEqual(context.exception.status, 409)

    def test_expanded_html_and_element_counts_are_bounded(self):
        self.write(fixture(overrides={"OPS/text/a.xhtml": b'<p>text &lt; &gt; &amp;</p>'}))
        with patch.object(epub, "MAX_RENDERED_CHAPTER_BYTES", 8):
            with self.assertRaisesRegex(epub.EpubError, "安全上限"):
                self.manager.chapter(self.book, 0)
        with patch.object(epub, "MAX_CHAPTER_ELEMENTS", 0):
            with self.assertRaisesRegex(epub.EpubError, "元素过多"):
                self.manager.chapter(self.book, 0)

    def test_busy_read_lane_observes_service_stop(self):
        self.manager.read_slots.acquire()
        self.manager.read_slots.acquire()
        results = []
        def read():
            try:
                self.inspect()
            except epub.EpubError as error:
                results.append(error.status)
        thread = threading.Thread(target=read)
        thread.start()
        self.library.stop_event.set()
        thread.join(timeout=2)
        self.manager.read_slots.release()
        self.manager.read_slots.release()
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [409])

    def test_inspection_reads_spine_markup_without_decoding_image_pixels(self):
        self.write(fixture(overrides={"OPS/text/a.xhtml": b"not an html document"}))
        with patch.object(epub._Archive, "read", autospec=True, side_effect=epub._Archive.read) as mocked:
            result = self.inspect()
        self.assertEqual(result["page_count"], 1)
        self.assertIn("OPS/text/a.xhtml", [call.args[1] for call in mocked.call_args_list])
        self.assertNotIn("OPS/images/cover.png", [call.args[1] for call in mocked.call_args_list])

    def test_picture_collection_uses_svg_and_image_order_without_prepending_cover(self):
        markup = b'''<html><body><svg><defs><image href="../images/cover.png"/></defs><image xlink:href="../images/second.png"/></svg><template><img src="../images/cover.png"/></template><img src="../images/cover.png"/><img src="https://invalid.example/a.png"/></body></html>'''
        photo = BytesIO()
        Image.new("RGB", (10, 20), "blue").save(photo, "PNG")
        self.write(fixture(extra_manifest='<item id="second" href="images/second.png" media-type="image/png"/>',
                           overrides={"OPS/text/a.xhtml": markup, "OPS/text/b.xhtml": b'<p>Credits only</p>',
                                      "OPS/images/second.png": photo.getvalue()}))
        result = self.inspect()
        self.assertEqual(result["metadata_version"], 2)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["chapter_ranges"], [dict(start=0, count=2), dict(start=2, count=0)])
        self.assertEqual(self.manager.image(self.book, 0)[0], photo.getvalue())
        self.assertEqual(self.manager.image(self.book, 1)[0], png())
        with self.assertRaises(epub.EpubError):
            self.manager.image(self.book, 2)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_pure_text_is_not_catalogued_as_picture_pages(self):
        self.write(fixture(overrides={"OPS/text/a.xhtml": b'<p>Text</p>', "OPS/text/b.xhtml": b'<p>More text</p>'}))
        with self.assertRaisesRegex(epub.EpubError, "没有可阅读的写真图片"):
            self.inspect()

    def test_pure_svg_spine_extracts_raster_reference_without_serving_svg_code(self):
        opf = OPF.format(extra_meta="", cover_property='', extra_manifest='', spine_attrs='', guide='')
        opf = opf.replace('href="text/a.xhtml" media-type="application/xhtml+xml"', 'href="text/a.xhtml" media-type="image/svg+xml"')
        self.write(fixture(overrides={"OPS/content.opf": opf.encode(),
            "OPS/text/a.xhtml": b'<svg xmlns="http://www.w3.org/2000/svg"><script>notExecuted()</script><image href="../images/cover.png"/></svg>'}))
        self.assertEqual(self.inspect()["page_count"], 2)
        self.assertEqual(self.manager.image(self.book, 0)[0], png())

    def test_image_page_count_is_bounded_and_legacy_positions_map_inside_chapters(self):
        with patch.object(epub, "MAX_PAGES", 1):
            with self.assertRaisesRegex(epub.EpubError, "10,000"):
                self.inspect()
        ranges = [dict(start=0, count=2), dict(start=2, count=0), dict(start=2, count=3)]
        self.assertEqual(epub.legacy_image_page(0, .75, ranges, 5), 1)
        self.assertEqual(epub.legacy_image_page(1, .5, ranges, 5), 2)
        self.assertEqual(epub.legacy_image_page(2, 1, ranges, 5), 4)


if __name__ == "__main__":
    unittest.main()

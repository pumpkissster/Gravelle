"""Synthetic PDF fixtures only; no personal library, NAS, or running service."""
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf_reader
from pdf_reader import PdfManager, MAX_PAGES, source_version, page_key, page_index, width_bucket


class PdfError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def pdf_bytes(pages=((240, 360, 0, "1 0 0"),), *, encrypted=False, no_user_password=False):
    """Small vector PDF, including a real standard-security RC4 test variant."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for width, height, rotation, colour in pages:
        number = len(objects) + 1
        kids.append(f"{number} 0 R")
        content = f"{colour} rg 0 0 {width} {height} re f\n".encode()
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Rotate {rotation} /Contents {number + 1} 0 R >>".encode())
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream")
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    suffix = b""
    if encrypted:
        padding = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")
        def pad(password):
            return (password + padding)[:32]
        def rc4(key, value):
            state = list(range(256))
            j = 0
            for i in range(256):
                j = (j + state[i] + key[i % len(key)]) & 255
                state[i], state[j] = state[j], state[i]
            out, i, j = bytearray(), 0, 0
            for byte in value:
                i = (i + 1) & 255
                j = (j + state[i]) & 255
                state[i], state[j] = state[j], state[i]
                out.append(byte ^ state[(state[i] + state[j]) & 255])
            return bytes(out)
        user = b"" if no_user_password else b"reader-test-password"
        owner = rc4(hashlib.md5(pad(b"owner-test-password")).digest()[:5], pad(user))
        ident, permissions = b"0123456789abcdef", -4
        key = hashlib.md5(pad(user) + owner + struct.pack("<i", permissions) + ident).digest()[:5]
        user_check = rc4(key, padding)
        for index in range(3, len(objects), 2):
            number = index + 1
            body = objects[index].split(b"stream\n", 1)[1].rsplit(b"endstream", 1)[0]
            object_key = hashlib.md5(key + number.to_bytes(3, "little") + b"\0\0").digest()[:10]
            body = rc4(object_key, body)
            objects[index] = f"<< /Length {len(body)} >>\nstream\n".encode() + body + b"endstream"
        number = len(objects) + 1
        objects.append(f"<< /Filter /Standard /V 1 /R 2 /Length 40 /O <{owner.hex()}> /U <{user_check.hex()}> /P {permissions} >>".encode())
        suffix = f" /Encrypt {number} 0 R /ID [<{ident.hex()}> <{ident.hex()}>]".encode()
    result, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{value:010d} 00000 n \n".encode() for value in offsets[1:])
    result += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R".encode() + suffix + f" >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(result)


class FakeCache:
    def __init__(self):
        self.limit = 1024
        self.providers = {}
    def settings(self):
        return dict(pdf_limit_mb=self.limit)
    def register_category(self, name, provider):
        self.providers[name] = provider


class FakeLibrary:
    def __init__(self, root):
        self.root = root / "media"
        self.root.mkdir()
        self.data = root / "data"
        self.data.mkdir()
        self.stop_event = threading.Event()
        self.cache_manager = FakeCache()
        with self.connect() as connection:
            connection.execute("CREATE TABLE books(id TEXT,signature TEXT,active INTEGER,removed INTEGER,format TEXT)")
    def connect(self):
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()
        connection = sqlite3.connect(self.data / "test.sqlite3", factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection
    def _directory_path(self, value):
        if Path(value) != self.root:
            raise PdfError("Invalid root")
        return self.root
    @contextmanager
    def _open_media_file(self, root, relative):
        path = root / relative
        if path.parent != root or path.is_symlink():
            raise PdfError("Invalid source")
        with path.open("rb") as handle:
            yield handle


class PdfReaderTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "pdf-reader-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="pdf-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.app = FakeLibrary(self.folder)
        self.pdf = self.app.root / "test.pdf"
        self.pdf.write_bytes(pdf_bytes())
        self.manager = PdfManager(self.app, PdfError)
        self.addCleanup(self.manager.close)
        self.book = self.book_info()

    def book_info(self, count=1):
        with self.pdf.open("rb") as handle:
            version = source_version(os.fstat(handle.fileno()))
        value = dict(id="a" * 24, source_root=str(self.app.root), path="test.pdf", format="pdf",
                     page_count=count, pdf_source_version=version, signature=version)
        with self.app.connect() as connection:
            connection.execute("DELETE FROM books")
            connection.execute("INSERT INTO books VALUES(?,?,1,0,'pdf')", (value["id"], version))
        return value

    def inspect(self):
        with self.pdf.open("rb") as handle:
            return self.manager.inspect(handle)

    def error(self, message, operation, *args, **kwargs):
        with self.assertRaisesRegex(PdfError, message):
            operation(*args, **kwargs)

    def test_inspection_and_render_are_read_only(self):
        before = self.pdf.read_bytes()
        report = self.inspect()
        self.assertEqual(report["page_count"], 1)
        self.assertEqual(report["sizes"], [[240., 360.]])
        self.assertEqual(report["source_version"], self.book["signature"])
        data, mime, etag = self.manager.image(self.book, 0, 600)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(etag, '"' + hashlib.sha256(data).hexdigest() + '"')
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (600, 900))
            self.assertGreater(image.getpixel((300, 450))[0], 240)
        self.assertEqual(self.pdf.read_bytes(), before)

    def test_rotated_and_mixed_pages(self):
        self.pdf.write_bytes(pdf_bytes(((240, 360, 90, "1 0 0"), (600, 300, 0, "0 1 0"))))
        self.book = self.book_info(2)
        self.assertEqual(self.inspect()["sizes"], [[360., 240.], [600., 300.]])
        for index, expected in ((0, (600, 400)), (1, (600, 300))):
            data, _, _ = self.manager.image(self.book, index, 600)
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.size, expected)

    def test_large_page_render_is_bounded(self):
        self.pdf.write_bytes(pdf_bytes(((10_000, 10_000, 0, "0 0 1"),)))
        self.book = self.book_info()
        data, _, _ = self.manager.image(self.book, 0, 4096)
        with Image.open(BytesIO(data)) as image:
            self.assertLessEqual(max(image.size), 4096)
            self.assertLessEqual(image.width * image.height, 16_000_000)

    def test_encrypted_documents_are_rejected(self):
        for blank in (False, True):
            with self.subTest(empty_user_password=blank):
                self.pdf.write_bytes(pdf_bytes(encrypted=True, no_user_password=blank))
                self.error("加密", self.inspect)

    def test_invalid_and_empty_documents(self):
        for payload in (b"not a PDF", b"%PDF-1.4\nbroken\n", pdf_bytes(())):
            with self.subTest(payload=payload[:24]):
                self.pdf.write_bytes(payload)
                self.error("无效|损坏|没有", self.inspect)

    def test_input_size_limit_before_copy(self):
        with patch.object(pdf_reader, "MAX_PDF_BYTES", 8), patch.object(self.manager, "_private_source") as copied:
            self.error("512 MiB", self.inspect)
            copied.assert_not_called()

    def test_page_count_limit(self):
        self.pdf.write_bytes(pdf_bytes(((10, 10, 0, "0 0 0"),) * (MAX_PAGES + 1)))
        self.error("10,000", self.inspect)

    def test_cache_hit_never_starts_decoder(self):
        expected = self.manager.image(self.book, 0)
        with patch.object(self.manager, "_run_worker", side_effect=AssertionError("Cache miss")):
            self.assertEqual(self.manager.image(self.book, 0), expected)
        self.assertEqual(self.manager.snapshot()["files"], 1)
        self.assertIs(self.app.cache_manager.providers["pdf"], self.manager)

    def test_different_widths_have_separate_bounded_cache(self):
        first = self.manager.image(self.book, 0, 600)[0]
        second = self.manager.image(self.book, 0, 800)[0]
        self.assertNotEqual(first, second)
        self.assertEqual(self.manager.snapshot()["files"], 2)

    def test_disabled_cache_still_renders_without_persistence(self):
        self.app.cache_manager.limit = 0
        self.manager.image(self.book, 0)
        self.assertEqual(self.manager.snapshot()["files"], 0)

    def test_disabling_cache_during_render_prevents_write(self):
        original = self.manager._run_worker
        def rendered(*args, **kwargs):
            result = original(*args, **kwargs)
            self.app.cache_manager.limit = 0
            return result
        with patch.object(self.manager, "_run_worker", side_effect=rendered):
            self.manager.image(self.book, 0)
        self.assertEqual(self.manager.snapshot()["files"], 0)

    def test_clear_during_render_never_resurrects_old_cache(self):
        original = self.manager._run_worker
        def rendered(*args, **kwargs):
            result = original(*args, **kwargs)
            self.manager.clear_cache()
            return result
        with patch.object(self.manager, "_run_worker", side_effect=rendered):
            self.manager.image(self.book, 0)
        self.assertEqual(self.manager.snapshot()["files"], 0)

    def test_source_change_invalidates_even_a_cache_hit(self):
        self.manager.image(self.book, 0)
        self.pdf.write_bytes(pdf_bytes(((240, 360, 0, "0 1 0"),)))
        self.error("已变化", self.manager.image, self.book, 0)

    def test_source_replaced_during_render_discards_result(self):
        original = self.manager._run_worker
        def rendered(*args, **kwargs):
            result = original(*args, **kwargs)
            # Windows does not permit replacing every already-open handle;
            # mutation also exercises before/after source stamps portably.
            with self.pdf.open("r+b") as source:
                source.seek(15)
                source.write(b" ")
            return result
        with patch.object(self.manager, "_run_worker", side_effect=rendered):
            self.error("已变化", self.manager.image, self.book, 0)
        self.assertEqual(self.manager.snapshot()["files"], 0)

    def test_scan_copy_is_reused_between_pages(self):
        self.inspect()
        snapshot = self.manager._input
        self.manager.image(self.book, 0, 600)
        self.assertEqual(self.manager._input, snapshot)

    def test_invalid_cached_image_is_regenerated(self):
        self.manager.image(self.book, 0)
        next(self.manager.root.glob("*.jpg")).write_bytes(b"not-jpeg")
        data, _, _ = self.manager.image(self.book, 0)
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_clear_owns_only_ordinary_files(self):
        self.manager.image(self.book, 0)
        foreign = self.manager.root / "do-not-remove.txt"
        foreign.write_bytes(b"keep")
        result = self.manager.clear_cache()
        self.assertEqual(result["files"], 1)
        self.assertEqual(foreign.read_bytes(), b"keep")
        self.assertIsNone(self.manager._input)

    def test_hardlinked_cache_is_not_read_or_removed(self):
        self.manager.image(self.book, 0)
        cache = next(self.manager.root.glob("*.jpg"))
        target = self.folder / "preserved.jpg"
        try:
            os.link(cache, target)
        except (PermissionError, NotImplementedError):
            self.skipTest("Host sandbox forbids creating hard links")
        self.assertEqual(self.manager.snapshot()["files"], 0)
        self.assertEqual(self.manager.clear_cache()["files"], 0)
        self.assertTrue(target.exists())
        self.assertTrue(cache.exists())

    def test_symlink_cache_root_is_rejected(self):
        if os.name == "nt":
            self.skipTest("Windows symlink creation requires a host privilege")
        self.manager.close()
        self.manager.root.rmdir()
        target = self.folder / "external"
        target.mkdir()
        self.manager.root.symlink_to(target, target_is_directory=True)
        self.error("不安全", PdfManager, self.app, PdfError)

    def test_prune_obsolete_books_and_versions(self):
        self.manager.image(self.book, 0)
        with self.app.connect() as connection:
            connection.execute("UPDATE books SET active=0")
        self.manager.prune()
        self.assertEqual(self.manager.snapshot()["files"], 0)

    def test_lru_eviction_uses_bounded_owned_entries(self):
        data = self.manager.image(self.book, 0, 600)[0]
        self.manager.image(self.book, 0, 800)
        with self.manager.lock:
            self.manager._trim(len(data))
        rows = self.manager._entries()
        self.assertLessEqual(sum(info.st_size for _, info in rows), len(data))
        self.assertFalse(any("-0600-" in path.name for path, _ in rows))

    def test_timeout_kills_private_child_and_retains_no_output(self):
        with patch.object(self.manager, "_worker_command", return_value=[sys.executable, "-c", "import time; time.sleep(10)"]), patch.object(pdf_reader, "TIMEOUT_SECONDS", .15):
            began = time.monotonic()
            self.error("超时", self.inspect)
            self.assertLess(time.monotonic() - began, 3)
        self.assertFalse(self.manager.is_alive())
        self.assertFalse(self.manager.processes)
        self.assertEqual(self.manager.snapshot()["files"], 0)

    def test_close_cancels_new_work(self):
        self.manager.close()
        self.error("取消", self.inspect)
        self.assertFalse(self.manager.is_alive())

    def test_page_manifest_and_width_validation(self):
        self.assertEqual(page_key(0), "pdf:000001")
        self.assertEqual(page_index("pdf:010000"), 9999)
        self.assertEqual(width_bucket(601), 800)
        for value in (-1, True, 10000, "0"):
            with self.assertRaises(ValueError):
                page_key(value)
        for value in ("pdf:000000", "pdf:010001", "../source.pdf", "pdf:1"):
            with self.assertRaises(ValueError):
                page_index(value)
        for value in (0, 4097, True, "1600"):
            self.error("宽度", self.manager.image, self.book, 0, value)

    def test_frozen_and_source_worker_dispatch(self):
        self.assertEqual(self.manager._worker_command(), [sys.executable, str(ROOT / "pdf_reader.py")])
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(self.manager._worker_command(), [sys.executable, "--pdf-worker"])

    def test_cross_api_windows_ctime_does_not_weaken_identity(self):
        values = dict(st_mode=0o100600, st_nlink=1, st_dev=1, st_ino=20, st_size=100,
                      st_mtime_ns=500, st_ctime_ns=600)
        path_info = SimpleNamespace(**values)
        opened_info = SimpleNamespace(**{**values, "st_ctime_ns": 200})
        self.assertNotEqual(source_version(path_info), source_version(opened_info))
        self.assertTrue(pdf_reader._same_opened_file(path_info, opened_info))
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
            changed = SimpleNamespace(**{**values, field: values[field] + 1})
            self.assertFalse(pdf_reader._same_opened_file(path_info, changed))
        linked = SimpleNamespace(**{**values, "st_nlink": 2})
        self.assertFalse(pdf_reader._same_opened_file(path_info, linked))

    def test_startup_removes_only_owned_plain_abandoned_files(self):
        stale = self.manager.root / ".input-abcdefgh"
        stale.mkdir()
        (stale / "source.pdf").write_bytes(b"old-private-source")
        transient = self.manager.root / (".tmp-" + "b" * 24)
        transient.write_bytes(b"incomplete-page")
        unknown = self.manager.root / ".input-foreign12"
        unknown.mkdir()
        (unknown / "do-not-remove.bin").write_bytes(b"foreign")
        another = PdfManager(self.app, PdfError)
        self.addCleanup(another.close)
        self.assertFalse(stale.exists())
        self.assertFalse(transient.exists())
        self.assertEqual((unknown / "do-not-remove.bin").read_bytes(), b"foreign")

    def test_idle_private_input_expires_but_active_page_remains(self):
        self.manager.image(self.book, 0, 600)
        directory = self.manager._input[0]
        self.manager._input_used = time.monotonic() - pdf_reader.INPUT_IDLE_SECONDS - 1
        self.manager.prune()
        self.assertIsNone(self.manager._input)
        self.assertFalse(directory.exists())
        self.assertEqual(self.manager.snapshot()["files"], 1)

    def test_all_decoders_share_one_lane(self):
        active, peak, errors = 0, 0, []
        counters = threading.Lock()
        original = self.manager._run_worker
        def rendered(*args, **kwargs):
            nonlocal active, peak
            with counters:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.05)
                return original(*args, **kwargs)
            finally:
                with counters:
                    active -= 1
        def render(width):
            try:
                self.manager.image(self.book, 0, width)
            except BaseException as error:
                errors.append(error)
        with patch.object(self.manager, "_run_worker", side_effect=rendered):
            threads = [threading.Thread(target=render, args=(width,)) for width in (600, 800)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(peak, 1)

    def test_memory_guard_exit_is_distinct_from_corrupt_pdf(self):
        command = [sys.executable, "-c", "raise SystemExit(86)"]
        with patch.object(self.manager, "_worker_command", return_value=command):
            self.error("内存", self.inspect)
        self.assertFalse(self.manager.processes)

    def test_close_terminates_only_its_private_pdf_child(self):
        errors = []
        def inspect():
            try:
                self.inspect()
            except PdfError as error:
                errors.append(error)
        command = [sys.executable, "-c", "import time; time.sleep(10)"]
        with patch.object(self.manager, "_worker_command", return_value=command):
            thread = threading.Thread(target=inspect)
            thread.start()
            deadline = time.monotonic() + 3
            while not self.manager.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.manager.is_alive())
            self.manager.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.manager.processes)
        self.assertIsNone(self.manager._input)

    def test_renderer_is_loaded_only_in_worker(self):
        # Packaging tests intentionally import the native renderer. Check the
        # service's boundary in a fresh interpreter, independently of test order.
        result = subprocess.run([sys.executable, "-c",
            "import sys; import pdf_reader; import server; "
            "assert 'pypdfium2' not in sys.modules; assert 'pypdfium2_raw' not in sys.modules"],
            cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.inspect()
        source = (ROOT / "pdf_reader.py").read_text(encoding="utf-8")
        self.assertNotIn(".init_forms(", source)
        self.assertNotIn("FORM_DoDocumentJSAction", source)
        self.assertIn("may_draw_forms=False, draw_annots=False", source)


if __name__ == "__main__":
    unittest.main()

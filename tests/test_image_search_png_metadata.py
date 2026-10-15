"""Narrow decode-only tolerance for invalid PNG eXIf checksums."""
from contextlib import closing
import hashlib
from io import BytesIO
import struct
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, ImageFile

from test_image_search import ImageSearchFixture, picture
from image_search import _features


def png_chunks(data):
    offset = 8
    while offset < len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + size
        yield data[offset + 4:offset + 8], data[offset + 8:end - 4], int.from_bytes(data[end - 4:end], "big")
        offset = end


def chunk(kind, payload, checksum=None):
    if checksum is None:
        checksum = zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def change_crc(data, target):
    changed = False
    output = bytearray(data[:8])
    for kind, payload, checksum in png_chunks(data):
        if kind == target and not changed:
            checksum ^= 1
            changed = True
        output.extend(chunk(kind, payload, checksum))
    assert changed
    return bytes(output)


def png_with_orientation(pixels, orientation):
    output = BytesIO()
    metadata = Image.Exif()
    metadata[274] = orientation
    with Image.open(BytesIO(pixels)) as image:
        image.save(output, "PNG", exif=metadata)
    return output.getvalue()


class PNGMetadataUnitTests(unittest.TestCase):
    def setUp(self):
        self.pixels = picture(71, size=(256, 384))
        self.valid = png_with_orientation(self.pixels, 6)
        self.bad = change_crc(self.valid, b"eXIf")

    def assert_same_visual_features(self, first, second):
        for name in ("dhash", "phash", "mean", "local_features", "local_sketch", "preview"):
            self.assertEqual(first[name], second[name], name)

    def test_valid_png_metadata_keeps_orientation_without_fallback_or_source_changes(self):
        output = BytesIO()
        with Image.open(BytesIO(self.pixels)) as image:
            image.transpose(Image.Transpose.ROTATE_270).save(output, "PNG")
        expected = _features(BytesIO(output.getvalue()), preview=True)
        source = BytesIO(self.valid)
        with patch("image_search._png_without_bad_exif", side_effect=AssertionError("Healthy image used fallback")):
            result = _features(source, preview=True)
        self.assert_same_visual_features(result, expected)
        self.assertEqual(source.getvalue(), self.valid)

    def test_only_bad_exif_crc_is_discarded_and_its_orientation_is_not_trusted(self):
        original = hashlib.sha256(self.bad).digest()
        policy = ImageFile.LOAD_TRUNCATED_IMAGES
        result = _features(BytesIO(self.bad), preview=True)
        expected = _features(BytesIO(self.pixels), preview=True)
        self.assert_same_visual_features(result, expected)
        self.assertEqual(hashlib.sha256(self.bad).digest(), original)
        self.assertEqual(ImageFile.LOAD_TRUNCATED_IMAGES, policy)

    def test_bad_critical_or_other_ancillary_crc_remains_rejected(self):
        # Each candidate also has the offending eXIf, so compatibility cannot
        # accidentally hide damage encountered after removing that metadata.
        ordinary_ancillary = (self.bad[:33] + chunk(b"pHYs", struct.pack(">IIB", 1000, 1000, 1)) + self.bad[33:])
        for name, candidate in (("IHDR", change_crc(self.bad, b"IHDR")),
                                ("IDAT", change_crc(self.bad, b"IDAT")),
                                ("IEND", change_crc(self.bad, b"IEND")),
                                ("pHYs", change_crc(ordinary_ancillary, b"pHYs"))):
            with self.subTest(chunk=name), self.assertRaises(ValueError):
                _features(BytesIO(candidate), preview=True)

    def test_duplicate_exif_is_rejected_even_when_one_checksum_is_valid(self):
        valid_exif = next(chunk(kind, payload, checksum) for kind, payload, checksum in png_chunks(self.valid) if kind == b"eXIf")
        bad_exif = next(chunk(kind, payload, checksum) for kind, payload, checksum in png_chunks(self.bad) if kind == b"eXIf")
        for duplicate in (valid_exif, bad_exif):
            with self.subTest(checksum=duplicate[-4:]), self.assertRaises(ValueError):
                _features(BytesIO(self.bad[:33] + duplicate + self.bad[33:]), preview=True)

    def test_truncated_or_oversized_chunk_boundaries_and_missing_iend_are_rejected(self):
        oversized = bytearray(self.bad)
        offset = self.bad.index(b"IDAT") - 4
        oversized[offset:offset + 4] = struct.pack(">I", 0x7fffffff)
        for name, candidate in (("short-crc", self.bad[:-1]), ("missing-iend", self.bad[:-12]),
                                ("oversized-chunk", bytes(oversized))):
            with self.subTest(case=name), self.assertRaises(ValueError):
                _features(BytesIO(candidate), preview=True)
        with patch("image_search.MAX_BYTES", len(self.bad) - 1), self.assertRaises(ValueError):
            _features(BytesIO(self.bad), preview=True)

    def test_unknown_critical_chunk_is_not_discarded(self):
        candidate = self.bad[:33] + chunk(b"ABCD", b"unknown") + self.bad[33:]
        with self.assertRaises(ValueError):
            _features(BytesIO(candidate), preview=True)


class PNGMetadataWorkerTests(ImageSearchFixture):
    def test_real_source_and_stdin_workers_share_policy_and_keep_original_digest(self):
        bad = change_crc(png_with_orientation(self.source, 6), b"eXIf")
        target = self.album / "1.png"
        target.write_bytes(bad)
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        self.app._scan_library(self.app._library_config(self.library_id))
        self.enable()  # Runs real isolated --source workers under their limits.
        self.assertEqual(self.manager.snapshot()["summary"], {"total": 2, "indexed": 2, "pending": 0, "failed": 0})
        row = next(row for row in self.rows() if row["page_key"].endswith("/1.png"))
        self.assertEqual(row["digest"], before)
        with closing(self.app.connect()) as connection:
            source_marker = connection.execute("SELECT source_version FROM tc_pages WHERE book_id=? AND page_key=?",
                                              (self.book_id, row["page_key"])).fetchone()[0]
        self.assertEqual(row["source_version"], source_marker)
        result = self.manager.query(bad)  # Runs a real isolated --stdin worker.
        self.assertIn({"book_id": self.book_id, "page": 0, "match": "exact", "distance": 0}, result["results"])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
        self.assertEqual(target.read_bytes(), bad)
        self.assertEqual(list(self.manager.root.glob(".index-*")), [])
        self.assertEqual(self.manager.processes, set())


if __name__ == "__main__":
    unittest.main()

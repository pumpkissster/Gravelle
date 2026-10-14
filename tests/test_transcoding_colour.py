"""Colour metadata and obsolete-derivative tests, using synthetic pictures."""
from contextlib import closing
import hashlib
from io import BytesIO
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

from test_transcoding import TranscodingFixture, image_bytes


def rgb_profile(wide=False):
    data = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    if wide:
        # A valid matrix/TRC RGB profile whose green primary differs from
        # sRGB. It needs no downloaded ICC file or private photograph.
        count = int.from_bytes(data[128:132], "big")
        for offset in range(132, 132 + count * 12, 12):
            if data[offset:offset + 4] == b"gXYZ":
                position = int.from_bytes(data[offset + 4:offset + 8], "big")
                xyz = (.2053, .6257, .0609)
                data[position + 8:position + 20] = b"".join(struct.pack(">i", round(value * 65536)) for value in xyz)
                break
        else:
            raise AssertionError("Synthetic RGB profile has no green matrix tag")
    return bytes(data)


class TranscodingColourTests(TranscodingFixture):
    def managed_fixture(self, profile, *, output_format="webp", exif=None):
        (self.album / "1.png").unlink()
        source = Image.new("RGB", (64, 96), (180, 90, 110))
        output = BytesIO()
        source.save(output, "JPEG", quality=100, subsampling=0, icc_profile=profile, exif=exif or b"")
        (self.album / "1.jpg").write_bytes(output.getvalue())
        self.scan()
        self.enable(output_format=output_format, quality=95)
        self.action("transcode")
        self.finish()
        return output.getvalue(), self.app.image(self.book_id, 0)[0]

    def test_srgb_profile_is_retained_for_jpeg_and_webp(self):
        for format_name in ("jpeg", "webp"):
            with self.subTest(format=format_name):
                if (self.album / "1.jpg").exists():
                    (self.album / "1.jpg").unlink()
                    (self.album / "1.png").write_bytes(self.sources[0])
                source, result = self.managed_fixture(rgb_profile(), output_format=format_name)
                with Image.open(BytesIO(result)) as image:
                    self.assertTrue(image.info.get("icc_profile"))
                    output_profile = ImageCms.ImageCmsProfile(BytesIO(image.info["icc_profile"]))
                    self.assertEqual(output_profile.profile.xcolor_space.strip(), "RGB")
                    self.assertLessEqual(max(abs(a - b) for a, b in zip(image.getpixel((10, 10)), (180, 90, 110))), 6)
                    self.assertFalse(image.getexif())

    def test_wide_rgb_is_converted_to_srgb_instead_of_dropping_profile(self):
        source, result = self.managed_fixture(rgb_profile(wide=True))
        with Image.open(BytesIO(source)) as original:
            profile = ImageCms.ImageCmsProfile(BytesIO(original.info["icc_profile"]))
            expected = ImageCms.profileToProfile(original, profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
            source_pixel = original.getpixel((10, 10))
            expected_pixel = expected.getpixel((10, 10))
        with Image.open(BytesIO(result)) as actual:
            actual_pixel = actual.getpixel((10, 10))
            self.assertTrue(actual.info.get("icc_profile"))
        self.assertGreater(max(abs(a - b) for a, b in zip(source_pixel, expected_pixel)), 3)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual_pixel, expected_pixel)), 7)

    def test_profile_and_exif_orientation_are_applied_together(self):
        exif = Image.Exif()
        exif[274] = 6
        source, result = self.managed_fixture(rgb_profile(wide=True), output_format="jpeg", exif=exif)
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (96, 64))
            self.assertTrue(image.info.get("icc_profile"))
            self.assertFalse(image.getexif())

    def test_untagged_cmyk_keeps_normal_rgb_conversion(self):
        (self.album / "1.png").unlink()
        output = BytesIO()
        Image.new("CMYK", (64, 96), (0, 100, 150, 20)).save(output, "JPEG", quality=100)
        (self.album / "1.jpg").write_bytes(output.getvalue())
        self.scan()
        self.enable(quality=95)
        self.action("transcode")
        self.finish()
        with Image.open(BytesIO(output.getvalue())) as original:
            expected = original.convert("RGB").getpixel((10, 10))
        with Image.open(BytesIO(self.app.image(self.book_id, 0)[0])) as actual:
            self.assertEqual(actual.mode, "RGB")
            self.assertLessEqual(max(abs(a - b) for a, b in zip(actual.getpixel((10, 10)), expected)), 7)

    def test_invalid_embedded_profile_fails_without_changing_source(self):
        (self.album / "1.png").write_bytes(image_bytes(icc_profile=b"invalid-icc-profile"))
        self.scan()
        before = self.source_hashes()
        self.enable()
        self.action("transcode")
        self.manager.run_once()
        self.assertEqual(self.record()["status"], "failed")
        self.assertIn("色彩配置", self.record()["error"])
        self.assertFalse(self.record()["published"])
        self.assertEqual(self.source_hashes(), before)


class TranscodingCleanupTests(TranscodingFixture):
    def test_restart_removes_abandoned_jobs_and_unregistered_outputs_only(self):
        self.enable()
        self.action("transcode")
        self.finish()
        self.save_rules({"quality": 50})
        self.action("transcode")
        self.manager.run_once()
        # Keep both the published generation and one replacement awaiting its
        # companion page, including every registered output byte-for-byte.
        registered = {row["file"]: (self.manager.root / row["file"]).read_bytes()
                      for row in self.rows("tc_outputs")}
        self.assertEqual(len(registered), 3)
        folder = self.manager._storage_directory(self.book_id)
        orphan = folder / ("b" * 64 + ".webp")
        orphan.write_bytes(image_bytes(format_name="WEBP"))
        job = folder / ".job-abc_1234"
        job.mkdir()
        (job / "input").write_bytes(self.sources[0])
        (job / "output.webp").write_bytes(image_bytes(format_name="WEBP"))
        unfamiliar = folder / ".job-custom12"
        unfamiliar.mkdir()
        (unfamiliar / "notes.txt").write_text("keep unfamiliar content", encoding="utf-8")
        note = folder / "notes.txt"
        note.write_text("keep", encoding="utf-8")
        before = self.source_hashes()
        self.restart()
        self.assertFalse(orphan.exists())
        self.assertFalse(job.exists())
        self.assertTrue((unfamiliar / "notes.txt").exists())
        self.assertEqual(note.read_text(encoding="utf-8"), "keep")
        for name, data in registered.items():
            self.assertEqual((self.manager.root / name).read_bytes(), data)
        self.assertEqual(self.manager.snapshot()["summary"]["stored_bytes"], sum(map(len, registered.values())))
        self.finish()
        self.assertEqual(self.source_hashes(), before)

    def test_abandoned_job_recovery_refuses_windows_reparse_directories(self):
        folder = self.manager._storage_directory(self.book_id)
        orphan = folder / ("c" * 64 + ".webp")
        orphan.write_bytes(b"do not follow a reparse directory")
        original_lstat = type(folder).lstat

        def reparse_lstat(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path == folder:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value

        with patch.object(type(folder), "lstat", reparse_lstat):
            self.manager._recover_abandoned_files()
        self.assertEqual(orphan.read_bytes(), b"do not follow a reparse directory")

    def _assert_invalidated_before_registration_is_cleaned(self, invalidate, expected_status):
        self.enable()
        self.action("transcode")
        source_hashes = self.source_hashes()
        unregistered = self.manager._storage_directory(self.book_id) / ("a" * 64 + ".webp")
        generated = image_bytes((32, 48), format_name="WEBP")
        checks = []
        installed = False
        current_job = self.manager._job_current

        def converted(job, rules):
            nonlocal installed
            # Reproduce the boundary after _convert has atomically installed an
            # output, but before the output/mapping transaction registers it.
            unregistered.write_bytes(generated)
            installed = True
            return {"file": self.book_id + "/" + unregistered.name, "size": len(generated),
                    "mime": "image/webp", "digest": hashlib.sha256(generated).hexdigest()}

        def invalidate_after_first_check(book_id, generation):
            valid = current_job(book_id, generation)
            # Shared-slot cancellation checks occur before conversion. Inject
            # this race only at its intended post-installation boundary.
            if not installed:
                return valid
            checks.append(valid)
            if len(checks) == 1:
                self.assertTrue(valid)
                invalidate()
            return valid

        with patch.object(self.manager, "_convert", side_effect=converted), \
                patch.object(self.manager, "_job_current", side_effect=invalidate_after_first_check):
            self.assertTrue(self.manager.run_once())

        self.assertEqual(checks, [True, False])
        self.assertEqual(self.record()["status"], expected_status)
        self.assertFalse(self.record()["published"])
        self.assertEqual(self.rows("tc_outputs"), [])
        self.assertEqual(self.manager.snapshot()["summary"]["stored_bytes"], 0)
        self.assertFalse(unregistered.exists(), "Invalidated output escaped SQLite/storage accounting")
        self.assertEqual(self.manager.cleanup({})["cleanup"]["removed_files"], 0)
        self.assertEqual(self.source_hashes(), source_hashes)

    def test_cancel_between_generation_checks_cleans_unregistered_output(self):
        self._assert_invalidated_before_registration_is_cleaned(lambda: self.action("cancel"), "cancelled")

    def test_rule_change_between_generation_checks_cleans_unregistered_output(self):
        self._assert_invalidated_before_registration_is_cleaned(lambda: self.save_rules({"quality": 60}), "pending")

    def test_cleanup_removes_only_unreferenced_old_generation(self):
        self.enable()
        self.action("transcode")
        self.finish()
        old_files = {row["file"] for row in self.rows("tc_outputs")}
        self.save_rules({"quality": 50})
        self.action("transcode")
        self.manager.run_once()
        # Both the active generation and a prepared page awaiting publication
        # must survive cleanup, even while no worker is currently running.
        partial = self.manager.cleanup({})
        self.assertEqual(partial["cleanup"]["removed_files"], 0)
        self.finish()
        active_files = {row["active_file"] for row in self.rows("tc_pages") if row["active_file"]}
        self.assertTrue(old_files.isdisjoint(active_files))
        before = self.source_hashes()
        current = self.app.image(self.book_id, 0)
        state = self.manager.snapshot()
        self.assertEqual(state["summary"]["reclaimable_files"], 2)
        cleared = self.manager.cleanup({"ids": [self.book_id]})
        self.assertEqual(cleared["cleanup"]["removed_files"], 2)
        self.assertGreater(cleared["cleanup"]["removed_bytes"], 0)
        self.assertEqual(cleared["summary"]["reclaimable_files"], 0)
        for name in active_files:
            self.assertTrue((self.manager.root / name).exists())
        for name in old_files:
            self.assertFalse((self.manager.root / name).exists())
        self.assertEqual(self.app.image(self.book_id, 0), current)
        self.assertEqual(self.source_hashes(), before)

    def test_cleanup_refuses_path_injection_child_and_active_worker(self):
        self.error(400, self.manager.cleanup, {"path": str(self.media)})
        child = self.app.save_user({"username": "cleanup-child", "password": "temporary-cleanup-password",
                                   "library_ids": [self.library_id]})["user"]
        with self.app.acting_as(child["id"]):
            self.error(403, self.manager.cleanup, {})
        self.manager.run_lock.acquire()
        try:
            self.error(409, self.manager.cleanup, {})
        finally:
            self.manager.run_lock.release()


if __name__ == "__main__":
    unittest.main()

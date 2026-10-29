"""Crop geometry and the bounded, catalogue-cover-only face worker protocol."""
import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import avatar_face_crop as crop


def jpeg(color="white", size=(400, 600)):
    output = BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, "JPEG")
    return output.getvalue()


def payload(covers):
    return json.dumps({"covers": [base64.b64encode(value).decode("ascii") for value in covers]}).encode()


def face(x=100, y=70, width=80, height=100, score=.95):
    return [x, y, width, height, *([0] * 10), score]


class CropGeometryTests(unittest.TestCase):
    def test_off_center_head_keeps_hair_face_and_shoulders(self):
        box = crop._head_crop_box(400, 600, (260., 70., 80., 100.))
        left, top, right, bottom = box
        self.assertEqual(right - left, bottom - top)
        self.assertLess(left, 260)
        self.assertLessEqual(top, 25)
        self.assertGreaterEqual(right, 340)
        self.assertGreaterEqual(bottom, 210)
        self.assertGreater(left, 0, "Face on the right must not keep the centered crop")

    def test_each_edge_is_clamped_without_black_borders_or_cutting_face(self):
        for bounds in ((0., 0., 75., 100.), (325., 0., 75., 100.),
                       (0., 500., 75., 100.), (325., 500., 75., 100.)):
            with self.subTest(face=bounds):
                left, top, right, bottom = crop._head_crop_box(400, 600, bounds)
                self.assertEqual(right - left, bottom - top)
                self.assertTrue(0 <= left < right <= 400)
                self.assertTrue(0 <= top < bottom <= 600)
                x, y, w, h = bounds
                self.assertTrue(left <= x < x + w <= right)
                self.assertTrue(top <= y < y + h <= bottom)

    def test_invalid_coordinates_are_never_used_for_cropping(self):
        for bounds in ((-1., 1., 20., 20.), (1., 1., float("nan"), 20.),
                       (1., float("inf"), 20., 20.), (395., 1., 20., 20.), (1., 1., 0., 20.)):
            with self.subTest(face=bounds), self.assertRaises(ValueError):
                crop._head_crop_box(400, 600, bounds)

    def test_similarly_sized_group_faces_are_ambiguous_but_small_background_face_is_not(self):
        self.assertIsNone(crop._select_face([face(), face(240, 80, 70, 90)], 400, 600))
        self.assertEqual(crop._select_face([face(), face(10, 20, 22, 25)], 400, 600), (100., 70., 80., 100.))

    def test_low_confidence_tiny_and_nonfinite_faces_are_discarded(self):
        invalid = [face(score=.6), face(width=10), face(x=float("nan")), face(height=float("inf")),
                   face(x=-200), face(width=-10), face(score=2), [1, 2, 3]]
        self.assertIsNone(crop._select_face(invalid, 400, 600))

    def test_only_tiny_edge_overflow_is_clamped(self):
        selected = crop._select_face([face(x=-2, y=2)], 400, 600)
        self.assertEqual(selected, (0., 2., 78., 100.))
        self.assertIsNone(crop._select_face([face(x=-40)], 400, 600))


class CropProtocolTests(unittest.TestCase):
    def fake_detector(self, rows):
        class Detector:
            def __init__(self):
                self.rows = iter(rows)
            def setInputSize(self, size):
                pass
            def detect(self, image):
                return None, next(self.rows)
        class CV:
            COLOR_RGB2BGR = 1
            error = RuntimeError
            @staticmethod
            def cvtColor(image, kind):
                return image
        class NP:
            @staticmethod
            def asarray(image):
                return image
        return CV, NP, Detector()

    def assert_avatar(self, result, index, found):
        self.assertEqual(set(result), {"ok", "index", "face_found", "avatar"})
        self.assertEqual((result["ok"], result["index"], result["face_found"]), (True, index, found))
        avatar = base64.b64decode(result["avatar"], validate=True)
        self.assertLessEqual(len(avatar), crop.MAX_AVATAR_BYTES)
        with Image.open(BytesIO(avatar)) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (256, 256)))
            image.load()

    def test_try_other_covers_after_no_face_and_group_face(self):
        with patch.object(crop, "_detector", return_value=self.fake_detector([None, [face(), face(240)], [face()]])):
            result = crop.crop_payload(payload([jpeg("red"), jpeg("green"), jpeg("blue")]))
        self.assert_avatar(result, 2, True)

    def test_fallback_is_first_decodable_cover_after_trying_all(self):
        with patch.object(crop, "_detector", return_value=self.fake_detector([None, None])):
            result = crop.crop_payload(payload([b"broken", jpeg("red"), jpeg("blue")]))
        self.assert_avatar(result, 1, False)

    def test_no_valid_cover_is_a_clear_error(self):
        with patch.object(crop, "_detector", return_value=self.fake_detector([])), self.assertRaisesRegex(ValueError, "封面"):
            crop.crop_payload(payload([b"broken", jpeg(size=(401, 600))]))

    def test_framing_limits_and_no_filesystem_arguments(self):
        invalid = [b"", b"[]", b"{", b'{"covers":[]}', b'{"covers":[1]}',
                   b'{"covers":["%%%"]}', b'{"covers":["YQ=="],"model":"/tmp/other.onnx"}',
                   payload([b"x"] * 9), payload([b"x" * (crop.MAX_COVER_BYTES + 1)])]
        for value in invalid:
            with self.subTest(size=len(value)), patch.object(crop, "_detector") as detector, self.assertRaises(ValueError):
                crop.crop_payload(value)
            detector.assert_not_called()

    def test_png_and_oversized_dimensions_are_rejected(self):
        stream = BytesIO()
        Image.new("RGB", (8, 8)).save(stream, "PNG")
        for value in (stream.getvalue(), jpeg(size=(401, 600)), jpeg(size=(400, 601))):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                crop._decode_cover(value)

    def test_bundled_model_and_provenance_are_pinned_and_tamper_fails(self):
        source = crop._verified_model()
        root = ROOT / "work" / "face-avatar-tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            target = Path(directory) / "models" / "yunet"
            target.mkdir(parents=True)
            (target / crop.MODEL_NAME).write_bytes(source)
            metadata = {"sha256": crop.MODEL_SHA256}
            (target / "provenance.json").write_text(json.dumps(metadata), encoding="utf-8")
            with patch.object(crop, "__file__", str(Path(directory) / "avatar_face_crop.py")):
                self.assertEqual(crop._verified_model(), source)
                metadata["sha256"] = "0" * 64
                (target / "provenance.json").write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "校验失败"):
                    crop._verified_model()
                metadata["sha256"] = crop.MODEL_SHA256
                (target / "provenance.json").write_text(json.dumps(metadata), encoding="utf-8")
                raw = bytearray((target / crop.MODEL_NAME).read_bytes())
                raw[0] ^= 1
                (target / crop.MODEL_NAME).write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "校验失败"):
                    crop._verified_model()

    def test_native_loader_uses_verified_bytes_even_if_unicode_path_changes(self):
        """Do not reopen the ONNX filename after authenticating its contents."""
        source = crop._verified_model()
        root = ROOT / "work" / "face-avatar-tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="头像模型-", dir=root) as directory:
            target = Path(directory) / "models" / "yunet"
            target.mkdir(parents=True)
            model = target / crop.MODEL_NAME
            model.write_bytes(source)
            (target / "provenance.json").write_text(json.dumps({"sha256": crop.MODEL_SHA256}), encoding="utf-8")
            verify = crop._verified_model
            def verify_then_change_path():
                data = verify()
                model.write_bytes(b"changed after verification")
                return data
            with patch.object(crop, "__file__", str(Path(directory) / "avatar_face_crop.py")), \
                    patch.object(crop, "_verified_model", side_effect=verify_then_change_path) as verified:
                cv2, np, detector = crop._detector()
                detector.setInputSize((64, 96))
                _, faces = detector.detect(np.zeros((96, 64, 3), dtype=np.uint8))
                self.assertIsNone(faces)
                verified.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

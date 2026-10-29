"""Bounded CPU face-aware avatar cropping, loaded by the private worker only.

The HTTP process may import the limits without importing OpenCV or NumPy.
Sources are already small, catalogue-owned JPEG covers; this module never
accepts a filesystem path from a caller or changes an original image.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path

from PIL import Image

MAX_COVERS = 8
MAX_COVER_BYTES = 512 * 1024
MAX_AVATAR_BYTES = 128 * 1024
MAX_INPUT_BYTES = 6 * 1024 * 1024
AVATAR_SIDE = 256
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
MODEL_BYTES = 232589
MODEL_NAME = "face_detection_yunet_2023mar.onnx"


def _verified_model():
    """Read and authenticate the exact model bytes passed to the native loader."""
    directory = Path(__file__).resolve().parent / "models" / "yunet"
    model, provenance = directory / MODEL_NAME, directory / "provenance.json"
    try:
        if (model.is_symlink() or provenance.is_symlink() or model.stat().st_size != MODEL_BYTES
                or not 0 < provenance.stat().st_size <= 16 * 1024):
            raise ValueError
        metadata = json.loads(provenance.read_text(encoding="utf-8"))
        with model.open("rb") as source:
            data = source.read(MODEL_BYTES + 1)
        if (not isinstance(metadata, dict) or metadata.get("sha256") != MODEL_SHA256
                or len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise ValueError("头像识别模型缺失或校验失败，请检查安装文件。") from None
    return data


def _detector():
    # Verify the exact bundled model before importing native decoder code.
    model = _verified_model()
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    try:
        # Loading the verified bytes also supports non-ASCII install paths on
        # Windows, and avoids reopening a path after its checksum was checked.
        detector = cv2.FaceDetectorYN.create("onnx", np.frombuffer(model, dtype=np.uint8),
                                            np.empty(0, dtype=np.uint8), (320, 320), .82, .3, 100,
                                            cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
    except (AttributeError, cv2.error):
        raise ValueError("头像识别模型无法加载，请检查安装文件。") from None
    return cv2, np, detector


def _select_face(rows, width, height):
    """Prefer one clear face; decline similarly sized people in group covers."""
    if rows is None:
        return None
    candidates = []
    for row in rows:
        try:
            if len(row) != 15:
                continue
            values = [float(value) for value in row]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in values):
            continue
        x, y, w, h = values[:4]
        confidence = values[-1]
        if (not .82 <= confidence <= 1 or min(w, h) < 22 or not .45 <= w / h <= 1.6
                or w > width or h > height):
            continue
        left, top = max(0., x), max(0., y)
        right, bottom = min(float(width), x + w), min(float(height), y + h)
        area = max(0., right - left) * max(0., bottom - top)
        if area < w * h * .85:
            continue
        candidates.append((area, confidence, (left, top, right - left, bottom - top)))
    candidates.sort(key=lambda item: item[:2], reverse=True)
    if not candidates or (len(candidates) > 1 and candidates[1][0] >= candidates[0][0] * .45):
        return None
    return candidates[0][2]


def _head_crop_box(width, height, face):
    """Square containing hair, face and some shoulders, shifted inside edges."""
    x, y, w, h = face
    if (type(width) is not int or type(height) is not int or min(width, height) < 1
            or any(not math.isfinite(float(value)) for value in face)
            or min(w, h) <= 0 or x < 0 or y < 0 or x + w > width or y + h > height):
        raise ValueError("头像裁切位置无效。")
    side = min(min(width, height), max(1, math.ceil(max(w * 2.15, h * 2.45))))
    center_x, center_y = x + w / 2, y + h * .725
    left = min(width - side, max(0, round(center_x - side / 2)))
    top = min(height - side, max(0, round(center_y - side / 2)))
    return left, top, left + side, top + side


def _jpeg_avatar(image, face=None):
    if face is None:
        side = min(image.size)
        left, top = (image.width - side) // 2, (image.height - side) // 2
        box = left, top, left + side, top + side
    else:
        box = _head_crop_box(image.width, image.height, face)
    with image.crop(box) as crop:
        with crop.resize((AVATAR_SIDE, AVATAR_SIDE), Image.Resampling.LANCZOS) as avatar:
            output = BytesIO()
            avatar.save(output, "JPEG", quality=88, optimize=True)
    data = output.getvalue()
    if not 0 < len(data) <= MAX_AVATAR_BYTES:
        raise ValueError("生成的头像超过安全上限。")
    return data


def _decode_cover(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_COVER_BYTES:
        raise ValueError("头像封面输入无效。")
    with Image.open(BytesIO(data)) as image:
        if (image.format != "JPEG" or not 0 < image.width <= 400 or not 0 < image.height <= 600
                or getattr(image, "is_animated", False)):
            raise ValueError("头像封面必须是小尺寸 JPG 图片。")
        image.load()
        return image.convert("RGB")


def crop_payload(data):
    """Private strict JSON protocol. Return only a small JPEG and source index."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_INPUT_BYTES:
        raise ValueError("头像任务超过安全上限。")
    try:
        request = json.loads(data)
        if (not isinstance(request, dict) or set(request) != {"covers"}
                or not isinstance(request["covers"], list) or not 1 <= len(request["covers"]) <= MAX_COVERS):
            raise ValueError
        covers = []
        for value in request["covers"]:
            if not isinstance(value, str) or not 0 < len(value) <= (MAX_COVER_BYTES + 2) // 3 * 4:
                raise ValueError
            raw = base64.b64decode(value, validate=True)
            if not 0 < len(raw) <= MAX_COVER_BYTES:
                raise ValueError
            covers.append(raw)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("头像任务输入无效。") from None
    cv2, np, detector = _detector()
    fallback = None
    for index, raw in enumerate(covers):
        try:
            image = _decode_cover(raw)
        except (OSError, ValueError):
            continue  # Try other catalogue-owned covers before giving up.
        with image:
            if fallback is None:
                fallback = index, _jpeg_avatar(image)
            frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
            try:
                detector.setInputSize((image.width, image.height))
                _, faces = detector.detect(frame)
            except cv2.error:
                continue
            face = _select_face(faces, image.width, image.height)
            if face is not None:
                return {"ok": True, "avatar": base64.b64encode(_jpeg_avatar(image, face)).decode("ascii"),
                        "index": index, "face_found": True}
    if fallback is None:
        raise ValueError("这些作品封面暂时无法生成头像，请换一组重试。")
    index, avatar = fallback
    return {"ok": True, "avatar": base64.b64encode(avatar).decode("ascii"), "index": index, "face_found": False}

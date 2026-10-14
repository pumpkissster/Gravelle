"""Bounded, CPU-only local features for screenshots and moderate crops.

The binary page record contains at most 512 ORB descriptors and normalised
coordinates (18 KiB). A 4 KiB bit sketch provides crop-aware candidate ranking;
it is deliberately a shortlist hint, never proof of a match. Geometry is checked
in the isolated image worker. Importing this module does not import native image
libraries, so the worker can install its memory limit first.
"""
from __future__ import annotations

import math
import struct

FEATURE_SIDE = 1280
MAX_FEATURES = 512
MIN_FEATURES = 12
_HEADER = struct.Struct("<4sHHH")
_MAGIC = b"GLF1"
_RECORD_BYTES = 36
MAX_BLOB_BYTES = _HEADER.size + MAX_FEATURES * _RECORD_BYTES
_TABLES = 8
_BITS = 12
_BUCKETS = 1 << _BITS
_TABLE_BYTES = _BUCKETS // 8
SKETCH_BYTES = _TABLES * _TABLE_BYTES
# Fixed projections are part of GLF1, independent of Python's sampling changes.
_PROJECTIONS = (
    (150, 9, 83, 101, 51, 93, 143, 38, 233, 234, 72, 238),
    (119, 31, 208, 188, 114, 130, 47, 143, 202, 225, 166, 6),
    (153, 11, 141, 125, 202, 207, 56, 180, 4, 62, 150, 3),
    (105, 54, 211, 86, 35, 135, 84, 157, 76, 29, 137, 3),
    (29, 93, 125, 45, 53, 21, 136, 248, 38, 129, 104, 161),
    (221, 229, 1, 218, 205, 180, 129, 199, 191, 187, 93, 203),
    (210, 117, 204, 189, 203, 224, 113, 15, 160, 159, 212, 145),
    (163, 211, 222, 187, 219, 120, 109, 22, 10, 123, 61, 179),
)


def _header(blob):
    if not isinstance(blob, bytes) or not _HEADER.size <= len(blob) <= MAX_BLOB_BYTES:
        return None
    magic, width, height, count = _HEADER.unpack_from(blob)
    if magic != _MAGIC or not 1 <= width <= FEATURE_SIDE or not 1 <= height <= FEATURE_SIDE:
        return None
    if count > MAX_FEATURES or len(blob) != _HEADER.size + count * _RECORD_BYTES:
        return None
    if count and min(width, height) < 32:
        return None
    return width, height, count


def validate(blob) -> bool:
    """Validate framing without loading OpenCV or NumPy into the server."""
    return _header(blob) is not None


def validate_sketch(value) -> bool:
    return isinstance(value, bytes) and len(value) == SKETCH_BYTES


def _native():
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    return cv2, np


def extract(pil_rgb) -> bytes:
    """Extract from an already EXIF-oriented, opaque RGB Pillow image."""
    cv2, np = _native()
    image = pil_rgb.copy()
    if max(image.size) > FEATURE_SIDE:
        from PIL import Image
        image.thumbnail((FEATURE_SIDE, FEATURE_SIDE), Image.Resampling.LANCZOS, reducing_gap=3)
    width, height = image.size
    if not width or not height:
        raise ValueError("图片尺寸无效。")
    if width < 32 or height < 32:
        return _HEADER.pack(_MAGIC, width, height, 0)
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(nfeatures=1200, scaleFactor=1.2, nlevels=8,
                         edgeThreshold=19, patchSize=31, fastThreshold=10,
                         scoreType=cv2.ORB_HARRIS_SCORE)
    points, descriptors = orb.detectAndCompute(gray, None)
    if descriptors is None:
        return _HEADER.pack(_MAGIC, width, height, 0)
    # Spatial quotas stop a dense title or patterned border consuming every
    # feature. Remaining places are filled by response, preserving weak regions.
    order = sorted(range(len(points)), key=lambda i: (-points[i].response, i))
    cells = [0] * 16
    selected, deferred = [], []
    for index in order:
        x, y = points[index].pt
        cell = min(3, int(y * 4 / height)) * 4 + min(3, int(x * 4 / width))
        if cells[cell] < MAX_FEATURES // 16:
            selected.append(index)
            cells[cell] += 1
        else:
            deferred.append(index)
    selected = (selected + deferred)[:MAX_FEATURES]
    output = bytearray(_HEADER.pack(_MAGIC, width, height, len(selected)))
    for index in selected:
        x, y = points[index].pt
        output.extend(struct.pack("<HH", round(x * 65535 / max(1, width - 1)),
                                  round(y * 65535 / max(1, height - 1))))
        output.extend(descriptors[index].tobytes())
    return bytes(output)


def tokens(blob) -> list[int]:
    """Small projected descriptor words; meant to build the compact sketch."""
    header = _header(blob)
    if header is None:
        raise ValueError("图片局部特征无效。")
    words = set()
    for offset in range(_HEADER.size, len(blob), _RECORD_BYTES):
        descriptor = blob[offset + 4:offset + _RECORD_BYTES]
        for table, bits in enumerate(_PROJECTIONS):
            signature = 0
            for index, bit in enumerate(bits):
                signature |= ((descriptor[bit >> 3] >> (bit & 7)) & 1) << index
            words.add(table * _BUCKETS + signature)
    return sorted(words)


def sketch(blob) -> bytes:
    value = bytearray(SKETCH_BYTES)
    for token in tokens(blob):
        value[token >> 3] |= 1 << (token & 7)
    return bytes(value)


def rank(query_sketch, source_sketch) -> float:
    """Rank crop candidates by excess signature collisions, without native code.

    A large score means more local descriptor overlap than chance predicts. The
    population correction helps compare pages with very different feature counts.
    Correlated image textures still create false candidates; match() verifies them.
    """
    if not validate_sketch(query_sketch) or not validate_sketch(source_sketch):
        raise ValueError("图片候选特征无效。")
    overlap = expected = variance = 0.0
    for offset in range(0, SKETCH_BYTES, _TABLE_BYTES):
        query = int.from_bytes(query_sketch[offset:offset + _TABLE_BYTES], "little")
        source = int.from_bytes(source_sketch[offset:offset + _TABLE_BYTES], "little")
        query_count, source_count = query.bit_count(), source.bit_count()
        overlap += (query & source).bit_count()
        probability = source_count / _BUCKETS
        expected += query_count * probability
        variance += query_count * probability * (1 - probability) * (
            (_BUCKETS - query_count) / (_BUCKETS - 1))
    return (overlap - expected) / math.sqrt(max(1.0, variance))


def _arrays(blob, np):
    header = _header(blob)
    if header is None:
        raise ValueError("图片局部特征无效。")
    width, height, count = header
    records = np.frombuffer(blob, dtype=np.uint8, offset=_HEADER.size).reshape(count, _RECORD_BYTES)
    coordinates = records[:, :4].copy().view("<u2").reshape(count, 2).astype(np.float32)
    coordinates[:, 0] *= max(1, width - 1) / 65535
    coordinates[:, 1] *= max(1, height - 1) / 65535
    return width, height, coordinates, records[:, 4:].copy()


def match(query_blob, source_blob) -> float | None:
    """Return a 0..13 geometric distance, or None when evidence is insufficient.

    This recognises the same flat image under scaling, compression, surrounding
    screenshot chrome and moderate cropping. Tiny patches and untextured images
    intentionally fail closed. It does not recognise people or semantic similarity.
    """
    cv2, np = _native()
    qw, qh, qp, qd = _arrays(query_blob, np)
    sw, sh, sp, sd = _arrays(source_blob, np)
    if min(len(qd), len(sd)) < MIN_FEATURES:
        return None
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(qd, sd, k=2)
    reverse = matcher.match(sd, qd)
    reverse_best = {item.queryIdx: item.trainIdx for item in reverse}
    good = [pair[0] for pair in pairs if len(pair) == 2
            and pair[0].distance <= 64 and pair[0].distance < .78 * pair[1].distance
            and reverse_best.get(pair[0].trainIdx) == pair[0].queryIdx]
    if len(good) < MIN_FEATURES:
        return None
    query_points = np.float32([qp[item.queryIdx] for item in good])
    source_points = np.float32([sp[item.trainIdx] for item in good])
    threshold = max(2.0, max(sw, sh) * .004)
    cv2.setRNGSeed(44729)
    transform, mask = cv2.findHomography(query_points, source_points, cv2.RANSAC,
                                        threshold, maxIters=1500, confidence=.995)
    if transform is None or mask is None or not np.isfinite(transform).all():
        return None
    keep = mask.ravel().astype(bool)
    count = int(keep.sum())
    if count < MIN_FEATURES or count / len(good) < .55:
        return None
    query_inliers, source_inliers = query_points[keep], source_points[keep]
    query_area, source_area = float(qw * qh), float(sw * sh)
    query_coverage = cv2.contourArea(cv2.convexHull(query_inliers)) / query_area
    source_coverage = cv2.contourArea(cv2.convexHull(source_inliers)) / source_area
    if query_coverage < .02 or source_coverage < .01:
        return None
    if min(query_coverage, source_coverage) < .04 and count < 25:
        return None
    # Require many independently positioned points, rather than several pyramid
    # descriptors from one corner of a repeated pattern.
    for points in (query_inliers, source_inliers):
        left, top = points.min(axis=0)
        width, height = np.maximum(1, points.max(axis=0) - (left, top))
        cells = {(min(7, int((x - left) * 8 / width)),
                  min(7, int((y - top) * 8 / height))) for x, y in points}
        if len(cells) < 6:
            return None
    corners = np.float32([[0, 0], [qw, 0], [qw, qh], [0, qh]]).reshape(-1, 1, 2)
    projected = cv2.perspectiveTransform(corners, transform).reshape(4, 2)
    if not np.isfinite(projected).all() or not cv2.isContourConvex(projected):
        return None
    if cv2.contourArea(projected, oriented=True) <= 0:
        return None
    edges = np.roll(projected, -1, axis=0) - projected
    lengths = np.linalg.norm(edges, axis=1)
    if lengths.min() <= 1 or lengths.max() > max(sw, sh) * 8:
        return None
    for index in range(4):
        cosine = float(np.dot(edges[index], edges[(index + 1) % 4]) /
                       (lengths[index] * lengths[(index + 1) % 4]))
        if abs(cosine) > .55:
            return None
    source_rectangle = np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]])
    overlap, _ = cv2.intersectConvexConvex(projected, source_rectangle)
    projected_area = cv2.contourArea(projected)
    if overlap / source_area < .30 or overlap / max(1.0, projected_area) < .20:
        return None
    projected_inliers = cv2.perspectiveTransform(query_inliers.reshape(-1, 1, 2), transform).reshape(-1, 2)
    error = float(np.median(np.linalg.norm(projected_inliers - source_inliers, axis=1))) / threshold
    confidence = min(1.0, count / 45) * .55 + (count / len(good)) * .30 + (1 - min(1, error)) * .15
    return round(max(0.25, 13 * (1 - confidence)), 4)

"""Exercise native image workers with generated, memory-only illustrations.

Run from the application directory, including inside the read-only release image:
    docker exec -i CONTAINER python - < packaging/smoke_image_workers.py
    python packaging/smoke_image_workers.py

No HTTP session, catalogue, source photograph or data directory is used. Decode
and geometric matching run through the real resource-limited worker entry point,
not by calling the image algorithm directly in this smoke-test process.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont


APP = Path.cwd()
WORKER = APP / "image_search.py"
THREAD_VARIABLES = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **{name: "1" for name in THREAD_VARIABLES})
MAX_WORKER_OUTPUT = 384 * 1024


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def child_json(command, data=None, *, label):
    result = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=65, env=ENV, cwd=APP)
    require(result.returncode == 0, f"{label}: child exited with code {result.returncode}")
    require(0 < len(result.stdout) <= MAX_WORKER_OUTPUT, f"{label}: invalid output size")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise RuntimeError(f"{label}: child did not return one JSON document") from None
    require(isinstance(value, dict), f"{label}: invalid response")
    return value


def native_probe():
    # Check native loading under the same memory budget, after establishing the
    # thread environment and before importing NumPy/OpenCV, as in worker_main.
    code = """
import json, os
from transcoding import _restrict_worker
limit_handle = _restrict_worker(1024 * 1024 * 1024)
from image_search_features import _native
cv2, np = _native()
assert cv2.getNumThreads() == 1
assert not cv2.ocl.useOpenCL()
names = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')
assert all(os.environ.get(name) == '1' for name in names)
sample = np.arange(12, dtype=np.float32).reshape(3, 4)
assert (sample @ sample.T).shape == (3, 3)
print(json.dumps({'opencv': cv2.__version__, 'numpy': np.__version__,
                  'opencv_threads': cv2.getNumThreads(), 'opencl': cv2.ocl.useOpenCL(),
                  'native_thread_environment': 1}))
"""
    return child_json([sys.executable, "-B", "-c", code], label="native runtime")


def illustration(seed):
    randomizer = random.Random(seed)
    width, height = 960, 1280
    image = Image.new("RGB", (width, height), (238, 229, 220))
    draw = ImageDraw.Draw(image)
    draw.polygon(((0, 0), (width * .56, 0), (width * .2, height), (0, height)),
                 fill=(45, 65, 91))
    draw.ellipse((width * .36, height * .08, width * 1.2, height * .83),
                 fill=(166, 83, 111))
    font = ImageFont.load_default(size=15)
    for row in range(16):
        for column in range(12):
            x = column * width // 12 + randomizer.randrange(5, 21)
            y = row * height // 16 + randomizer.randrange(5, 21)
            w, h = randomizer.randrange(32, 55), randomizer.randrange(30, 57)
            color = tuple(randomizer.randrange(20, 235) for _ in range(3))
            variant = randomizer.randrange(3)
            if variant == 0:
                draw.rectangle((x, y, x + w, y + h), fill=color, outline="white", width=2)
            elif variant == 1:
                draw.ellipse((x, y, x + w, y + h), fill=color, outline="black", width=3)
            else:
                draw.polygon(((x, y + h), (x + w // 3, y), (x + w, y + h // 3)),
                             fill=color, outline="white", width=2)
            draw.line((x + 4, y + h // 3, x + w - 4, y + h - 4),
                      fill=(250, 247, 235), width=randomizer.randrange(2, 5))
            draw.text((x, y + 5), f"{randomizer.randrange(1000):03}", font=font,
                      fill=(10, 10, 15), stroke_width=1, stroke_fill=(245, 245, 240))
            for _ in range(3):
                dx, dy = x + randomizer.randrange(w), y + randomizer.randrange(h)
                draw.line((dx, dy, dx + randomizer.randrange(5, 17),
                           dy + randomizer.randrange(4, 14)), fill="black", width=2)
    draw.text((32, height - 44), f"Generated fixture {seed}", font=font,
              fill="white", stroke_width=2, stroke_fill="black")
    return image


def encode(image, format_name="PNG", quality=65):
    output = BytesIO()
    image.save(output, format_name, quality=quality)
    return output.getvalue()


def queries(image):
    content = image.copy()
    content.thumbnail((660, 880), Image.Resampling.LANCZOS)
    screenshot = Image.new("RGB", (1000, 1120), (28, 27, 31))
    draw = ImageDraw.Draw(screenshot)
    draw.rectangle((0, 0, 1000, 42), fill=(244, 240, 243))
    draw.rounded_rectangle((105, 7, 820, 35), radius=10, fill=(218, 215, 219))
    draw.text((125, 14), "localhost / generated screenshot", fill=(60, 60, 65))
    draw.rectangle((0, 42, 1000, 112), fill=(49, 45, 49))
    draw.text((30, 70), "Fixture reader", fill=(240, 230, 237))
    screenshot.paste(content, ((1000 - content.width) // 2, 136))
    draw.rounded_rectangle((72, 1054, 928, 1097), radius=14, fill=(72, 65, 72))
    draw.line((118, 1075, 875, 1075), fill=(234, 80, 152), width=5)
    factor = math.sqrt(.45)
    width, height = round(image.width * factor), round(image.height * factor)
    left, top = round((image.width - width) * .22), round((image.height - height) * .72)
    crop = image.crop((left, top, left + width, top + height))
    crop.thumbnail((900, 900), Image.Resampling.LANCZOS)
    whole = image.copy()
    whole.thumbnail((720, 960), Image.Resampling.LANCZOS)
    return {
        "whole_resized_jpeg": encode(whole, "JPEG", 61),
        "full_screenshot": encode(screenshot, "JPEG", 68),
        "crop_45_percent": encode(crop, "JPEG", 57),
    }


def decode(data, features_module, *, label, preview=False):
    command = [sys.executable, "-B", str(WORKER), "--worker", "--stdin"]
    if preview:
        command.append("--preview")
    value = child_json(command, data, label=label)
    allowed = {"ok", "digest", "dhash", "phash", "mean", "width", "height", "local_features", "local_sketch"}
    if preview:
        allowed.add("preview")
    require(set(value) == allowed and value["ok"] is True, f"{label}: invalid feature response")
    require(value["digest"] == hashlib.sha256(data).hexdigest(), f"{label}: digest mismatch")
    require(all(isinstance(value[name], str) and re.fullmatch(r"[0-9a-f]{16}", value[name])
                for name in ("dhash", "phash")), f"{label}: invalid whole-image fingerprints")
    blob = base64.b64decode(value["local_features"], validate=True)
    sketch = base64.b64decode(value["local_sketch"], validate=True)
    require(features_module.validate(blob) and features_module.validate_sketch(sketch),
            f"{label}: invalid local feature framing")
    require(features_module.sketch(blob) == sketch, f"{label}: candidate sketch mismatch")
    require(len(blob) > 10 + 64 * 36, f"{label}: too few generated features")
    if preview:
        jpeg = base64.b64decode(value["preview"], validate=True)
        require(len(jpeg) <= 192 * 1024, f"{label}: preview exceeded size limit")
        with Image.open(BytesIO(jpeg)) as image:
            require(image.format == "JPEG" and max(image.size) <= 384,
                    f"{label}: invalid small preview")
            image.load()
    return value


def match(query, candidates, *, label):
    payload = json.dumps({"query": query["local_features"],
                          "candidates": [item["local_features"] for item in candidates]},
                         separators=(",", ":")).encode("ascii")
    require(len(payload) <= 4 * 1024 * 1024, f"{label}: match payload exceeded size limit")
    value = child_json([sys.executable, "-B", str(WORKER), "--worker", "--stdin", "--match"],
                       payload, label=label)
    require(set(value) == {"ok", "matches"} and value["ok"] is True
            and isinstance(value["matches"], list), f"{label}: invalid match response")
    require(all(isinstance(item, list) and len(item) == 2 and type(item[0]) is int
                and 0 <= item[0] < len(candidates) and type(item[1]) in (int, float)
                and math.isfinite(item[1]) and 0 <= item[1] <= 13 for item in value["matches"]),
            f"{label}: invalid candidate index or distance")
    return value["matches"]


def main():
    started = time.monotonic()
    try:
        require(WORKER.is_file(), "Run this smoke test from the application directory")
        # This module is deliberately native-library-free until its worker calls
        # _native(). Importing framing/ranking helpers here does not use OpenCV.
        sys.path.insert(0, str(APP))
        import image_search_features as features_module

        runtime = native_probe()
        source_image = illustration(701)
        source_bytes = encode(source_image)
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        source = decode(source_bytes, features_module, label="source decode", preview=True)
        unrelated = decode(encode(illustration(903)), features_module, label="unrelated decode")
        distances = {}
        for name, data in queries(source_image).items():
            query = decode(data, features_module, label=name + " decode")
            matches = match(query, [unrelated, source], label=name + " geometry")
            require(len(matches) == 1 and matches[0][0] == 1,
                    name + ": query must identify only its generated source")
            query_sketch = base64.b64decode(query["local_sketch"], validate=True)
            ranks = [features_module.rank(query_sketch, base64.b64decode(item["local_sketch"], validate=True))
                     for item in (unrelated, source)]
            require(ranks[1] > ranks[0], name + ": candidate ranking preferred the unrelated image")
            distances[name] = matches[0][1]
        require(match(unrelated, [source], label="unrelated negative") == [],
                "Unrelated generated image must not match the source")
        require(hashlib.sha256(source_bytes).hexdigest() == source_digest,
                "Source illustration was modified")
        print(json.dumps({"ok": True, "runtime": runtime,
                          "matches": distances, "unrelated_rejected": True,
                          "source_feature_bytes": len(base64.b64decode(source["local_features"])),
                          "fixtures": "generated in memory from seeds 701 and 903",
                          "catalogue_or_data_directory_used": False,
                          "elapsed_seconds": round(time.monotonic() - started, 3)}, separators=(",", ":")))
        return 0
    except Exception as error:
        # Only controlled test labels are emitted, never image data, descriptors,
        # worker stderr, catalogue contents or process environments.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(json.dumps({"ok": False, "error": message[:300]}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

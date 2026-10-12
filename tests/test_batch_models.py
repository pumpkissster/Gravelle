"""Atomic administrator-only bulk model replacement, including scan persistence."""
from contextlib import closing
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Library, ReaderError


class BatchModelTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("SUMI_TEST_TEMP", ROOT / "work" / "batch-model-tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="models-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.media = self.base / "media"
        output = BytesIO()
        Image.new("RGB", (10, 15), (120, 30, 80)).save(output, "PNG")
        self.original = output.getvalue()
        for index in (1, 2, 3):
            folder = self.media / "自动模特" / f"2026-10-09 作品{index}"
            folder.mkdir(parents=True)
            (folder / "1.png").write_bytes(self.original)
        self.app = Library(self.media, self.base / "data", "", 0)
        self.addCleanup(self.app.close)
        self.app.register({"username": "admin", "password": "temporary-test-password"}, "test")
        self.library_id = self.app.save_library({"name": "批量测试", "path": str(self.media), "media_type": "photo"})["library"]["id"]
        self.wait_scan()
        self.ids = [book["id"] for book in self.app.listing()["books"]]
        self.models = [self.app.save_model({"name": name})[0]["model"]["id"] for name in ("甲", "乙")]

    def wait_scan(self):
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)

    def bindings(self):
        return {book_id: [model["id"] for model in self.app.book(book_id)["models"]] for book_id in self.ids}

    def assert_error(self, status, body):
        with self.assertRaises(ReaderError) as caught:
            self.app.batch_books(body)
        self.assertEqual(caught.exception.status, status)

    def test_replaces_multiple_books_in_order_deduplicates_and_persists_after_scan(self):
        result = self.app.batch_books({"action": "models", "ids": self.ids[:2] + [self.ids[0]],
                                      "model_ids": self.models[::-1] + [self.models[0]]})
        self.assertEqual(result, {"updated": self.ids[:2], "model_ids": self.models[::-1]})
        bound = self.bindings()
        for book_id in self.ids[:2]:
            self.assertEqual(bound[book_id], self.models[::-1])
        self.assertNotEqual(bound[self.ids[2]], self.models[::-1])
        with closing(self.app.connect()) as connection:
            manual = dict(connection.execute("SELECT book_id,models_manual FROM book_metadata"))
            self.assertEqual([manual[item] for item in self.ids[:2]], [1, 1])
        self.app.start_scan(self.library_id)
        self.wait_scan()
        self.assertEqual(self.bindings(), bound)
        self.assertTrue(all(path.read_bytes() == self.original for path in self.media.rglob("*.png")))

    def test_empty_selection_clears_models_and_remains_manual_after_default_author_changes(self):
        self.app.batch_books({"action": "models", "ids": self.ids, "model_ids": []})
        self.app.save_library({"default_author_id": self.models[0]}, self.library_id)
        self.wait_scan()
        self.assertEqual(self.bindings(), dict.fromkeys(self.ids, []))
        # A single-work edit uses exactly the same model ordering and persistence.
        self.app.save_book_metadata(self.ids[0], {"model_ids": self.models})
        self.app.start_scan(self.library_id)
        self.wait_scan()
        self.assertEqual(self.bindings()[self.ids[0]], self.models)

    def test_invalid_book_and_model_inputs_leave_the_complete_batch_unchanged(self):
        before = self.bindings()
        invalid_models = [None, "not-an-array", ["bad"], ["f" * 24], self.models * 51, [123]]
        for value in invalid_models:
            with self.subTest(model_ids=value):
                self.assert_error(400, {"action": "models", "ids": self.ids, "model_ids": value})
                self.assertEqual(self.bindings(), before)
        self.assert_error(404, {"action": "models", "ids": [self.ids[0], "f" * 24], "model_ids": self.models})
        self.assertEqual(self.bindings(), before)
        self.app.batch_books({"action": "remove", "ids": [self.ids[-1]]})
        self.assert_error(404, {"action": "models", "ids": self.ids, "model_ids": self.models})
        self.assertEqual([model["id"] for model in self.app.book(self.ids[0])["models"]], before[self.ids[0]])

    def test_mid_batch_failure_rolls_back_bindings_and_manual_flags(self):
        before = self.bindings()
        with closing(self.app.connect()) as connection:
            flags = dict(connection.execute("SELECT book_id,models_manual FROM book_metadata"))
        original = self.app._replace_book_models
        calls = 0

        def fail_second(connection, book_id, model_ids):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated transaction failure")
            original(connection, book_id, model_ids)

        with patch.object(self.app, "_replace_book_models", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self.app.batch_books({"action": "models", "ids": self.ids, "model_ids": self.models})
        self.assertEqual(self.bindings(), before)
        with closing(self.app.connect()) as connection:
            self.assertEqual(dict(connection.execute("SELECT book_id,models_manual FROM book_metadata")), flags)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_subaccount_cannot_replace_models_even_for_authorized_works(self):
        child = self.app.save_user({"username": "child", "password": "temporary-child-password",
                                    "library_ids": [self.library_id]})["user"]
        before = self.bindings()
        with self.app.acting_as(child["id"]):
            self.assert_error(403, {"action": "models", "ids": self.ids, "model_ids": self.models})
        self.assertEqual(self.bindings(), before)


if __name__ == "__main__":
    unittest.main()

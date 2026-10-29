"""Model avatar drafts use associated small covers and persist only on save."""
import base64
from contextlib import closing
import hashlib
from io import BytesIO
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from server import Library, MAX_BODY_BYTES, MAX_COVER_UPLOAD_BYTES, ReaderError
from test_integration import RunningApp


ROOT = Path(__file__).resolve().parents[1]


class ModelAvatarRegenerationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("SUMI_TEST_TEMP", ROOT / "work/model-avatar-qa"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="model-avatar-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        for index in range(5):
            folder = self.media / f"book-{index}"
            folder.mkdir(parents=True)
            Image.new("RGB", (120, 180), (30 + index * 35, 70, 180)).save(folder / "1.jpg")
        self.originals = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in self.media.rglob("*.jpg")}
        self.app = Library(self.media, self.root / "data", "", 0)
        self.addCleanup(self.app.close)
        self.app.transcoding.auto_start = self.app.image_search.auto_start = False
        self.app.register({"username": "avatar-admin", "password": "temporary-avatar-password"}, "test")
        self.model = self.app.save_model({"name": "Avatar model"})[0]["model"]
        self.library_id = self.app.save_library({"name": "Photos", "path": str(self.media),
            "media_type": "photo", "default_author_id": self.model["id"]})["library"]["id"]
        deadline = time.monotonic() + 10
        while self.app.scanning and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.app.scanning)
        self.ids = sorted(book["id"] for book in self.app.listing()["books"])
        self.assertEqual(len(self.ids), 5)

    def record(self):
        with closing(self.app.connect()) as connection:
            return dict(connection.execute("SELECT * FROM models WHERE id=?", (self.model["id"],)).fetchone())

    def draft(self, body=None):
        return self.app.regenerate_model_avatar(self.model["id"], body or {})

    def assert_error(self, status, operation, *args):
        with self.assertRaises(ReaderError) as caught:
            operation(*args)
        self.assertEqual(caught.exception.status, status)

    @staticmethod
    def jpeg(size=(256, 256), color=(190, 80, 120)):
        output = BytesIO()
        Image.new("RGB", size, color).save(output, "JPEG")
        return output.getvalue()

    def worker_result(self, index=0, face_found=False):
        return {"avatar": self.jpeg(), "index": index, "face_found": face_found}

    def test_preview_is_square_small_unsaved_and_avoids_default_cover(self):
        before = self.record()
        avatar_files = list(self.app.avatars.iterdir())
        with patch.object(self.app, "models", side_effect=AssertionError("loaded model catalogue")), \
                patch.object(self.app, "listing", side_effect=AssertionError("loaded book catalogue")):
            result = self.draft()
        self.assertNotEqual(result["cover_book_id"], self.ids[0])
        self.assertIn(result["cover_book_id"], self.ids)
        self.assertTrue(result["avatar_upload"].startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(result["avatar_upload"].split(",", 1)[1], validate=True)
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (256, 256)))
        self.assertLess(len(raw), 100_000)
        self.assertEqual(result["crop_method"], "center")
        self.assertEqual(self.record(), before)
        self.assertEqual(list(self.app.avatars.iterdir()), avatar_files)
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.originals}, self.originals)

    def test_only_saving_preview_replaces_avatar_and_keeps_sources(self):
        first = self.draft()
        self.app.save_model({"avatar_upload": first["avatar_upload"]}, self.model["id"])
        saved = self.record()
        files = sorted(self.app.avatars.iterdir())
        self.assertTrue(saved["avatar_file"])
        second = self.draft({"exclude_book_id": first["cover_book_id"]})
        self.assertNotEqual(second["cover_book_id"], first["cover_book_id"])
        # Dismissing the dialog sends no save: the prior avatar and file stay.
        self.assertEqual(self.record(), saved)
        self.assertEqual(sorted(self.app.avatars.iterdir()), files)
        payload, status = self.app.save_model({"name": "Renamed", "avatar_upload": second["avatar_upload"]}, self.model["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"]["name"], "Renamed")
        updated = self.record()
        self.assertEqual(updated["avatar_revision"], saved["avatar_revision"] + 1)
        self.assertFalse((self.app.avatars / saved["avatar_file"]).exists())
        self.assertEqual(len(list(self.app.avatars.iterdir())), 1)
        self.assertEqual(self.app.avatar("models", self.model["id"])[1], "image/jpeg")
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.originals}, self.originals)

    def test_ignores_inactive_removed_unpublished_and_unassociated_works(self):
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE books SET active=0 WHERE id=?", (self.ids[1],))
            connection.execute("UPDATE books SET removed=1 WHERE id=?", (self.ids[2],))
            connection.execute("UPDATE tc_settings SET enabled=1")
            connection.execute("UPDATE tc_books SET published=0 WHERE book_id=?", (self.ids[3],))
            connection.execute("DELETE FROM book_models WHERE book_id=? AND model_id=?", (self.ids[4], self.model["id"]))
        # One valid source is still usable even when it is the current default.
        with patch.object(self.app, "cover", wraps=self.app.cover) as cover:
            result = self.draft({"exclude_book_id": self.ids[0]})
        self.assertEqual(result["cover_book_id"], self.ids[0])
        self.assertEqual(cover.call_args_list[0].args, (self.ids[0],))
        self.assertEqual(cover.call_count, 1)

    def test_permissions_validation_and_empty_model_never_write_avatar(self):
        child = self.app.save_user({"username": "avatar-child", "password": "temporary-child-password",
                                   "library_ids": [self.library_id]})["user"]["id"]
        with self.app.acting_as(child):
            self.assert_error(403, self.draft)
        self.assert_error(404, self.app.regenerate_model_avatar, "f" * 24, {})
        for body in ({"exclude_book_id": "../../secret"}, {"exclude_book_id": 2},
                     {"exclude_book_id": True}, {"exclude_book_id": ""}, {"url": "https://example.invalid"},
                     {"path": str(self.media / "book-0/1.jpg")}, {"data": "external image data"}):
            with patch.object(self.app, "cover", side_effect=AssertionError("read arbitrary source")), \
                    patch.object(self.app.image_search, "avatar_preview", side_effect=AssertionError("processed arbitrary source")):
                self.assert_error(400, self.draft, body)
        empty = self.app.save_model({"name": "No work"})[0]["model"]
        self.assert_error(422, self.app.regenerate_model_avatar, empty["id"], {})
        self.assertEqual(list(self.app.avatars.iterdir()), [])

    def test_invalid_cover_cache_cannot_feed_large_or_wrong_format_decode(self):
        png = BytesIO()
        Image.new("RGB", (10, 10)).save(png, "PNG")
        oversized = BytesIO()
        Image.new("RGB", (1000, 1000)).save(oversized, "JPEG")
        for data, mime in ((png.getvalue(), "image/jpeg"), (oversized.getvalue(), "image/jpeg"),
                           (b"x" * (MAX_COVER_UPLOAD_BYTES + 1), "image/jpeg"), (b"small", "image/png")):
            with self.subTest(size=len(data), mime=mime), \
                    patch.object(self.app, "cover", return_value=(data, mime, "etag")), \
                    patch.object(self.app, "_decode_avatar_upload", side_effect=AssertionError("unsafe cover decoded")):
                self.assert_error(422, self.draft)
        self.assertEqual(list(self.app.avatars.iterdir()), [])

    def test_bad_cover_falls_back_and_removed_source_cannot_escape_recheck(self):
        original_cover = self.app.cover
        calls = []
        def fail_first(book_id):
            calls.append(book_id)
            if len(calls) == 1:
                raise ReaderError("damaged cover", 422)
            return original_cover(book_id)
        with patch.object(self.app, "cover", side_effect=fail_first):
            result = self.draft()
        # The face detector gets all eligible small covers so it can try another
        # source before choosing a normal crop when none contains a usable face.
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["cover_book_id"], calls[1])
        def remove_during_generation(book_id):
            response = original_cover(book_id)
            with closing(self.app.connect()) as connection, connection:
                connection.execute("DELETE FROM book_models WHERE model_id=?", (self.model["id"],))
            return response
        with patch.object(self.app, "cover", side_effect=remove_during_generation):
            self.assert_error(422, self.draft)
        self.assertEqual(list(self.app.avatars.iterdir()), [])

    def test_worker_selection_maps_to_valid_source_after_skipping_a_bad_cover(self):
        original_cover = self.app.cover
        submitted = []

        def cover(book_id):
            if book_id == self.ids[1]:
                raise ReaderError("damaged cover", 422)
            return original_cover(book_id)

        def preview(covers):
            submitted.extend(covers)
            return self.worker_result(index=1, face_found=True)

        with patch("server.secrets.SystemRandom.shuffle", return_value=None), \
                patch.object(self.app, "cover", side_effect=cover), \
                patch.object(self.app.image_search, "avatar_preview", side_effect=preview) as worker:
            result = self.draft()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(submitted, [original_cover(book_id)[0] for book_id in self.ids[2:]])
        self.assertEqual(result["cover_book_id"], self.ids[3])
        self.assertEqual(result["crop_method"], "face")
        self.assertEqual(base64.b64decode(result["avatar_upload"].split(",", 1)[1]), self.jpeg())
        self.assertEqual(list(self.app.avatars.iterdir()), [])

    def test_face_preview_is_independent_of_enabling_the_search_index(self):
        with closing(self.app.connect()) as connection, connection:
            connection.execute("UPDATE is_settings SET enabled=0,paused=1")
            before = dict(connection.execute("SELECT * FROM is_settings").fetchone())
        with patch.object(self.app.image_search, "avatar_preview", return_value=self.worker_result(face_found=True)) as worker:
            result = self.draft()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(result["crop_method"], "face")
        with closing(self.app.connect()) as connection:
            self.assertEqual(dict(connection.execute("SELECT * FROM is_settings").fetchone()), before)

    def test_only_eight_bounded_covers_are_read_or_submitted(self):
        # Add database-only sources: this test must use cached covers exclusively,
        # so it never needs to open their synthetic full-image locations.
        with closing(self.app.connect()) as connection, connection:
            source = dict(connection.execute("SELECT * FROM books WHERE id=?", (self.ids[0],)).fetchone())
            columns = list(source)
            for number in range(10):
                row = {**source, "id": f"{number + 100:024x}", "path": f"/unreadable/avatar-fixture-{number}"}
                connection.execute(f"INSERT INTO books ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                   [row[column] for column in columns])
                connection.execute("INSERT INTO book_models(book_id,model_id,position) VALUES(?,?,0)",
                                   (row["id"], self.model["id"]))
        data = self.jpeg(size=(120, 180))
        with patch.object(self.app, "cover", return_value=(data, "image/jpeg", "cached")) as covers, \
                patch.object(self.app.image_search, "avatar_preview", return_value=self.worker_result(index=7)) as worker:
            result = self.draft()
        self.assertEqual(covers.call_count, 8)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(worker.call_args.args, ([data] * 8,))
        self.assertEqual(result["cover_book_id"], covers.call_args_list[7].args[0])

    def test_avatar_lock_is_released_while_worker_runs(self):
        acquired = []

        def preview(covers):
            def acquire_from_another_thread():
                locked = self.app.avatar_lock.acquire(timeout=.5)
                acquired.append(locked)
                if locked:
                    self.app.avatar_lock.release()
            thread = threading.Thread(target=acquire_from_another_thread)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            return self.worker_result()

        with patch.object(self.app.image_search, "avatar_preview", side_effect=preview):
            self.draft()
        self.assertEqual(acquired, [True])

    def test_membership_is_rechecked_after_worker_and_falls_back_to_remaining_sources(self):
        calls = []

        def preview(covers):
            calls.append(list(covers))
            if len(calls) == 1:
                with closing(self.app.connect()) as connection, connection:
                    connection.execute("DELETE FROM book_models WHERE book_id=? AND model_id=?",
                                       (self.ids[2], self.model["id"]))
                return self.worker_result(index=1, face_found=True)
            return self.worker_result(index=len(covers) - 1, face_found=True)

        with patch("server.secrets.SystemRandom.shuffle", return_value=None), \
                patch.object(self.app.image_search, "avatar_preview", side_effect=preview):
            result = self.draft()
        self.assertEqual([len(covers) for covers in calls], [4, 3])
        self.assertNotIn(calls[0][1], calls[1])
        self.assertEqual(result["cover_book_id"], self.ids[4])
        self.assertEqual(result["crop_method"], "face")

    def test_account_disabled_during_worker_cannot_return_draft(self):
        def preview(covers):
            with closing(self.app.connect()) as connection, connection:
                connection.execute("UPDATE users SET enabled=0 WHERE id=1")
            return self.worker_result(face_found=True)

        try:
            with patch.object(self.app.image_search, "avatar_preview", side_effect=preview):
                self.assert_error(401, self.draft)
        finally:
            with closing(self.app.connect()) as connection, connection:
                connection.execute("UPDATE users SET enabled=1 WHERE id=1")
        self.assertEqual(list(self.app.avatars.iterdir()), [])

    def test_management_permission_is_checked_again_after_worker(self):
        completed = []

        def preview(covers):
            completed.append(True)
            return self.worker_result(face_found=True)

        def permission(*args):
            if completed:
                raise ReaderError("permission revoked", 403)

        with patch.object(self.app, "require_permission", side_effect=permission), \
                patch.object(self.app.image_search, "avatar_preview", side_effect=preview):
            self.assert_error(403, self.draft)
        self.assertEqual(completed, [True])
        self.assertEqual(list(self.app.avatars.iterdir()), [])

    def test_invalid_worker_results_are_rejected_without_persisting(self):
        valid = self.worker_result()
        invalid_results = (None, {}, {**valid, "index": True}, {**valid, "index": -1},
                           {**valid, "index": 4}, {**valid, "index": "0"},
                           {**valid, "face_found": 1}, {**valid, "avatar": "encoded string"},
                           {**valid, "avatar": b"broken JPEG"},
                           {**valid, "avatar": self.jpeg(size=(255, 256))})
        before = self.record()
        for value in invalid_results:
            with self.subTest(value=value if not isinstance(value, dict) else {k: type(v).__name__ for k, v in value.items()}), \
                    patch.object(self.app.image_search, "avatar_preview", return_value=value):
                self.assert_error(422, self.draft)
        self.assertEqual(self.record(), before)
        self.assertEqual(list(self.app.avatars.iterdir()), [])


class ModelAvatarRegenerationApiTests(unittest.TestCase):
    def test_http_route_requires_account_origin_and_small_valid_body(self):
        app = RunningApp()
        self.addCleanup(app.close)
        status, payload, _ = app.request("/api/models", {"name": "API model"})
        self.assertEqual(status, 201)
        model_id = payload["model"]["id"]
        app.create_library(media_type="photo")
        for book in app.books()["books"]:
            self.assertEqual(app.request(f"/api/books/{book['id']}/metadata", {"model_ids": [model_id]})[0], 200)
        route = f"/api/models/{model_id}/avatar/regenerate"
        self.assertEqual(app.request(route, {}, {"Cookie": ""})[0], 401)
        self.assertEqual(app.request(route, {}, {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(app.request(route + "?url=external", {})[0], 400)
        self.assertEqual(app.request(route, {"unexpected": True})[0], 400)
        self.assertEqual(app.request(route, {"exclude_book_id": "x" * MAX_BODY_BYTES})[0], 413)
        status, result, _ = app.request(route, {})
        self.assertEqual(status, 200, result)
        self.assertEqual(set(result), {"avatar_upload", "cover_book_id", "crop_method"})
        self.assertIn(result["crop_method"], {"face", "center"})
        self.assertTrue(result["avatar_upload"].startswith("data:image/jpeg;base64,"))
        # A preview is not served as a persisted model avatar before saving.
        self.assertEqual(app.request(f"/api/avatars/models/{model_id}")[0], 404)
        status, saved, _ = app.request(f"/api/models/{model_id}", {"avatar_upload": result["avatar_upload"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(app.request(saved["model"]["avatar_url"])[0], 200)

    def test_authorized_child_can_view_model_but_cannot_generate_avatar(self):
        app = RunningApp()
        self.addCleanup(app.close)
        status, payload, _ = app.request("/api/models", {"name": "Visible model"})
        self.assertEqual(status, 201, payload)
        model_id = payload["model"]["id"]
        library = app.create_library(media_type="photo")
        book = app.books(library["id"])["books"][0]
        self.assertEqual(app.request(f"/api/books/{book['id']}/metadata", {"model_ids": [model_id]})[0], 200)
        password = "temporary-child-password"
        status, payload, _ = app.request("/api/users", {"username": "avatar-child", "password": password,
                                                         "library_ids": [library["id"]]})
        self.assertEqual(status, 201, payload)
        self.assertEqual(app.request("/api/login", {"username": "avatar-child", "password": password})[0], 200)
        status, catalogue, _ = app.request("/api/models")
        self.assertEqual(status, 200, catalogue)
        self.assertIn(model_id, [model["id"] for model in catalogue["models"]])
        self.assertEqual(app.request(f"/api/models/{model_id}/avatar/regenerate", {})[0], 403)
        self.assertEqual(app.request(f"/api/avatars/models/{model_id}")[0], 404)


if __name__ == "__main__":
    unittest.main()

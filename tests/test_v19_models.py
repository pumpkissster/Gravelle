"""Model defaults and removal regressions using disposable synthetic media."""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request

from test_integration import APP, RunningApp, png
from server import COOKIE_NAME, Library, ReaderServer


def source_snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class ModelDefaultsAndRemovalTests(unittest.TestCase):
    def setUp(self):
        self.app = RunningApp()
        self.addCleanup(self.app.close)
        self.collection = self.app.library / "收集目录不是模特名"

    def album(self, root, title):
        folder = root / title
        folder.mkdir(parents=True)
        for index, image in enumerate(self.app.images, 1):
            (folder / f"{index}.png").write_bytes(image)
        return folder

    def model(self, name):
        status, result, _ = self.app.request("/api/models", {"name": name})
        self.assertEqual(status, 201, result)
        return result["model"]

    def models(self):
        status, result, _ = self.app.request("/api/models")
        self.assertEqual(status, 200, result)
        return result["models"]

    def library(self, library_id):
        return next(item for item in self.app.request("/api/libraries")[1]["libraries"]
                    if item["id"] == library_id)

    def create_library(self, **options):
        library = self.app.create_library(path=str(self.collection), media_type="photo", **options)
        self.app.books(library["id"])
        return library

    def rescan(self, library):
        status, result, _ = self.app.request(f"/api/libraries/{library['id']}/scan", {})
        self.assertEqual(status, 202, result)
        return self.app.books(library["id"])["books"]

    def detail(self, book_id):
        status, result, _ = self.app.request(f"/api/books/{book_id}")
        self.assertEqual(status, 200, result)
        return result

    def remove(self, model_id):
        status, result, _ = self.app.request(f"/api/models/{model_id}/remove", {})
        self.assertEqual((status, result), (200, {"removed": [model_id]}))

    def test_selected_default_model_wins_without_creating_root_or_folder_models(self):
        self.album(self.collection, "2026-01-02 直接作品")
        self.album(self.collection / "不应创建的目录模特", "2026-01-03 两级作品")
        selected = self.model("已经存在的模特")
        library = self.create_library(default_author_id=selected["id"])
        books = self.app.books(library["id"])["books"]
        self.assertEqual(len(books), 2)
        self.assertEqual([model["id"] for model in self.models()], [selected["id"]])
        self.assertEqual(self.library(library["id"])["default_author_id"], selected["id"])
        for book in books:
            self.assertEqual([model["id"] for model in book["models"]], [selected["id"]])
            self.assertEqual(book["author"], selected["name"])
            self.assertIn(book["release_date"], ("2026-01-02", "2026-01-03"))
        self.rescan(library)
        self.app.stop()
        self.app.start()
        self.app.books(library["id"])
        self.assertEqual([model["id"] for model in self.models()], [selected["id"]])
        for book in books:
            self.assertEqual([model["id"] for model in self.detail(book["id"])["models"]], [selected["id"]])

    def test_default_changes_apply_to_automatic_multi_path_books_preserving_manual_choices(self):
        first_root, second_root = self.collection / "目录甲", self.collection / "目录乙"
        self.album(first_root, "2026-02-01 作品甲")
        self.album(second_root, "2026-02-02 作品乙")
        selected, replacement, manual = (self.model(name) for name in ("所选默认", "新的默认", "手工模特"))
        library = self.create_library(paths=list(map(str, (first_root, second_root))),
                                      default_author_id=selected["id"])
        books = self.app.books(library["id"])["books"]
        self.assertEqual(len(books), 2)
        self.assertTrue(all([model["id"] for model in book["models"]] == [selected["id"]] for book in books))
        self.assertEqual(self.app.request(f"/api/books/{books[0]['id']}/metadata",
                                          {"model_ids": [manual["id"]]})[0], 200)
        self.assertEqual(self.app.request(f"/api/libraries/{library['id']}",
                                          {"default_author_id": replacement["id"]})[0], 200)
        self.app.books(library["id"])
        self.assertEqual([model["id"] for model in self.detail(books[0]["id"])["models"]], [manual["id"]])
        self.assertEqual([model["id"] for model in self.detail(books[1]["id"])["models"]], [replacement["id"]])
        self.assertEqual({model["id"] for model in self.models()}, {selected["id"], replacement["id"], manual["id"]})

    def test_without_default_two_level_model_inference_and_renamed_alias_stay_available(self):
        self.album(self.collection / "【原目录模特】", "2026-03-01 作品甲")
        self.album(self.collection / "另一目录模特", "2026-03-02 作品乙")
        selected = self.model("原目录模特")
        library = self.create_library()
        books = self.app.books(library["id"])["books"]
        self.assertEqual(len(books), 2)
        self.assertEqual({model["name"] for model in self.models()}, {"原目录模特", "另一目录模特"})
        inferred = next(book for book in books if "作品甲" in book["source_title"])
        self.assertEqual([model["id"] for model in inferred["models"]], [selected["id"]])
        self.assertEqual(self.app.request(f"/api/models/{selected['id']}", {"name": "新的展示名字"})[0], 200)
        self.rescan(library)
        updated = self.detail(inferred["id"])
        self.assertEqual([(model["id"], model["name"]) for model in updated["models"]],
                         [(selected["id"], "新的展示名字")])
        self.assertEqual(updated["author"], "新的展示名字")
        self.assertEqual(len(self.models()), 2)

    def test_delete_default_clears_references_and_avatar_preserving_books_sources_and_state(self):
        self.album(self.collection, "2026-04-01 作品甲")
        self.album(self.collection, "2026-04-02 作品乙")
        deleted, survivor = self.model("待删除默认模特"), self.model("保留模特")
        first = self.create_library(default_author_id=deleted["id"])
        second = self.create_library(name="另一媒体库", default_author_id=deleted["id"])
        first_books = self.app.books(first["id"])["books"]
        second_books = self.app.books(second["id"])["books"]
        route = f"/api/books/{first_books[0]['id']}"
        self.assertEqual(self.app.request(route + "/metadata", {"title": "保留作品资料", "cover_page": 2,
                                          "model_ids": [deleted["id"], survivor["id"]]})[0], 200)
        self.assertEqual(self.app.request(f"/api/books/{first_books[1]['id']}/metadata",
                                          {"model_ids": [deleted["id"]]})[0], 200)
        self.assertEqual(self.app.request(route + "/progress", {"page": 1, "completed": False})[0], 200)
        self.assertEqual(self.app.request(route + "/favorite", {"favorite": True})[0], 200)
        self.assertEqual(self.app.request(route + "/bookmarks", {"page": 2, "note": "保留书签"})[0], 201)
        avatar = "data:image/png;base64," + base64.b64encode(self.app.images[0]).decode()
        status, updated, _ = self.app.request(f"/api/models/{deleted['id']}", {"avatar_upload": avatar})
        self.assertEqual(status, 200, updated)
        avatar_url = updated["model"]["avatar_url"]
        self.assertEqual(self.app.request(avatar_url)[0], 200)
        baseline = self.detail(first_books[0]["id"])
        original = source_snapshot(self.app.library)
        all_ids = {book["id"] for book in first_books + second_books}
        self.remove(deleted["id"])
        self.assertEqual([model["id"] for model in self.models()], [survivor["id"]])
        self.assertEqual(self.app.request(avatar_url)[0], 404)
        self.assertEqual(list((self.app.data / "avatars").glob(f"model-{deleted['id']}-*.jpg")), [])
        for library in (first, second):
            current = self.library(library["id"])
            self.assertIsNone(current["default_author_id"])
            self.assertEqual(current["default_author"], "")
            self.rescan(library)
        self.assertEqual({book["id"] for book in self.app.books()["books"]}, all_ids)
        current = self.detail(first_books[0]["id"])
        for field in ("id", "title", "release_date", "page_count", "added_at", "cover_page", "progress", "favorite", "bookmarks"):
            self.assertEqual(current[field], baseline[field], field)
        self.assertEqual([model["id"] for model in current["models"]], [survivor["id"]])
        self.assertEqual(self.detail(first_books[1]["id"])["models"], [])
        self.assertTrue(all(self.detail(book["id"])["models"] == [] for book in second_books))
        self.assertEqual([model["id"] for model in self.models()], [survivor["id"]])
        for book_id in all_ids:
            self.assertEqual(self.app.request(f"/api/books/{book_id}/pages/0")[0:2], (200, self.app.images[0]))
            self.assertEqual(self.app.request(f"/api/books/{book_id}/cover")[0], 200)
        with closing(sqlite3.connect(self.app.data / "library.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM models WHERE id=?", (deleted["id"],)).fetchone()[0], 0)
            for table, field in (("book_models", "model_id"), ("model_folder_names", "model_id"),
                                 ("book_metadata", "folder_model_id"), ("media_libraries", "default_author_id")):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE {field}=?", (deleted["id"],)).fetchone()[0], 0)
        # Suppression is separate from manual editing: a deliberately chosen
        # new default still applies to automatic books, while manual blanks stay blank.
        for library in (first, second):
            self.assertEqual(self.app.request(f"/api/libraries/{library['id']}",
                                              {"default_author_id": survivor["id"]})[0], 200)
            self.app.books(library["id"])
        self.assertEqual(self.detail(first_books[1]["id"])["models"], [])
        self.assertTrue(all([model["id"] for model in self.detail(book["id"])["models"]] == [survivor["id"]]
                            for book in second_books))
        self.app.stop()
        self.app.start()
        self.app.books()
        self.assertEqual([model["id"] for model in self.models()], [survivor["id"]])
        self.assertEqual(self.detail(first_books[0]["id"])["progress"], baseline["progress"])
        self.assertEqual(source_snapshot(self.app.library), original)

    def test_deleted_renamed_inferred_model_and_alias_do_not_return_on_scans_or_restart(self):
        model_root = self.collection / "【旧目录模特】"
        self.album(model_root, "2026-05-01 作品甲")
        self.album(self.collection / "无关模特", "2026-05-02 作品乙")
        library = self.create_library()
        deleted = next(model for model in self.models() if model["name"] == "旧目录模特")
        books = self.app.books(library["id"])["books"]
        target = next(book for book in books if book["models"][0]["id"] == deleted["id"])
        self.assertEqual(self.app.request(f"/api/models/{deleted['id']}", {"name": "删除前新展示名"})[0], 200)
        original = source_snapshot(self.app.library)
        self.remove(deleted["id"])
        self.assertEqual(self.detail(target["id"])["models"], [])
        self.assertEqual(self.detail(target["id"])["author"], "")
        self.rescan(library)
        self.assertEqual({model["name"] for model in self.models()}, {"无关模特"})
        self.assertEqual(source_snapshot(self.app.library), original)
        self.album(model_root, "2026-05-03 后续作品")
        original = source_snapshot(self.app.library)
        self.rescan(library)
        self.app.stop()
        self.app.start()
        after = self.app.books(library["id"])["books"]
        self.assertEqual(len(after), 3)
        self.assertEqual({model["name"] for model in self.models()}, {"无关模特"})
        for book in after:
            if "旧目录模特" in book["path"]:
                self.assertEqual(book["models"], [])
                self.assertEqual(book["author"], "")
        self.assertEqual(source_snapshot(self.app.library), original)

    def test_deleted_name_can_be_explicitly_created_again_for_new_work(self):
        root = self.collection / "可重新添加模特"
        self.album(root, "2026-06-01 原作品")
        library = self.create_library()
        deleted = self.models()[0]
        original_book = self.app.books(library["id"])["books"][0]
        self.remove(deleted["id"])
        self.rescan(library)
        self.assertEqual(self.models(), [])
        recreated = self.model(deleted["name"])
        self.assertNotEqual(recreated["id"], deleted["id"])
        self.album(root, "2026-06-02 新作品")
        books = self.rescan(library)
        new_book = next(book for book in books if book["id"] != original_book["id"])
        self.assertEqual([model["id"] for model in new_book["models"]], [recreated["id"]])
        self.assertEqual(self.detail(original_book["id"])["models"], [])

    def test_remove_requires_auth_and_matching_origin_and_rejects_missing_ids_atomically(self):
        self.album(self.collection, "2026-07-01 作品")
        selected = self.model("权限测试模特")
        library = self.create_library(default_author_id=selected["id"])
        route = f"/api/models/{selected['id']}/remove"
        original = source_snapshot(self.app.library)
        self.assertEqual(self.app.request(route, {}, {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.app.request(route, {}, {"Cookie": ""})[0], 401)
        for model_id in ("f" * 24, "not-an-id", "ABCDEF" * 4, "0", "1" * 25):
            status, result, _ = self.app.request(f"/api/models/{model_id}/remove", {})
            self.assertEqual(status, 404, result)
        self.assertEqual([model["id"] for model in self.models()], [selected["id"]])
        self.assertEqual(self.library(library["id"])["default_author_id"], selected["id"])
        self.assertEqual(len(self.app.books(library["id"])["books"]), 1)
        self.remove(selected["id"])
        self.assertEqual(self.app.request(route, {})[0], 404)
        self.assertEqual(self.app.request(f"/api/models/{selected['id']}", {"name": "不得重建"})[0], 404)
        self.assertEqual(self.models(), [])
        self.assertEqual(source_snapshot(self.app.library), original)


class ModelRemovalDuringScanTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.getenv("SUMI_TEST_TEMP") or APP / "work" / "test-runtime")
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="model-removal-race-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        folder = self.media / "自动目录模特" / "2026-08-01 作品"
        folder.mkdir(parents=True)
        self.source = folder / "1.png"
        self.original = png((20, 60, 120))
        self.source.write_bytes(self.original)
        self.library = Library(self.media, self.root / "data", "", 0)
        self.addCleanup(self.library.close)
        self.library_id = self.library.save_library({"name": "竞争测试", "path": str(self.media),
                                                      "media_type": "photo"})["library"]["id"]
        self.wait_scan()
        self.book_id = self.library.listing()["books"][0]["id"]
        self.model_id = self.library.models()["models"][0]["id"]
        self.library.register({"username": "race-reader", "password": "temporary-race-password"}, "test")
        token = self.library.make_session(1)
        self.headers = {"Content-Type": "application/json", "Cookie": COOKIE_NAME + "=" + token}
        self.server = ReaderServer(("127.0.0.1", 0), self.library)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wait_scan(self):
        deadline = time.monotonic() + 10
        while self.library.scanning and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.library.scanning, "Scan did not finish")

    def remove_during_prepared_scan(self):
        prepared, release = threading.Event(), threading.Event()
        original_book = self.library._book

        def paused_book(*args, **kwargs):
            result = original_book(*args, **kwargs)
            prepared.set()
            if not release.wait(10):
                raise RuntimeError("Test scan was not released")
            return result

        self.library._book = paused_book
        self.library.start_scan(self.library_id)
        try:
            self.assertTrue(prepared.wait(10), "Scan did not reach its preparation boundary")
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.server.server_port}/api/models/{self.model_id}/remove",
                data=b"{}", headers=self.headers)
            with self.client.open(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"removed": [self.model_id]})
        finally:
            release.set()
            self.wait_scan()
            self.library._book = original_book
        result = self.library.listing(self.library_id)
        self.assertEqual(result["errors"], [])
        self.assertEqual([book["id"] for book in result["books"]], [self.book_id])
        self.assertEqual(result["books"][0]["models"], [])
        self.assertEqual(self.library.models()["models"], [])
        self.assertEqual(self.library.image(self.book_id, 0)[0], self.original)
        self.assertEqual(self.source.read_bytes(), self.original)
        with closing(self.library.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.library.start_scan(self.library_id)
        self.wait_scan()
        self.assertEqual(self.library.book(self.book_id)["models"], [])
        self.assertEqual(self.library.models()["models"], [])

    def test_inferred_model_removed_during_prepared_scan_cannot_be_recreated(self):
        self.remove_during_prepared_scan()

    def test_default_model_removed_during_prepared_scan_cannot_be_rebound(self):
        self.library.save_library({"default_author_id": self.model_id}, self.library_id)
        self.wait_scan()
        self.remove_during_prepared_scan()
        library = self.library.libraries()["libraries"][0]
        self.assertIsNone(library["default_author_id"])
        self.assertEqual(library["default_author"], "")


if __name__ == "__main__":
    unittest.main()

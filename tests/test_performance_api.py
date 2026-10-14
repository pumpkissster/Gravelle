"""Authenticated shared task budgets, using disposable HTTP/data only."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server import COOKIE_NAME, Library, ReaderServer


ROOT = Path(__file__).resolve().parents[1]


class PerformanceAPITests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work" / "performance-api-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="performance-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.data = self.root / "data"
        self.media.mkdir()
        self.app = Library(self.media, self.data, "", 0)
        self.app.register({"username": "performance-admin", "password": "temporary-performance-password"}, "test")
        child = self.app.save_user({"username": "performance-child", "password": "temporary-child-password",
                                   "library_ids": []})["user"]
        self.cookies = {"admin": f"{COOKIE_NAME}={self.app.make_session(1)}",
                        "child": f"{COOKIE_NAME}={self.app.make_session(child['id'])}"}
        self.server = None
        self.addCleanup(self.close_service)
        self.start_http()

    def start_http(self):
        self.server = ReaderServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close_service(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.server = None
        self.app.close()

    def request(self, *, method="GET", body=None, actor="admin", headers=None):
        request_headers = {"Cookie": self.cookies[actor]} if actor else {}
        content = json.dumps(body).encode() if method == "POST" else None
        if content is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, "/api/performance", body=content, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            payload = json.loads(data) if data and "application/json" in response.getheader("Content-Type", "") else data
            return response.status, payload
        finally:
            connection.close()

    def test_default_and_full_budget_range(self):
        self.assertEqual(self.request(), (200, {"taskConcurrency": 1, "maxTaskConcurrency": 8}))
        for value in range(1, 9):
            with self.subTest(value=value):
                expected = {"taskConcurrency": value, "maxTaskConcurrency": 8}
                self.assertEqual(self.request(method="POST", body={"taskConcurrency": value}), (200, expected))
                self.assertEqual(self.request(), (200, expected))

    def test_child_and_signed_out_cannot_read_or_change_global_budget(self):
        for actor, expected in ((None, 401), ("child", 403)):
            for method in ("GET", "HEAD", "POST"):
                with self.subTest(actor=actor, method=method):
                    status, _ = self.request(actor=actor, method=method, body={"taskConcurrency": 8})
                    self.assertEqual(status, expected)
        self.assertEqual(self.request()[1]["taskConcurrency"], 1)

    def test_invalid_values_and_unknown_fields_are_atomic(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 4})[0], 200)
        invalid = [{}, {"taskConcurrency": 0}, {"taskConcurrency": 9}, {"taskConcurrency": -1},
                   {"taskConcurrency": True}, {"taskConcurrency": False}, {"taskConcurrency": 2.0},
                   {"taskConcurrency": "2"}, {"taskConcurrency": None}, {"taskConcurrency": []},
                   {"taskConcurrency": {}}, {"taskConcurrency": 2, "workers": 8},
                   {"workers": 2}, [], None, "invalid"]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request(method="POST", body=body)[0], 400)
                self.assertEqual(self.request()[1]["taskConcurrency"], 4)

    def test_post_rejects_cross_origin_and_accepts_matching_origin(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 8},
                                     headers={"Origin": "https://unrelated.example"})[0], 403)
        self.assertEqual(self.request()[1]["taskConcurrency"], 1)
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 3},
                                     headers={"Origin": origin})[0], 200)
        self.assertEqual(self.request()[1]["taskConcurrency"], 3)

    def test_budget_survives_service_restart_with_accounts_and_integrity_preserved(self):
        self.assertEqual(self.request(method="POST", body={"taskConcurrency": 6})[0], 200)
        self.close_service()
        self.app = Library(self.media, self.data, "", 0)
        self.start_http()
        self.assertEqual(self.request(), (200, {"taskConcurrency": 6, "maxTaskConcurrency": 8}))
        with closing(self.app.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()

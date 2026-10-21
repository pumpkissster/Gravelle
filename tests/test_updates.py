"""Read-only update checks: mock GitHub, bound requests, cache and HTTP auth.

Run: python -m unittest discover -s tests -p test_updates.py -v
No user database is opened, and no real GitHub request is made.
"""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from server import (COOKIE_NAME, VERSION, ReaderError, ReaderServer, UpdateChecker, UpdateCheckError,
                    latest_github_release, release_configuration, release_repository,
                    release_version)


REPOSITORY = "example/gravelle"
RELEASE_URL = "https://github.com/example/gravelle/releases/tag/v1.9.0"


def release(tag="v1.9.0", **extra):
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "html_url": "https://untrusted.invalid/ignored", **extra}


def github_response(document=None, status=200, raw=None, length=None):
    response = Mock()
    response.status = status
    content = json.dumps(document if document is not None else release()).encode() if raw is None else raw
    response.read.side_effect = lambda size: content[:size]
    response.getheader.return_value = str(len(content)) if length is None else length
    return response


class ReleaseValidationTests(unittest.TestCase):
    def test_stable_versions_compare_numerically(self):
        self.assertEqual(release_version("v1.8.2"), (1, 8, 2))
        self.assertEqual(release_version("0.0.0"), (0, 0, 0))
        self.assertGreater(release_version("1.10.0"), release_version("1.9.99"))

    def test_prerelease_malformed_and_ambiguous_versions_are_rejected(self):
        for value in (None, 182, "", "v1.8", "1.8.2.0", "1.8.2-rc.1", "1.8.2+local",
                      " 1.8.2", "1.8.2\n", "01.8.2", "1.08.2", "V1.8.2", "1.2." + "9" * 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release_version(value)

    def test_repository_is_an_identifier_not_a_url(self):
        self.assertEqual(release_repository(" pumpkissster/Gravelle "), "pumpkissster/Gravelle")
        self.assertEqual(release_repository("example-org/project_reader"), "example-org/project_reader")
        for value in (None, "", "https://github.com/example/repo", "http://127.0.0.1/x", "example/../repo",
                      "example/%2e%2e", "example/repo?url=localhost", "example/repo#x", "example//repo",
                      "-example/repo", "example-/repo", "example/..", "例子/repo", "a" * 40 + "/repo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release_repository(value)

    def test_environment_configuration_takes_precedence(self):
        with patch.dict(os.environ, {"GRAVELLE_RELEASE_REPOSITORY": REPOSITORY}), \
                patch("pathlib.Path.open", side_effect=AssertionError("Must not read bundled config")):
            self.assertEqual(release_configuration(), (REPOSITORY, ""))

    def test_bundled_configuration_and_invalid_configuration(self):
        temporary_parent = Path(os.getenv("SUMI_TEST_TEMP", str(APP / "work")))
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="update-config-", dir=temporary_parent) as directory, \
                patch.dict(os.environ, {"GRAVELLE_RELEASE_REPOSITORY": ""}), \
                patch("server.__file__", str(Path(directory) / "server.py")):
            config = Path(directory) / "release.json"
            self.assertEqual(release_configuration(), (None, ""))
            config.write_text(json.dumps({"repository": REPOSITORY}), encoding="utf-8")
            self.assertEqual(release_configuration(), (REPOSITORY, ""))
            for text in ("not-json", "[]", json.dumps({"repository": "https://127.0.0.1/private"}), " " * 4097):
                config.write_text(text, encoding="utf-8")
                repository, error = release_configuration()
                self.assertIsNone(repository)
                self.assertTrue(error)


class GithubRequestTests(unittest.TestCase):
    def test_only_fixed_https_host_and_latest_path_are_requested(self):
        connection = Mock()
        connection.getresponse.return_value = github_response()
        with patch("server.http.client.HTTPSConnection", return_value=connection) as factory:
            tag, url = latest_github_release(REPOSITORY)
        factory.assert_called_once_with("api.github.com", timeout=8)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", "/repos/example/gravelle/releases/latest"))
        self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github+json")
        self.assertIn("User-Agent", kwargs["headers"])
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual((tag, url), ("v1.9.0", RELEASE_URL))
        connection.close.assert_called_once()

    def test_untrusted_redirects_and_http_errors_are_not_followed(self):
        for status in (301, 302, 307, 308, 403, 404, 429, 500):
            with self.subTest(status=status):
                connection = Mock()
                connection.getresponse.return_value = github_response(status=status)
                with patch("server.http.client.HTTPSConnection", return_value=connection) as factory, \
                        self.assertRaises(UpdateCheckError):
                    latest_github_release(REPOSITORY)
                factory.assert_called_once()
                connection.request.assert_called_once()
                connection.close.assert_called_once()

    def test_invalid_repo_never_opens_a_connection(self):
        with patch("server.http.client.HTTPSConnection") as factory, self.assertRaises(ValueError):
            latest_github_release("https://127.0.0.1/private")
        factory.assert_not_called()

    def test_drafts_prereleases_and_bad_payloads_are_ignored(self):
        documents = [release(prerelease=True), release(draft=True), release("v2.0.0-rc.1"),
                     release("latest"), release(draft="false"), [], {"tag_name": "v2.0.0"}]
        for document in documents:
            with self.subTest(document=document):
                connection = Mock()
                connection.getresponse.return_value = github_response(document)
                with patch("server.http.client.HTTPSConnection", return_value=connection), \
                        self.assertRaises(UpdateCheckError):
                    latest_github_release(REPOSITORY)

    def test_timeout_invalid_json_and_oversized_body_fail_closed(self):
        responses = [github_response(raw=b"<html>bad gateway</html>"),
                     github_response(length=str(1024 * 1024 + 1)),
                     github_response(raw=b"x" * (1024 * 1024 + 1), length="0")]
        for response in responses:
            connection = Mock()
            connection.getresponse.return_value = response
            with patch("server.http.client.HTTPSConnection", return_value=connection), \
                    self.assertRaises(UpdateCheckError):
                latest_github_release(REPOSITORY)
        connection = Mock()
        connection.request.side_effect = TimeoutError("test timeout")
        with patch("server.http.client.HTTPSConnection", return_value=connection), \
                self.assertRaises(UpdateCheckError):
            latest_github_release(REPOSITORY)
        connection.close.assert_called_once()


class UpdateCacheTests(unittest.TestCase):
    def test_unconfigured_or_invalid_configuration_never_uses_network(self):
        with patch("server.latest_github_release") as fetch:
            self.assertEqual(UpdateChecker(VERSION).check()["status"], "unconfigured")
            result = UpdateChecker(VERSION, configuration_error="发布配置无效。").check()
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["release_url"])
        fetch.assert_not_called()

    def test_new_equal_and_older_release_cannot_trigger_downgrade(self):
        for tag, expected in (("v1.9.0", "available"), ("v1.8.2", "up_to_date"), ("v1.8.1", "up_to_date")):
            with self.subTest(tag=tag), patch("server.latest_github_release", return_value=(tag, RELEASE_URL)):
                result = UpdateChecker("1.8.2", REPOSITORY).check()
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["current"], "1.8.2")

    def test_success_cache_expires_after_ten_minutes_and_returns_copies(self):
        clock = [1000.0]
        checker = UpdateChecker(VERSION, REPOSITORY)
        with patch("server.time.monotonic", side_effect=lambda: clock[0]), \
                patch("server.latest_github_release", return_value=("v1.9.0", RELEASE_URL)) as fetch:
            first = checker.check()
            first["status"] = "tampered"
            self.assertTrue(checker.check()["cached"])
            self.assertEqual(checker.check()["status"], "available")
            self.assertEqual(fetch.call_count, 1)
            clock[0] += 601
            self.assertFalse(checker.check()["cached"])
            self.assertEqual(fetch.call_count, 2)

    def test_error_cache_retries_after_thirty_seconds(self):
        clock = [1000.0]
        checker = UpdateChecker(VERSION, REPOSITORY)
        with patch("server.time.monotonic", side_effect=lambda: clock[0]), \
                patch("server.latest_github_release", side_effect=UpdateCheckError("暂时不可用。")) as fetch:
            self.assertEqual(checker.check()["status"], "error")
            self.assertTrue(checker.check()["cached"])
            self.assertEqual(fetch.call_count, 1)
            clock[0] += 31
            self.assertFalse(checker.check()["cached"])
            self.assertEqual(fetch.call_count, 2)

    def test_simultaneous_checks_share_one_request(self):
        checker = UpdateChecker(VERSION, REPOSITORY)
        def slow_release(_):
            time.sleep(0.03)
            return "v1.9.0", RELEASE_URL
        with patch("server.latest_github_release", side_effect=slow_release) as fetch, \
                ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: checker.check(), range(8)))
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(all(result["status"] == "available" for result in results))
        self.assertEqual(sum(not result["cached"] for result in results), 1)


class UpdateHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A fake auth store makes this an HTTP test without touching any database.
        library = Mock()
        library.request_condition = threading.Condition()
        library.request_context = threading.local()
        library.request_stopping = False
        library.active_requests = 0
        # Match the real network-settings contract while keeping this auth-only
        # fixture free of databases and independent HTTP listeners.
        library.network.lock = threading.RLock()
        library.network.current.return_value = {
            "security_entry_enabled": False, "security_entry_code": "",
            "share_port_enabled": False, "public_share_url": "",
            "entry_epoch": "0" * 64,
        }
        library.session.side_effect = lambda token: {
            "authenticated": token in ("update-test-session", "child-test-session"),
            "user": {"id": 1 if token == "update-test-session" else 2}
            if token in ("update-test-session", "child-test-session") else None}
        def require_admin():
            if getattr(library.request_context, "user_id", None) != 1:
                raise ReaderError("此操作需要管理员权限。", 403)
        library.require_admin.side_effect = require_admin
        with patch("server.release_configuration", return_value=(None, "")):
            cls.server = ReaderServer(("127.0.0.1", 0), library)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path="/api/updates", authenticated=True, token="update-test-session"):
        headers = {"Cookie": f"{COOKIE_NAME}={token}"} if authenticated else {}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.headers), json.loads(response.read())
        finally:
            connection.close()

    def test_authentication_precedes_update_check(self):
        with patch.object(self.server.updates, "check") as check:
            status, _, result = self.request(authenticated=False)
        self.assertEqual(status, 401)
        self.assertNotIn("latest", result)
        check.assert_not_called()

    def test_child_account_cannot_check_service_updates(self):
        with patch.object(self.server.updates, "check") as check:
            status, _, result = self.request(token="child-test-session")
        self.assertEqual(status, 403)
        self.assertNotIn("latest", result)
        check.assert_not_called()

    def test_authenticated_response_uses_server_configuration_and_is_not_http_cached(self):
        status, headers, result = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["current"], VERSION)

    def test_request_cannot_supply_repository_or_arbitrary_url(self):
        with patch.object(self.server.updates, "check") as check:
            for query in ("url=http://127.0.0.1/private", "repository=other/repo", "force=1"):
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/updates?" + query)[0], 400)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()

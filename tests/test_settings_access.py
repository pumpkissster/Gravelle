"""LAN access URLs are accurate, authenticated and independent of media data."""

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import COOKIE_NAME, Library, ReaderServer, access_urls


class AccessAddressTests(unittest.TestCase):
    def test_current_lan_host_keeps_docker_published_port(self):
        with patch("server._lan_ipv4_addresses", side_effect=AssertionError("No discovery needed")):
            result = access_urls("192.168.50.10:18080", False, ("0.0.0.0", 8080))
        self.assertEqual(result, {"lan_url": "http://192.168.50.10:18080",
                                  "lan_urls": ["http://192.168.50.10:18080"]})

    def test_reverse_proxy_keeps_hostname_and_https(self):
        self.assertEqual(access_urls("photos.home.example:8443", True, ("127.0.0.1", 8080))["lan_url"],
                         "https://photos.home.example:8443")

    def test_nas_hostname_works(self):
        self.assertEqual(access_urls("diskstation.local:8080", False, ("0.0.0.0", 8080))["lan_url"],
                         "http://diskstation.local:8080")

    def test_localhost_uses_default_route_instead_of_virtual_adapters(self):
        with patch("server._container_runtime", return_value=False), \
                patch("server._lan_ipv4_addresses", return_value=["192.168.50.10", "10.0.0.15"]):
            result = access_urls("127.0.0.1:8080", False, ("0.0.0.0", 8080))
        self.assertEqual(result["lan_url"], "http://192.168.50.10:8080")
        self.assertEqual(result["lan_urls"], ["http://192.168.50.10:8080", "http://10.0.0.15:8080"])

    def test_specific_lan_binding_is_preferred(self):
        with patch("server._container_runtime", return_value=False):
            result = access_urls("localhost:8080", False, ("192.168.1.8", 8080))
        self.assertEqual(result["lan_url"], "http://192.168.1.8:8080")

    def test_loopback_only_service_does_not_advertise_unreachable_address(self):
        for binding in ("127.0.0.1", "127.0.0.2", "::1"):
            with self.subTest(binding=binding), patch("server._lan_ipv4_addresses") as discover:
                self.assertEqual(access_urls("localhost:8080", False, (binding, 8080)),
                                 {"lan_url": None, "lan_urls": []})
                discover.assert_not_called()

    def test_container_localhost_does_not_guess_nas_address_or_port(self):
        with patch("server._container_runtime", return_value=True), patch("server._lan_ipv4_addresses") as discover:
            self.assertIsNone(access_urls("localhost:8080", False, ("0.0.0.0", 8080))["lan_url"])
            discover.assert_not_called()

    def test_default_port_is_omitted_after_discovery(self):
        with patch("server._container_runtime", return_value=False), \
                patch("server._lan_ipv4_addresses", return_value=["192.168.1.8"]):
            self.assertEqual(access_urls("localhost", False, ("0.0.0.0", 80))["lan_url"],
                             "http://192.168.1.8")

    def test_unknown_network_returns_no_url(self):
        with patch("server._container_runtime", return_value=False), \
                patch("server._lan_ipv4_addresses", return_value=[]):
            self.assertEqual(access_urls("localhost:8080", False, ("0.0.0.0", 8080)),
                             {"lan_url": None, "lan_urls": []})

    def test_ipv6_private_address_keeps_brackets(self):
        self.assertEqual(access_urls("[fd12:3456::10]:8080", False, ("0.0.0.0", 8080))["lan_url"],
                         "http://[fd12:3456::10]:8080")

    def test_invalid_hosts_and_non_lan_ip_addresses_are_not_links(self):
        for host in ("", "javascript:alert(1)", "user@192.168.1.2:8080", "192.168.1.2/path",
                     "192.168.1.2:0", "192.168.1.2:65536", "192.168.1.2:bad", "bad host", "a..b",
                     "[bad", "192.168.1.2?query", "192.168.1.2#fragment", "8.8.8.8:8080",
                     "169.254.1.2:8080", "[fe80::1]:8080"):
            with self.subTest(host=host):
                self.assertEqual(access_urls(host, False, ("0.0.0.0", 8080)),
                                 {"lan_url": None, "lan_urls": []})


class AccessHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        media = root / "media"
        media.mkdir()
        cls.library = Library(media, root / "data", "", 0)
        cls.library.register({"username": "access-test", "password": "temporary-access-password"}, "test")
        cls.cookie = f"{COOKIE_NAME}={cls.library.make_session(1)}"
        cls.server = ReaderServer(("127.0.0.1", 0), cls.library)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.library.close()
        cls.temporary.cleanup()

    def request(self, authenticated=True, method="GET", extra=None):
        headers = {"Host": "192.168.50.10:18080"}
        if authenticated:
            headers["Cookie"] = self.cookie
        headers.update(extra or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, "/api/access", headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.headers), json.loads(payload) if payload else None
        finally:
            connection.close()

    def test_login_is_required_before_network_information(self):
        status, _, body = self.request(authenticated=False)
        self.assertEqual(status, 401)
        self.assertNotIn("lan_url", body)

    def test_authenticated_request_retains_browser_host_and_is_not_cached(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["lan_url"], "http://192.168.50.10:18080")

    def test_proxy_https_is_preserved_but_forwarded_host_is_not_trusted(self):
        status, _, body = self.request(extra={"X-Forwarded-Proto": "https",
                                               "X-Forwarded-Host": "unrelated.example"})
        self.assertEqual(status, 200)
        self.assertEqual(body["lan_url"], "https://192.168.50.10:18080")

    def test_head_has_headers_without_body(self):
        status, headers, body = self.request(method="HEAD")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertIsNone(body)


if __name__ == "__main__":
    unittest.main()

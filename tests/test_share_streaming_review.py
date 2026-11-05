"""Independent protocol and resource-lifetime checks for guest image streams.

All images, accounts, databases, and temporary files are generated fixtures.
No network listener, personal media, or existing deployment is touched.
"""
from contextlib import closing, contextmanager
import base64
import errno
import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import share_crypto as crypto
from sharing import SharingManager
from server import ReaderError, ReaderHandler
from test_sharing import Catalogue, SharingError


CHUNK_BYTES = 1024 * 1024


class BoundedReader(BytesIO):
    """Reject a hidden read-all while allowing ordinary short binary reads."""
    def __init__(self, value, hook=None):
        super().__init__(value)
        self.requests = []
        self.hook = hook

    def read(self, size=-1):
        if not 0 <= size <= CHUNK_BYTES:
            raise AssertionError(f"Unbounded source read: {size}")
        self.requests.append(size)
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return super().read(size)


class RawReader:
    """File-like source without the immutable BytesIO exemption."""
    def __init__(self, value, hook=None):
        self.buffer = BoundedReader(value, hook)

    def read(self, size=-1):
        return self.buffer.read(size)

    def close(self):
        self.buffer.close()

    @property
    def closed(self):
        return self.buffer.closed


class StreamCatalogue(Catalogue):
    def __init__(self, database):
        super().__init__(database)
        self.stop_event = threading.Event()
        self.source_handles = []
        self.source_factory = BoundedReader
        self.exit_hook = None
        self.etag_override = None

    @contextmanager
    def open_image(self, book_id, index, width=None):
        if self.request_context.user_id != 1:
            raise AssertionError("Guest stream did not use its creator")
        source = self.source_factory(self.image_bytes, self.read_hook)
        self.source_handles.append(source)
        etag = self.etag_override or '"' + hashlib.sha256(self.image_bytes).hexdigest() + '"'
        with closing(source):
            yield source, len(self.image_bytes), "image/png", etag
            if self.exit_hook:
                self.exit_hook()


class ShareCryptoStreamingReviewTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.nonce = bytes(range(12))
        self.aad = b"gravelle-share-v1:generated-review:7"

    def test_stream_matches_previous_envelope_at_chunk_boundaries_for_every_mime(self):
        for mime in crypto.MIME_CODES:
            for size in (1, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 17):
                with self.subTest(mime=mime, size=size):
                    data = (bytes(range(256)) * (size // 256 + 1))[:size]
                    with BoundedReader(data) as source, BytesIO() as target:
                        with patch.object(crypto.secrets, "token_bytes", return_value=self.nonce):
                            length = crypto.encrypt_stream(source, target, len(data), mime, self.key, self.aad)
                            previous = crypto.encrypt(data, mime, self.key, self.aad)
                        packet = target.getvalue()
                        self.assertEqual(packet, previous)
                        self.assertEqual(length, len(data) + crypto.HEADER_BYTES + crypto.TAG_BYTES)
                        self.assertEqual(len(packet), length)
                        self.assertTrue(source.requests)
                        self.assertEqual(AESGCM(self.key).decrypt(packet[4:16], packet[17:], packet[:17] + self.aad), data)

    def test_callbacks_observe_every_source_byte_and_can_cancel_before_completion(self):
        data = b"generated image data" * 100000
        chunks, checks = [], []
        with BoundedReader(data) as source, BytesIO() as target:
            crypto.encrypt_stream(source, target, len(data), "image/png", self.key,
                                 self.aad, check=lambda: checks.append(True),
                                 on_chunk=chunks.append)
        self.assertTrue(checks)
        self.assertTrue(chunks)
        self.assertEqual(b"".join(chunks), data)
        self.assertLessEqual(max(map(len, chunks)), CHUNK_BYTES)

        class Stopped(Exception):
            pass

        with BoundedReader(data) as source, BytesIO() as target:
            def cancel():
                raise Stopped("generated cancellation")
            with self.assertRaises(Stopped):
                crypto.encrypt_stream(source, target, len(data), "image/png", self.key, self.aad, check=cancel)
            self.assertLess(len(target.getvalue()), len(data) + crypto.HEADER_BYTES + crypto.TAG_BYTES)

    def test_truncated_or_growing_source_never_returns_a_successful_packet(self):
        for source_bytes, advertised in ((b"short", 7), (b"longer", 4)):
            with self.subTest(advertised=advertised), BoundedReader(source_bytes) as source, BytesIO() as target:
                with self.assertRaises((ValueError, OSError, EOFError)):
                    crypto.encrypt_stream(source, target, advertised, "image/png", self.key, self.aad)

    def test_stream_keeps_header_context_and_tag_authentication(self):
        with BoundedReader(b"generated secret page") as source, BytesIO() as target:
            crypto.encrypt_stream(source, target, len(b"generated secret page"), "image/png", self.key, self.aad)
            original = target.getvalue()
        for position in (0, 4, 16, 17, len(original) - 1):
            damaged = bytearray(original)
            damaged[position] ^= 1
            with self.subTest(position=position), self.assertRaises(InvalidTag):
                AESGCM(self.key).decrypt(bytes(damaged[4:16]), bytes(damaged[17:]), bytes(damaged[:17]) + self.aad)


class _ShareResponseFixture:
    error_type = SharingError

    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "work" / "share-streaming-review"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.library = StreamCatalogue(Path(temporary.name) / "catalogue.sqlite3")
        self.manager = SharingManager(self.library, self.error_type)
        self.addCleanup(self.manager.close)
        created = self.manager.create({"book_id": "a" * 24, "password": "generated-review-password"})
        self.share = created["share"]
        self.guest = self.manager.unlock(self.share["token"], created["password"])

    def response(self):
        return self.manager.image_response(self.share["token"], self.guest["session"], 0)

    def statement(self, query, arguments=()):
        with closing(self.library.connect()) as connection, connection:
            connection.execute(query, arguments)

    def assert_status(self, status, operation):
        with self.assertRaises(self.error_type) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)


class ShareResponseStreamingReviewTests(_ShareResponseFixture, unittest.TestCase):
    def test_envelope_is_complete_before_yield_and_closed_after_exit(self):
        with self.response() as response:
            self.assertEqual(response.mime, "application/octet-stream")
            self.assertEqual(response.length, len(self.library.image_bytes) + 33)
            packet = response.source.read()
            self.assertEqual(len(packet), response.length)
            key = base64.urlsafe_b64decode(self.guest["key"] + "=")
            aad = ("gravelle-share-v1:" + self.share["token"] + ":0").encode()
            self.assertEqual(AESGCM(key).decrypt(packet[4:16], packet[17:], packet[:17] + aad), self.library.image_bytes)
            response.check()
            output = response.source
        self.assertTrue(output.closed)
        self.assertTrue(all(source.closed for source in self.library.source_handles))

    def test_two_slots_remain_held_through_send_context_and_release_at_exit(self):
        with self.response() as first:
            with self.response() as second:
                def third():
                    with self.response():
                        self.fail("Third simultaneous response entered")
                self.assert_status(503, third)
                self.assertFalse(first.source.closed)
                self.assertFalse(second.source.closed)
            with self.response() as replacement:
                self.assertFalse(replacement.source.closed)
        with self.response():
            with self.response():
                pass

    def test_disconnect_exception_closes_output_and_returns_the_slot(self):
        output = None
        with self.assertRaises(BrokenPipeError):
            with self.response() as response:
                output = response.source
                raise BrokenPipeError("generated client disconnect")
        self.assertTrue(output.closed)
        with self.response():
            with self.response():
                pass

    def test_known_expiry_is_checked_during_sending_without_poll_interval_delay(self):
        with self.response() as response:
            with patch("sharing.time.time", return_value=self.share["expires_at"] + 1):
                self.assert_status(410, response.check)

    def test_logout_or_revocation_is_rechecked_during_sending(self):
        for mutation in ("logout", "revoke"):
            with self.subTest(mutation=mutation):
                # Separate fresh capability for each revocation path.
                created = self.manager.create({"book_id": "a" * 24})
                share = created["share"]
                guest = self.manager.unlock(share["token"], created["password"])
                with self.manager.image_response(share["token"], guest["session"], 0) as response:
                    if mutation == "logout":
                        self.manager.logout(share["token"], guest["session"])
                    else:
                        self.manager.revoke(share["id"])
                    with patch("sharing.time.monotonic", return_value=time.monotonic() + 2):
                        self.assert_status(401 if mutation == "logout" else 410, response.check)

    def test_revocation_from_source_read_hook_prevents_any_response_from_yielding(self):
        self.library.read_hook = lambda: self.manager.revoke(self.share["id"])
        def open_response():
            with self.response():
                self.fail("Revoked share yielded an encrypted response")
        self.assert_status(410, open_response)
        self.assertTrue(all(source.closed for source in self.library.source_handles))

    def test_source_context_exit_revocation_is_checked_before_yield(self):
        self.library.exit_hook = lambda: self.manager.revoke(self.share["id"])
        def open_response():
            with self.response():
                self.fail("Source context exit revoked this share before headers")
        self.assert_status(410, open_response)
        self.assertTrue(all(source.closed for source in self.library.source_handles))

    def test_source_context_exit_failure_does_not_yield_or_leak_a_slot(self):
        def fail_exit():
            raise OSError("generated source close failure")
        self.library.exit_hook = fail_exit
        def open_response():
            with self.response():
                self.fail("Failing source context unexpectedly yielded")
        self.assert_status(422, open_response)
        self.assertTrue(all(source.closed for source in self.library.source_handles))
        self.library.exit_hook = None
        with self.response():
            with self.response():
                pass

    def test_mutable_source_hash_is_checked_before_yield(self):
        self.library.source_factory = RawReader
        self.library.etag_override = '"' + "0" * 64 + '"'
        def open_response():
            with self.response():
                self.fail("Changed source hash unexpectedly yielded")
        self.assert_status(422, open_response)
        self.assertTrue(all(source.closed for source in self.library.source_handles))
        self.library.etag_override = None
        with self.response() as response:
            self.assertEqual(len(response.source.read()), response.length)

    def test_disk_full_cleans_private_spool_and_releases_slots(self):
        class FullPacket(BytesIO):
            def write(self, value):
                raise OSError(errno.ENOSPC, "generated full disk")
        packet = FullPacket()
        def open_response():
            with self.response():
                self.fail("Disk-full preparation unexpectedly yielded")
        with patch("sharing.tempfile.TemporaryFile", return_value=packet) as factory:
            self.assert_status(507, open_response)
        self.assertEqual(factory.call_args.kwargs["dir"], self.library.data)
        self.assertTrue(packet.closed)
        self.assertTrue(all(source.closed for source in self.library.source_handles))
        with self.response():
            with self.response():
                pass

    def test_shutdown_during_sending_fails_and_slot_is_released(self):
        with self.response() as response:
            self.manager.close()
            self.assert_status(503, response.check)
        self.assertTrue(self.manager.image_slots.acquire(blocking=False))
        self.assertTrue(self.manager.image_slots.acquire(blocking=False))
        self.manager.image_slots.release()
        self.manager.image_slots.release()


class ShareHTTPStreamingReviewTests(_ShareResponseFixture, unittest.TestCase):
    """Exercise actual HTTP dispatch and error handling without opening sockets."""
    error_type = ReaderError

    def setUp(self):
        super().setUp()
        self.library.sharing = self.manager
        self.library.request_condition = threading.Condition()
        self.library.request_stopping = False
        self.library.active_requests = 0
        self.library.image_bytes = b"generated large source " * 100000

    def handler(self, *, command="GET", on_write=None):
        case = self

        class Writer:
            def __init__(self):
                self.sizes = []
                self.total = 0

            def write(self, data):
                case.assertLessEqual(len(data), CHUNK_BYTES)
                self.sizes.append(len(data))
                if on_write is not None:
                    on_write(data, self)
                self.total += len(data)

        handler = ReaderHandler.__new__(ReaderHandler)
        handler.server = SimpleNamespace(library=self.library)
        handler.command = command
        handler.path = "/api/share/" + self.share["token"] + "/pages/0"
        handler.client_address = ("127.0.0.1", 0)
        handler.close_connection = False
        handler.network_gate = lambda: None
        handler.share_session_token = lambda: self.guest["session"]
        handler.statuses = []
        handler.headers_written = {}
        handler.json_errors = []
        handler.send_response = handler.statuses.append
        handler.send_header = handler.headers_written.__setitem__
        handler.end_headers = lambda: None
        handler.respond = lambda *args, **kwargs: handler.json_errors.append((args, kwargs))
        handler.wfile = Writer()
        return handler

    def dispatch(self, handler):
        outputs = []
        original = self.manager.image_response

        @contextmanager
        def capture(*args, **kwargs):
            with original(*args, **kwargs) as response:
                outputs.append(response.source)
                yield response

        with patch.object(self.manager, "image_response", side_effect=capture):
            handler._handle(handler.get_route)
        self.assertEqual(self.library.active_requests, 0)
        self.assertTrue(all(output.closed for output in outputs))
        self.assertTrue(all(source.closed for source in self.library.source_handles))
        return outputs

    def assert_slots_free(self):
        acquired = []
        try:
            acquired = [self.manager.image_slots.acquire(blocking=False) for _ in range(3)]
            self.assertEqual(acquired, [True, True, False])
        finally:
            for held in acquired:
                if held:
                    self.manager.image_slots.release()

    def test_http_slot_stays_held_during_every_bounded_body_write(self):
        self.manager.image_slots.acquire()
        try:
            def check_slot(data, writer):
                held = self.manager.image_slots.acquire(blocking=False)
                if held:
                    self.manager.image_slots.release()
                self.assertFalse(held, "HTTP returned its image slot before sending finished")
            handler = self.handler(on_write=check_slot)
            self.dispatch(handler)
            self.assertEqual(handler.statuses, [200])
            self.assertEqual(handler.json_errors, [])
            self.assertGreater(len(handler.wfile.sizes), 1)
            self.assertEqual(handler.wfile.total, int(handler.headers_written["Content-Length"]))
            self.assertFalse(handler.close_connection)
        finally:
            self.manager.image_slots.release()
        self.assert_slots_free()

    def test_http_write_failure_closes_connection_without_appending_json(self):
        for error in (BrokenPipeError("generated disconnect"), OSError("generated write failure")):
            with self.subTest(error=type(error).__name__):
                def fail_write(data, writer):
                    raise error
                handler = self.handler(on_write=fail_write)
                self.dispatch(handler)
                self.assertEqual(handler.statuses, [200])
                self.assertEqual(handler.json_errors, [])
                self.assertTrue(handler.close_connection)
                self.assert_slots_free()

    def test_http_head_does_not_write_body_and_releases_spool_and_slots(self):
        def unexpected_write(data, writer):
            self.fail("HEAD wrote an encrypted response body")
        handler = self.handler(command="HEAD", on_write=unexpected_write)
        self.dispatch(handler)
        self.assertEqual(handler.statuses, [200])
        self.assertEqual(handler.wfile.sizes, [])
        self.assertEqual(handler.json_errors, [])
        self.assertGreater(int(handler.headers_written["Content-Length"]), 0)
        self.assert_slots_free()

    def test_http_expiry_after_headers_truncates_body_without_json(self):
        with patch("sharing.time.time", return_value=time.time()) as clock:
            def expire_after_first_chunk(data, writer):
                clock.return_value = self.share["expires_at"] + 1
            handler = self.handler(on_write=expire_after_first_chunk)
            self.dispatch(handler)
        self.assertEqual(handler.statuses, [200])
        self.assertEqual(handler.json_errors, [])
        self.assertTrue(handler.close_connection)
        self.assertEqual(len(handler.wfile.sizes), 1)
        self.assertGreater(handler.wfile.total, 0)
        self.assertLess(handler.wfile.total, int(handler.headers_written["Content-Length"]))
        self.assert_slots_free()


if __name__ == "__main__":
    unittest.main()

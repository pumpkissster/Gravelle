"""Authenticated share envelope tests, including the cross-browser fixture."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import share_crypto as crypto

ROOT = Path(__file__).resolve().parents[1]


class ShareCryptoTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.context = b"gravelle-share-v1:public-token:3"

    def open_packet(self, packet, key=None, aad=None):
        return AESGCM(key or self.key).decrypt(packet[4:16], packet[17:], packet[:17] + (self.context if aad is None else aad))

    def test_random_session_keys_and_nonces(self):
        first, second = crypto.generate_key(), crypto.generate_key()
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, second)
        one = crypto.encrypt(b"image", "image/jpeg", first)
        two = crypto.encrypt(b"image", "image/jpeg", first)
        self.assertNotEqual(one[4:16], two[4:16])
        self.assertNotEqual(one, two)
        self.assertNotIn(b"image", one)
        encoded = crypto.encode_key(first)
        self.assertEqual(len(encoded), 43)
        self.assertNotIn("=", encoded)
        self.assertEqual(base64.urlsafe_b64decode(encoded + "="), first)

    def test_each_supported_mime_keeps_the_exact_page_bytes(self):
        data = bytes(range(256)) * 5
        for mime, code in crypto.MIME_CODES.items():
            with self.subTest(mime=mime):
                packet = crypto.encrypt(data, mime, self.key, self.context)
                self.assertEqual(packet[:4], b"GSH1")
                self.assertEqual(packet[16], code)
                self.assertEqual(len(packet), len(data) + 17 + 16)
                self.assertEqual(self.open_packet(packet), data)

    def test_header_page_context_key_and_ciphertext_are_authenticated(self):
        packet = crypto.encrypt(b"JPEG data", "image/jpeg", self.key, self.context)
        for index in (0, 4, 15, 16, 17, len(packet) - 1):
            damaged = bytearray(packet)
            damaged[index] ^= 1
            with self.subTest(index=index), self.assertRaises(InvalidTag):
                self.open_packet(bytes(damaged))
        for context in (b"", b"gravelle-share-v1:public-token:4", b"gravelle-share-v1:another-token:3"):
            with self.subTest(context=context), self.assertRaises(InvalidTag):
                self.open_packet(packet, aad=context)
        with self.assertRaises(InvalidTag):
            self.open_packet(packet, key=bytes(reversed(self.key)))

    def test_fixed_python_fixture_is_the_same_input_for_native_and_fallback_js(self):
        vector = json.loads((ROOT / "tests/share_crypto_vectors.json").read_text(encoding="utf-8"))
        with patch.object(crypto.secrets, "token_bytes", return_value=bytes(range(12))):
            packet = crypto.encrypt(bytes.fromhex(vector["data_hex"]), vector["mime"], self.key, vector["aad"].encode())
        self.assertEqual(packet.hex(), vector["packet_hex"])
        self.assertEqual(crypto.encode_key(self.key), vector["key"])

    def test_invalid_keys_types_limits_and_unrenderable_mimes_fail_closed(self):
        for key in (b"", bytes(16), bytes(24), bytes(33), "password", bytearray(32), None):
            with self.subTest(key=type(key).__name__), self.assertRaises(ValueError):
                crypto.encrypt(b"data", "image/png", key)
            with self.assertRaises(ValueError):
                crypto.encode_key(key)
        for data in (b"", "data", bytearray(b"data"), None):
            with self.subTest(data=type(data).__name__), self.assertRaises(ValueError):
                crypto.encrypt(data, "image/png", self.key)
        for mime in ("text/html", "image/svg+xml", "application/octet-stream", "image/png; charset=utf-8", None):
            with self.subTest(mime=mime), self.assertRaises(ValueError):
                crypto.encrypt(b"<script>", mime, self.key)
        for context in ("string", bytearray(2), b"x" * 4097, None):
            with self.subTest(context=type(context).__name__), self.assertRaises(ValueError):
                crypto.encrypt(b"data", "image/png", self.key, context)
        with patch.object(crypto, "MAX_IMAGE_BYTES", 5), self.assertRaises(ValueError):
            crypto.encrypt(b"123456", "image/png", self.key)


if __name__ == "__main__":
    unittest.main()

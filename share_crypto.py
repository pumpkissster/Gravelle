"""Authenticated image envelopes for an already-authorized share session.

This module deliberately does not decide who may view a page. Every request
must be authorized by the sharing service before encryption, and again after
image loading. Encryption is not a replacement for HTTPS or access control.
"""
from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"GSH1"
NONCE_BYTES = 12
KEY_BYTES = 32
HEADER_BYTES = 17
TAG_BYTES = 16
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MIME_CODES = {
    "image/jpeg": 1,
    "image/png": 2,
    "image/webp": 3,
    "image/gif": 4,
    "image/avif": 5,
    "image/bmp": 6,
    "image/tiff": 7,
}


def generate_key() -> bytes:
    """Generate a key for one short-lived visitor session, never a password."""
    return AESGCM.generate_key(bit_length=256)


def _key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError("A share image key must contain exactly 32 bytes.")
    return key


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(_key(key)).rstrip(b"=").decode("ascii")


def encrypt(data: bytes, mime: str, key: bytes, aad: bytes = b"") -> bytes:
    """Return GSH1 | nonce12 | MIME1 | AES-256-GCM ciphertext and tag16.

    Both the envelope header and caller-supplied page identity are authenticated.
    The caller rotates keys per visitor session, limits session lifetime/request
    count, and must not persist these envelopes as the original image cache.
    """
    _key(key)
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("The share image is empty or exceeds the size limit.")
    if mime not in MIME_CODES:
        raise ValueError("The share image type is not supported.")
    if not isinstance(aad, bytes) or len(aad) > 4096:
        raise ValueError("The share image context is invalid.")
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + nonce + bytes((MIME_CODES[mime],))
    return header + AESGCM(key).encrypt(nonce, data, header + aad)

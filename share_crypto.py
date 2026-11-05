"""Authenticated image envelopes for an already-authorized share session.

This module deliberately does not decide who may view a page. Every request
must be authorized by the sharing service before encryption, and again after
image loading. Encryption is not a replacement for HTTPS or access control.
"""
from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"GSH1"
NONCE_BYTES = 12
KEY_BYTES = 32
HEADER_BYTES = 17
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024
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
    if not isinstance(data, bytes) or not data:
        raise ValueError("The share image is empty or invalid.")
    if mime not in MIME_CODES:
        raise ValueError("The share image type is not supported.")
    if not isinstance(aad, bytes) or len(aad) > 4096:
        raise ValueError("The share image context is invalid.")
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + nonce + bytes((MIME_CODES[mime],))
    return header + AESGCM(key).encrypt(nonce, data, header + aad)


def encrypt_stream(source, target, length: int, mime: str, key: bytes,
                   aad: bytes = b"", *, check=None, on_chunk=None) -> int:
    """Write the identical GSH1 envelope using bounded plaintext buffers.

    The caller owns the private seekable destination and must not expose it
    before this function completes and source/access checks have succeeded.
    No partially authenticated packet is returned on failure.
    """
    _key(key)
    if type(length) is not int or length <= 0:
        raise ValueError("The share image is empty or invalid.")
    if mime not in MIME_CODES:
        raise ValueError("The share image type is not supported.")
    if not isinstance(aad, bytes) or len(aad) > 4096:
        raise ValueError("The share image context is invalid.")
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + nonce + bytes((MIME_CODES[mime],))
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header + aad)
    if check:
        check()
    target.write(header)
    remaining = length
    while remaining:
        if check:
            check()
        chunk = source.read(min(CHUNK_BYTES, remaining))
        if not chunk or len(chunk) > remaining:
            raise ValueError("The share image changed while being read.")
        if on_chunk:
            on_chunk(chunk)
        target.write(cipher.update(chunk))
        remaining -= len(chunk)
    if source.read(1):
        raise ValueError("The share image changed while being read.")
    if check:
        check()
    target.write(cipher.finalize())
    target.write(cipher.tag)
    return HEADER_BYTES + length + TAG_BYTES

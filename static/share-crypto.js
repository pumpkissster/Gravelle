(function (root) {
  'use strict';

  const HEADER_BYTES = 17;
  const TAG_BYTES = 16;
  const MAX_IMAGE_BYTES = 64 * 1024 * 1024;
  const MIME_TYPES = Object.freeze({
    1: 'image/jpeg', 2: 'image/png', 3: 'image/webp', 4: 'image/gif',
    5: 'image/avif', 6: 'image/bmp', 7: 'image/tiff',
  });

  function invalid() { return new Error('分享图片无法解密，请重新打开分享链接。'); }

  function bytes(value) {
    if (value instanceof Uint8Array) return value;
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    throw invalid();
  }

  function decodeKey(value) {
    if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(value)) throw invalid();
    let decoded;
    try { decoded = root.atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='); }
    catch (_) { throw invalid(); }
    if (decoded.length !== 32) throw invalid();
    // Canonical unpadded base64url rejects ambiguous final padding bits.
    if (root.btoa(decoded).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') !== value) throw invalid();
    return Uint8Array.from(decoded, character => character.charCodeAt(0));
  }

  function context(value) {
    const encoded = value === undefined ? new Uint8Array(0)
      : typeof value === 'string' ? new TextEncoder().encode(value) : bytes(value);
    if (encoded.length > 4096) throw invalid();
    return encoded;
  }

  async function decrypt(packet, base64key, aad) {
    let key;
    try {
      const payload = bytes(packet);
      if (payload.length <= HEADER_BYTES + TAG_BYTES || payload.length > MAX_IMAGE_BYTES + HEADER_BYTES + TAG_BYTES) throw invalid();
      if (payload[0] !== 71 || payload[1] !== 83 || payload[2] !== 72 || payload[3] !== 49) throw invalid();
      const mime = MIME_TYPES[payload[16]];
      if (!mime) throw invalid();
      key = decodeKey(base64key);
      const extra = context(aad);
      const additionalData = new Uint8Array(HEADER_BYTES + extra.length);
      additionalData.set(payload.subarray(0, HEADER_BYTES));
      additionalData.set(extra, HEADER_BYTES);
      const nonce = payload.subarray(4, 16);
      const ciphertext = payload.subarray(HEADER_BYTES);
      let data;
      if (root.crypto && root.crypto.subtle && typeof root.crypto.subtle.decrypt === 'function') {
        // Authentication errors are terminal: never retry a failed native
        // decrypt with a fallback or display ciphertext as an image.
        const imported = await root.crypto.subtle.importKey('raw', key, 'AES-GCM', false, ['decrypt']);
        data = new Uint8Array(await root.crypto.subtle.decrypt(
          { name: 'AES-GCM', iv: nonce, additionalData, tagLength: 128 }, imported, ciphertext));
      } else {
        // LAN HTTP lacks WebCrypto on many browsers. This pinned local library
        // is a compatibility fallback; it does not secure HTTP transport.
        if (!root.GravelleNobleGCM || typeof root.GravelleNobleGCM.gcm !== 'function') throw invalid();
        data = root.GravelleNobleGCM.gcm(key, nonce, additionalData).decrypt(ciphertext);
      }
      return { data, mime };
    } catch (_) {
      throw invalid();
    } finally {
      if (key) key.fill(0);
    }
  }

  root.GravelleShareCrypto = Object.freeze({ decrypt });
}(typeof window !== 'undefined' ? window : globalThis));

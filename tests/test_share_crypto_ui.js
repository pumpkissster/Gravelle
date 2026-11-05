'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const zlib = require('node:zlib');

const root = path.resolve(__dirname, '..');
const fallback = fs.readFileSync(path.join(root, 'static/vendor/noble-gcm.js'), 'utf8');
const wrapper = fs.readFileSync(path.join(root, 'static/share-crypto.js'), 'utf8');
const vector = JSON.parse(fs.readFileSync(path.join(__dirname, 'share_crypto_vectors.json'), 'utf8'));
const key = Buffer.from(vector.key, 'base64url');
const mimeByCode = { 1: 'image/jpeg', 2: 'image/png', 3: 'image/webp', 4: 'image/gif', 5: 'image/avif', 6: 'image/bmp', 7: 'image/tiff' };

function runtime(native, withFallback = true) {
  const scope = vm.createContext({ Uint8Array, ArrayBuffer, TextEncoder, atob, btoa,
    crypto: native === true ? crypto.webcrypto : native || undefined });
  if (withFallback) vm.runInContext(fallback, scope);
  vm.runInContext(wrapper, scope);
  return scope;
}

function encrypt(data, code, aad = vector.aad, useKey = key) {
  const nonce = crypto.randomBytes(12);
  const header = Buffer.concat([Buffer.from('GSH1'), nonce, Buffer.from([code])]);
  const cipher = crypto.createCipheriv('aes-256-gcm', useKey, nonce);
  cipher.setAAD(Buffer.concat([header, Buffer.from(aad)]));
  return Uint8Array.from(Buffer.concat([header, cipher.update(data), cipher.final(), cipher.getAuthTag()]));
}

function largePng() {
  const table = Uint32Array.from({ length: 256 }, (_, byte) => {
    let value = byte;
    for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    return value >>> 0;
  });
  function chunk(kind, data) {
    const result = Buffer.allocUnsafe(data.length + 12);
    result.writeUInt32BE(data.length); result.write(kind, 4, 4, 'ascii'); data.copy(result, 8);
    let checksum = 0xffffffff;
    for (let index = 4; index < result.length - 4; index++) checksum = table[(checksum ^ result[index]) & 255] ^ (checksum >>> 8);
    result.writeUInt32BE((checksum ^ 0xffffffff) >>> 0, result.length - 4);
    return result;
  }
  const header = Buffer.alloc(13); header.writeUInt32BE(1); header.writeUInt32BE(1, 4); header[8] = 8; header[9] = 2;
  return Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), chunk('IHDR', header),
    chunk('paDd', Buffer.alloc(64 * 1024 * 1024 + 1, 0x5a)),
    chunk('IDAT', zlib.deflateSync(Buffer.from([0, 0xc0, 0x1f, 0x5d]))), chunk('IEND', Buffer.alloc(0))]);
}

for (const native of [true, false]) {
  const label = native ? 'native WebCrypto' : 'local HTTP fallback';
  test(`${label} decrypts the exact Python-generated envelope`, async () => {
    const scope = runtime(native);
    const packet = Uint8Array.from(Buffer.from(vector.packet_hex, 'hex'));
    const result = await scope.GravelleShareCrypto.decrypt(packet.buffer, vector.key, vector.aad);
    assert.equal(Buffer.from(result.data).toString('hex'), vector.data_hex);
    assert.equal(result.mime, vector.mime);
    assert.equal(Object.isFrozen(scope.GravelleShareCrypto), true);
  });

  test(`${label} interoperates with Node/OpenSSL for all image types and unaligned views`, async () => {
    const api = runtime(native).GravelleShareCrypto;
    for (const [code, mime] of Object.entries(mimeByCode)) {
      const data = crypto.randomBytes(1025 + Number(code));
      const packet = encrypt(data, Number(code));
      const padded = new Uint8Array(packet.length + 5);
      padded.set(packet, 3);
      const result = await api.decrypt(padded.subarray(3, -2), vector.key, new TextEncoder().encode(vector.aad));
      assert.equal(result.mime, mime);
      assert.deepEqual(Buffer.from(result.data), data);
      assert.deepEqual(packet, padded.subarray(3, -2), 'decrypt must not modify the response buffer');
    }
  });

  test(`${label} rejects wrong key, wrong page, wrong share and any modified header/body/tag`, async () => {
    const api = runtime(native).GravelleShareCrypto;
    const packet = encrypt(Buffer.from('private photo'), 2);
    for (const index of [0, 4, 15, 16, 17, packet.length - 1]) {
      const damaged = packet.slice();
      damaged[index] ^= 1;
      await assert.rejects(api.decrypt(damaged, vector.key, vector.aad), /无法解密/);
    }
    for (const context of ['', vector.aad + '4', vector.aad.replace('samplePublicToken', 'anotherPublicToken')]) {
      await assert.rejects(api.decrypt(packet, vector.key, context), /无法解密/);
    }
    await assert.rejects(api.decrypt(packet, crypto.randomBytes(32).toString('base64url'), vector.aad), /无法解密/);
    await assert.rejects(api.decrypt(packet.subarray(0, -1), vector.key, vector.aad), /无法解密/);
    await assert.rejects(api.decrypt(packet.subarray(0, 33), vector.key, vector.aad), /无法解密/);
  });

  test(`${label} rejects malformed base64 keys, unsafe MIME codes and invalid contexts`, async () => {
    const api = runtime(native).GravelleShareCrypto;
    const packet = encrypt(Buffer.from('private photo'), 1);
    for (const value of [undefined, '', 'a'.repeat(42), 'a'.repeat(44), vector.key + '=', vector.key.slice(0, -1) + '9', []]) {
      await assert.rejects(api.decrypt(packet, value, vector.aad), /无法解密/);
    }
    for (const code of [0, 8, 99, 255]) {
      await assert.rejects(api.decrypt(encrypt(Buffer.from('<html>'), code), vector.key, vector.aad), /无法解密/);
    }
    for (const context of [null, {}, 42, 'x'.repeat(4097)]) {
      await assert.rejects(api.decrypt(packet, vector.key, context), /无法解密/);
    }
  });

  test(`${label} decrypts a PNG above 64 MiB and still rejects a modified authentication tag`, async () => {
    const api = runtime(native).GravelleShareCrypto;
    const data = largePng();
    assert.ok(data.length > 64 * 1024 * 1024);
    const expected = crypto.createHash('sha256').update(data).digest('hex');
    const packet = encrypt(data, 2);
    const result = await api.decrypt(packet, vector.key, vector.aad);
    assert.equal(result.mime, 'image/png'); assert.equal(result.data.length, data.length);
    assert.equal(crypto.createHash('sha256').update(result.data).digest('hex'), expected);
    packet[packet.length - 1] ^= 1;
    await assert.rejects(api.decrypt(packet, vector.key, vector.aad), /无法解密/);
  });
}

test('native authentication errors never fall back or expose bytes', async () => {
  const scope = runtime({ subtle: { importKey: async () => ({}), decrypt: async () => { throw new Error('bad tag'); } } });
  let fallbacks = 0;
  scope.GravelleNobleGCM = { gcm: () => { fallbacks++; return { decrypt: () => new Uint8Array([1]) }; } };
  await assert.rejects(scope.GravelleShareCrypto.decrypt(encrypt(Buffer.from('photo'), 1), vector.key, vector.aad), /无法解密/);
  assert.equal(fallbacks, 0);
});

test('HTTP with a missing local crypto bundle fails closed', async () => {
  const api = runtime(false, false).GravelleShareCrypto;
  await assert.rejects(api.decrypt(encrypt(Buffer.from('photo'), 1), vector.key, vector.aad), /无法解密/);
});

test('empty and truncated image envelopes are rejected before decryption', async () => {
  let decryptions = 0;
  const api = runtime({ subtle: { importKey: async () => ({}), decrypt: async () => { decryptions++; return new ArrayBuffer(0); } } }).GravelleShareCrypto;
  for (const length of [0, 1, 16, 17, 32, 33]) {
    await assert.rejects(api.decrypt(new Uint8Array(length), vector.key, vector.aad), /无法解密/);
  }
  assert.equal(decryptions, 0);
});

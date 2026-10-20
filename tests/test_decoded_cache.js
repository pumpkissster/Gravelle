'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createCache, decodedImageBytes, normalize } = require('../static/performance-settings.js');
const MiB = 1024 * 1024;

test('decoded estimates use pixel dimensions rather than compressed file size', () => {
  assert.equal(decodedImageBytes({ width: 9903, height: 6959 }), 9903 * 6959 * 4);
  assert.equal(decodedImageBytes({ width: 1920, height: 1080 }), 8294400);
  assert.equal(decodedImageBytes({ width: 0, height: 1080 }), 16 * MiB);
  assert.equal(decodedImageBytes(null, 123), 123);
  assert.equal(normalize({ imageCacheMemoryMB: 0 }).imageCacheMemoryMB, 0);
  assert.equal(normalize({ imageCacheMemoryMB: 16385 }).imageCacheMemoryMB, 256);
  assert.equal(normalize({ imageCacheMemoryMB: '512' }).imageCacheMemoryMB, 256);
});

test('decoded memory budget evicts least recently used pages and releases them once', () => {
  const disposed = []; const cache = createCache(0, (_entry, page) => disposed.push(page), { byteLimit: 100 });
  cache.set(1, { bytes: 40 }); cache.set(2, { bytes: 40 }); cache.get(1); cache.set(3, { bytes: 40 });
  cache.trim([3]); assert.deepEqual(cache.keys(), [1, 3]); assert.deepEqual(disposed, [2]);
  assert.equal(cache.bytes, 80); assert.equal(cache.availableBytes, 20);
  cache.clear(); assert.deepEqual(disposed, [2, 1, 3]); assert.equal(cache.bytes, 0);
});

test('currently displayed oversized spread survives the budget and disables spare capacity', () => {
  const cache = createCache(0, () => {}, { byteLimit: 256 * MiB });
  cache.set(0, { bytes: 263 * MiB }); cache.set(1, { bytes: 20 * MiB }); cache.trim([0]);
  assert.deepEqual(cache.keys(), [0]); assert.equal(cache.availableBytes, 0);
  cache.set(2, { bytes: 280 * MiB }); cache.setLimits(1, 256 * MiB, [0, 2]);
  assert.deepEqual(cache.keys(), [0, 2], 'a displayed dual page spread is protected even above both limits');
  cache.setLimits(1, 256 * MiB, [2]); assert.deepEqual(cache.keys(), [2]);
});

test('updated decoded size, lower budgets and count limits all participate in eviction', () => {
  const cache = createCache(0, () => {}, { byteLimit: 100 }); const pending = { bytes: 10 };
  cache.set(1, { bytes: 30 }); cache.set(2, pending); pending.bytes = 90; cache.trim([2]);
  assert.deepEqual(cache.keys(), [2]);
  cache.setLimits(2, 0, [2]); cache.set(3, { bytes: 500 }); cache.set(4, { bytes: 500 }); cache.trim([4]);
  assert.deepEqual(cache.keys(), [3, 4]);
  cache.setLimits(0, 0); assert.equal(cache.availableBytes, Infinity);
  cache.setByteLimit(200, [4]); assert.deepEqual(cache.keys(), [4]);
});

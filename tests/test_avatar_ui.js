'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Avatars = require('../static/avatar-ui.js');

test('avatar uploads accept JPEG, PNG and WebP up to 6 MiB and reject empty or incompatible files', () => {
  ['image/jpeg', 'image/png', 'image/webp'].forEach(type => {
    assert.equal(Avatars.validateFile({ type, size: 1 }).valid, true);
    assert.equal(Avatars.validateFile({ type, size: Avatars.MAX_FILE_SIZE }).valid, true);
    assert.equal(Avatars.validateFile({ type, size: Avatars.MAX_FILE_SIZE + 1 }).valid, false);
  });
  [null, {}, { type: 'image/png', size: 0 }, { type: 'image/svg+xml', size: 100 }, { type: 'text/plain', size: 100 }].forEach(file => {
    assert.equal(Avatars.validateFile(file).valid, false);
  });
});

test('avatar sources preserve server cache revisions and fall back to a model cover or the supplied default', () => {
  assert.equal(Avatars.source({ avatar_url: '/api/profile/avatar?v=3', cover_url: '/api/books/1/cover' }), '/api/profile/avatar?v=3');
  assert.equal(Avatars.source({ cover_url: '/api/books/1/cover' }), '/api/books/1/cover');
  assert.equal(Avatars.source(null), '/default-avatar.png');
  assert.equal(Avatars.source({ avatar_url: 'javascript:alert(1)' }), '/default-avatar.png');
  assert.equal(Avatars.source({ avatar_url: '//external.example/avatar.png' }, '/api/books/2/cover'), '/api/books/2/cover');
});

test('changing an avatar selection can cancel an unfinished read without accepting stale results', async () => {
  const previousReader = global.FileReader;
  let active;
  global.FileReader = class {
    constructor() { active = this; this.readyState = 0; }
    readAsDataURL() { this.readyState = 1; }
    abort() { this.readyState = 2; this.wasAborted = true; this.onabort?.(); }
  };
  try {
    const controller = new AbortController();
    const result = Avatars.readFile({ type: 'image/png', size: 10 }, { signal: controller.signal });
    controller.abort();
    await assert.rejects(result, error => error.name === 'AbortError');
    assert.equal(active.wasAborted, true);
    assert.equal(active.onload, null);
  } finally { global.FileReader = previousReader; }
});

test('a selected avatar resolves to a reusable data URL and invalid reads reject before upload', async () => {
  const previousReader = global.FileReader;
  global.FileReader = class {
    readAsDataURL() { this.result = 'data:image/webp;base64,UklGRg=='; queueMicrotask(() => this.onload()); }
  };
  try {
    assert.equal(await Avatars.readFile({ type: 'image/webp', size: 10 }), 'data:image/webp;base64,UklGRg==');
    await assert.rejects(Avatars.readFile({ type: 'image/gif', size: 10 }), /JPG、PNG 或 WebP/);
  } finally { global.FileReader = previousReader; }
});

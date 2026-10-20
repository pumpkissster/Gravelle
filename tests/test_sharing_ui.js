'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Management = require('../static/sharing-ui.js');
const Reader = require('../static/share-reader.js');
const token = 'S'.repeat(43), key = 'K'.repeat(43);
const access = { manifest: { title: '<Photo title>', format: 'folder', page_count: 3 }, key };
const response = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, async json() { return value; }, async arrayBuffer() { return Uint8Array.of(1, 2, 3).buffer; } });
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
class Element {
  constructor(name = 'div') { this.tagName = name.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.handlers = new Map(); this.hidden = false; this.disabled = false; this.value = ''; this.textContent = ''; this.className = ''; this.classList = { add: name => { this.className += ' ' + name; } }; }
  append(...nodes) { nodes.forEach(node => { node.parentElement = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(type, callback) { this.handlers.set(type, callback); }
  emit(type, extra = {}) { return this.handlers.get(type)?.({ preventDefault() {}, ...extra }); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'src') this.src = ''; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  reportValidity() { return true; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}
function nodesIn(node) { return [node, ...node.children.flatMap(nodesIn)]; }
function fixture({ locked = false, responder, decode, storage = new Map() } = {}) {
  const nodes = new Map(); const body = new Element('body'); const doc = { body, hidden: false, title: '', activeElement: null, handlers: new Map(), createElement: name => new Element(name), createElementNS: (namespace, name) => new Element(name), getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element(id.includes('form') ? 'form' : id.includes('password') || id.includes('number') ? 'input' : 'div')); return nodes.get(id); }, addEventListener(type, callback) { this.handlers.set(type, callback); } };
  const requests = [], decryption = [], created = [], revoked = [], written = [], timers = new Map(); let timerIndex = 0;
  const win = { location: { pathname: '/share/' + token, href: 'http://192.168.1.2:7878/share/' + token }, navigator: {}, AbortController, Blob, URL: { createObjectURL(blob) { const url = 'blob:private-' + (created.length + 1); created.push({ url, blob }); return url; }, revokeObjectURL(url) { revoked.push(url); } }, sessionStorage: { getItem(name) { return storage.get(name) ?? null; }, setItem(name, value) { storage.set(name, value); written.push({ name, value }); }, removeItem(name) { storage.delete(name); } }, setInterval(fn) { timers.set(++timerIndex, fn); return timerIndex; }, clearInterval(id) { timers.delete(id); }, setTimeout(fn) { timers.set(++timerIndex, fn); return timerIndex; }, clearTimeout(id) { timers.delete(id); }, addEventListener(type, callback) { this[type] = callback; } };
  const fetch = async (url, options) => { requests.push({ url, options }); if (responder) { const result = await responder(url, options); if (result) return result; } if (url.endsWith('/manifest')) return locked ? response({}, 401) : response(access); if (url.endsWith('/unlock')) return response(access); if (url.endsWith('/logout')) return response({ logged_out: true }); if (/\/pages\/\d+$/.test(url)) return response({}); return response({ status: 'active', requires_password: true }); };
  const crypto = { async decrypt(packet, secret, aad) { decryption.push({ packet, secret, aad }); return decode ? decode(packet, secret, aad) : { data: Uint8Array.of(255, 216, 255), mime: 'image/jpeg' }; } };
  const reader = Reader.create({ document: doc, window: win, fetch, crypto });
  return { reader, doc, win, nodes, requests, decryption, created, revoked, written, timers, get: id => doc.getElementById(id) };
}

test('share creation allows only 1, 3 or 7 days and always requires an access password at the server', () => {
  assert.deepEqual(Management.payload({ id: 'book' }, {}), { book_id: 'book', mode: 'time', days: 1 });
  assert.deepEqual(Management.payload({ id: 'book' }, { duration_days: '7', password: 'pass-1234' }), { book_id: 'book', mode: 'time', days: 7, password: 'pass-1234' });
  assert.deepEqual(Management.payload({ id: 'book' }, { duration_days: 3 }), { book_id: 'book', mode: 'time', days: 3 });
  assert.throws(() => Management.payload({ id: 'book' }, { kind: 'visits', max_visits: 1 }));
  for (const count of [0, 11, 1.5, '', 'x']) assert.throws(() => Management.payload({ id: 'book' }, { kind: 'visits', max_visits: count }));
  for (const duration_days of [0, 2, 14, '']) assert.throws(() => Management.payload({ id: 'book' }, { duration_days }));
  for (const password of ['short', 'p'.repeat(65)]) assert.throws(() => Management.payload({ id: 'book' }, { password }));
  assert.equal(Management.payload({ id: 'book', format: 'epub' }, {}).book_id, 'book');
});

test('copied share URLs remain on the current deployment origin and reject unsafe values', () => {
  assert.equal(Management.shareUrl({ token }, 'http://192.168.0.8:7878/#/home'), 'http://192.168.0.8:7878/share/' + token);
  assert.equal(Management.shareUrl({ url: '/share/' + token }, 'https://photos.example/'), 'https://photos.example/share/' + token);
  for (const url of ['javascript:alert(1)', 'https://other.example/share/' + token, '/share/' + token + '?password=secret', '/api/books/private/cover']) assert.throws(() => Management.shareUrl({ url }, 'http://localhost:8080/'));
});

test('management status distinguishes expiry and closed links without showing old visit counts', () => {
  assert.equal(Management.status({ status: 'closed' }), '已关闭');
  assert.equal(Management.status({ status: 'active', expires_at: 1 }), '已到期');
  assert.equal(Management.status({ status: 'active', expires_at: Date.now()/1000+86400 }), '分享中');
  assert.match(Management.rule({ expires_at: Date.now()/1000+86400 }), /^有效至/);
  assert.equal(Management.rule({}), '—');
  assert.equal(Management.status({ status: 'closed', max_visits: 1, visits: 1 }), '已关闭');
  assert.equal(Management.rule({ max_visits: 10, visits: 7 }), '—');
  assert.notEqual(Management.date(1720000000), '—');
});

test('anonymous path and manifest validation reject lookalike routes and unsupported content', () => {
  assert.equal(Reader.tokenFromPath('/share/' + token), token);
  for (const path of ['/share/' + token + '/extra', '/share/short', '/share/' + token + '%2f', '/api/share/' + token]) assert.equal(Reader.tokenFromPath(path), null);
  assert.equal(Reader.validManifest(access), true);
  assert.equal(Reader.validManifest({ ...access, key: 'short' }), false);
  assert.equal(Reader.validManifest({ ...access, manifest: { ...access.manifest, page_count: 0 } }), false);
  assert.equal(Reader.validManifest({ ...access, manifest: { ...access.manifest, format: 'epub' } }), true);
  assert.equal(Reader.validManifest({ ...access, manifest: { ...access.manifest, format: 'unknown' } }), false);
});

test('anonymous reader unlocks without an account and binds ciphertext to this share and exact page', async () => {
  const f = fixture({ locked: true }); await f.reader.start();
  assert.equal(f.get('share-unlock-form').hidden, false); assert.equal(f.get('share-reader').hidden, true);
  assert.equal(f.get('share-gate-message').textContent, ''); assert.equal(f.get('share-gate-message').hidden, true);
  f.get('share-password').value = 'private-password'; await f.get('share-unlock-form').emit('submit'); await tick();
  const unlock = f.requests.find(item => item.url.endsWith('/unlock'));
  assert.deepEqual(JSON.parse(unlock.options.body), { password: 'private-password' });
  assert.equal(f.get('share-password').value, ''); assert.equal(f.get('share-reader').hidden, false);
  assert.equal(f.decryption[0].aad, 'gravelle-share-v1:' + token + ':0');
  assert.equal(f.decryption[0].secret, key); assert.equal(f.created[0].blob.type, 'image/jpeg');
  const img = f.get('share-image-host').children[0]; assert.equal(img.src, 'blob:private-1'); assert.equal(img.alt, '<Photo title> · 第 1 页'); img.onload();
  assert.deepEqual(f.written, [{ name: 'gravelle-share-page:' + token, value: '0' }]);
  assert.ok(f.requests.every(item => item.options.credentials === 'same-origin' && item.options.cache === 'no-store'));
  assert.ok(f.requests.every(item => !item.url.includes('/api/session') && !item.url.includes('/api/books')));
  f.reader.reset();
});

test('refresh resumes an already authorized visit without consuming another unlock', async () => {
  const f = fixture(); f.win.sessionStorage.getItem = () => '2'; await f.reader.start();
  assert.equal(f.decryption[0].aad, 'gravelle-share-v1:' + token + ':2');
  assert.equal(f.get('share-page-number').value, 3); assert.equal(f.get('share-next').disabled, true);
  assert.ok(!f.requests.some(item => item.url.endsWith('/unlock'))); f.reader.reset();
});

test('page changes revoke the previous blob and do not expose the encrypted endpoint as an image URL', async () => {
  const f = fixture(); await f.reader.start(); f.get('share-image-host').children[0].onload();
  await f.get('share-next').emit('click'); await tick();
  assert.deepEqual(f.revoked, ['blob:private-1']); assert.equal(f.decryption[1].aad, 'gravelle-share-v1:' + token + ':1');
  assert.equal(f.get('share-image-host').children[0].src, 'blob:private-2'); f.reader.reset();
  assert.deepEqual(f.revoked, ['blob:private-1', 'blob:private-2']); assert.equal(f.timers.size, 0);
});

test('closed or expired shares immediately discard the key and displayed blob on the next validation', async () => {
  let revoked = false; const f = fixture({ responder(url) { if (revoked && url.endsWith('/manifest')) return response({ status: 'closed' }, 410); } });
  await f.reader.start(); const img = f.get('share-image-host').children[0]; img.onload(); revoked = true;
  await f.reader.checkSession(); assert.equal(f.get('share-reader').hidden, true); assert.equal(f.get('share-gate').hidden, false); assert.equal(f.get('share-gate-message').textContent, Reader.messages.closed);
  assert.equal(img.src, ''); assert.equal(f.get('share-image-host').children.length, 0); assert.equal(f.revoked.length, 1); assert.equal(f.timers.size, 0);
  const calls = f.requests.length; await f.get('share-next').emit('click'); assert.equal(f.requests.length, calls);
});

test('revocation during asynchronous decryption cannot recreate a visible image or blob', async () => {
  let finish; let revoke = false; const pending = new Promise(resolve => { finish = resolve; });
  const f = fixture({ decode() { return pending; }, responder(url) { if (revoke && url.endsWith('/manifest')) return response({ status: 'expired' }, 410); } });
  const starting = f.reader.start(); await tick(); assert.equal(f.decryption.length, 1); revoke = true;
  await f.reader.checkSession(); finish({ data: Uint8Array.of(1), mime: 'image/png' }); await starting;
  assert.equal(f.created.length, 0); assert.equal(f.get('share-image-host').children.length, 0); assert.equal(f.get('share-reader').hidden, true);
});

test('authentication expiration clears media and asks for a password; network loss also hides media', async () => {
  for (const fail of [401, 'network']) {
    let broken = false; const f = fixture({ responder(url) { if (broken && url.endsWith('/manifest')) { if (fail === 'network') throw new Error('offline'); return response({}, fail); } } });
    await f.reader.start(); broken = true; await f.reader.checkSession();
    assert.equal(f.get('share-reader').hidden, true); assert.equal(f.created.length, f.revoked.length);
    assert.equal(f.get('share-unlock-form').hidden, fail !== 401); f.reader.reset();
  }
});

test('password throttling keeps the form available and does not keep the submitted password', async () => {
  const f = fixture({ locked: true, responder(url) { if (url.endsWith('/unlock')) return response({}, 429); } });
  await f.reader.start(); f.get('share-password').value = 'private-password'; await f.get('share-unlock-form').emit('submit');
  assert.equal(f.get('share-password').value, ''); assert.equal(f.get('share-unlock').disabled, false); assert.equal(f.get('share-password-error').hidden, false); assert.match(f.get('share-password-error').textContent, /稍后/); f.reader.reset();
});

test('incorrect access passwords remain retryable and an exhausted link displays its server explanation', async () => {
  let wrong = true; const f = fixture({ locked: true, responder(url) { if (url.endsWith('/unlock') && wrong) return response({ error: '访问密码不正确。' }, 403); } });
  await f.reader.start(); f.get('share-password').value = 'wrong-password'; await f.get('share-unlock-form').emit('submit');
  assert.equal(f.get('share-unlock-form').hidden, false); assert.equal(f.get('share-password-error').textContent, '访问密码不正确。');
  wrong = false; f.get('share-password').value = 'correct-password'; await f.get('share-unlock-form').emit('submit'); assert.equal(f.get('share-reader').hidden, false); f.reader.reset();
  const ended = fixture({ responder(url) { if (url.endsWith('/manifest')) return response({ error: '分享的访问次数已用完。' }, 410); } });
  await ended.reader.start(); assert.equal(ended.get('share-gate-message').textContent, '分享的访问次数已用完。'); assert.equal(ended.get('share-unlock-form').hidden, true);
});

test('admin-only UI hides operations and discards stale responses on account reset', async () => {
  const f = fixture(); let administrator = false, finish; const pending = new Promise(resolve => { finish = resolve; }); const requests = [];
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => administrator, api: async path => { requests.push(path); return pending; }, post: async () => ({}) });
  const dialog = f.doc.body.children[0]; const host = f.doc.body.children.find(node => node.tagName === 'SECTION'); ui.showManagement(); assert.notEqual(dialog.open, true); assert.equal(requests.length, 0); assert.equal(host.hidden, true);
  administrator = true; ui.showManagement(); assert.notEqual(dialog.open, true); assert.equal(host.hidden, false); assert.equal(requests.length, 1); ui.reset(); administrator = false;
  finish({ shares: [{ id: 1, token, title: 'Private title', status: 'active', created_at: 1720000000, max_visits: 1, visits: 0 }] }); await tick();
  assert.notEqual(dialog.open, true); assert.equal(host.hidden, true); assert.ok(!nodesIn(host).some(node => node.textContent === 'Private title'));
});

test('administrator creation sends the chosen rule, shows one-time password, and clears it when dismissed', async () => {
  const f = fixture(); const posts = [], copied = [];
  f.win.navigator.clipboard = { async writeText(value) { copied.push(value); } };
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares: [] }), post: async (path, data) => { posts.push({ path, data }); return { share: { token, status: 'active', expires_at: 2100000000 }, password: 'random-pass-1' }; } });
  ui.showCreate({ id: 'book-id', title: '<Work title>' });
  const dialog = f.doc.body.children[0]; const all = () => nodesIn(dialog);
  const choices=all().filter(node=>node.getAttribute('role')==='radio');
  assert.deepEqual(choices.map(node=>node.textContent),['1 天','3 天','7 天']);
  choices[0].emit('keydown',{key:'ArrowRight'});
  assert.equal(choices[1].getAttribute('aria-checked'),'true');
  assert.ok(!all().some(node=>node.tagName==='SELECT'));
  await all().find(node => node.tagName === 'FORM').emit('submit');
  assert.deepEqual(posts, [{ path: '/api/shares', data: { book_id: 'book-id', mode: 'time', days: 3 } }]);
  const secret = all().find(node => node.getAttribute('aria-label') === '分享访问密码'); assert.equal(secret.value, 'random-pass-1');
  await all().find(node => node.textContent === '复制链接和密码').emit('click'); assert.equal(copied[0], '分享链接：http://192.168.1.2:7878/share/' + token + '\n访问密码：random-pass-1');
  all().find(node => node.getAttribute('aria-label') === '关闭').emit('click'); assert.equal(secret.value, ''); assert.equal(dialog.open, false); ui.reset();
});

test('management is a secondary page and confirms revocation in a separate dialog', async () => {
  const f = fixture(); const posts = [];
  const share = { id: 'a'.repeat(32), token, book_id: 'book', title: 'Time-limited work', status: 'active', expires_at: Date.now()/1000+86400, created_at: 1720000000 };
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares: [share] }), post: async (path, data) => { posts.push({ path, data }); share.status = 'closed'; return { share }; } });
  try {
    ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION');
    const card = nodesIn(host).find(node => node.className === 'sharing-card');
    assert.ok(nodesIn(card).some(node => node.textContent === '分享中'));
    const close = nodesIn(card).find(node => node.tagName === 'BUTTON' && node.textContent === '关闭分享'); assert.ok(close);
    close.emit('click'); const confirm = f.doc.body.children.find(node => node.className.includes('sharing-confirm-dialog')); assert.equal(confirm.open, true);
    // A refresh while confirming must retain the pending selection.
    await nodesIn(host).find(node => node.tagName === 'BUTTON' && node.textContent === '刷新').emit('click');
    assert.equal(confirm.open, true);
    await nodesIn(confirm).find(node => node.tagName === 'BUTTON' && node.textContent === '关闭分享').emit('click');
    assert.deepEqual(posts, [{ path: '/api/shares/' + share.id + '/close', data: {} }]);
    assert.equal(confirm.open, false);
    const closedCard = nodesIn(host).find(node => node.className === 'sharing-card');
    assert.ok(!nodesIn(closedCard).some(node => node.tagName === 'BUTTON' && node.textContent === '关闭分享'));
  } finally { ui.reset(); }
});

test('closed records require a separate deletion confirmation and re-sharing creates a new link without reopening the old one', async () => {
  const f = fixture(), posts = []; let loads = 0;
  const host = new Element('section'); f.doc.body.append(host);
  const old = { id: 'b'.repeat(32), book_id: 'original-book', title: 'Closed work', token, status: 'closed', max_visits: 1, visits: 1, created_at: 1720000000 };
  const shares = [old]; const newToken = 'N'.repeat(43);
  const ui = Management.create({ document: f.doc, window: f.win, managementHost: host, isAdmin: () => true,
    api: async () => { loads++; return { shares: [...shares] }; },
    post: async (path, data) => { posts.push({ path, data }); if (path === '/api/shares') { const share = { ...old, id: 'c'.repeat(32), token: newToken, status: 'active', expires_at: 2100000000, max_visits: null }; shares.push(share); return { share, password: 'new-access-password' }; } shares.splice(shares.indexOf(old), 1); return {}; }
  });
  try {
    ui.showManagement(); ui.showManagement(); await tick(); assert.equal(loads, 1); assert.equal(host.hidden, false);
    const createDialog = f.doc.body.children.find(node => node.tagName === 'DIALOG' && node.className === 'sharing-dialog');
    const confirm = f.doc.body.children.find(node => node.className.includes('sharing-confirm-dialog'));
    assert.notEqual(createDialog.open, true); assert.notEqual(confirm.open, true);
    let card = nodesIn(host).find(node => node.className === 'sharing-card');
    assert.ok(nodesIn(card).some(node => node.textContent === '已关闭'));
    assert.ok(!nodesIn(card).some(node => /剩余|次访问/.test(node.textContent)));
    nodesIn(card).find(node => node.textContent === '重新分享').emit('click'); assert.equal(createDialog.open, true);
    const radios = nodesIn(createDialog).filter(node => node.getAttribute('role') === 'radio');
    assert.equal(radios[0].tabIndex, 0); assert.equal(radios[1].tabIndex, -1);
    radios[0].emit('keydown', { key: 'End' }); assert.equal(radios[2].getAttribute('aria-checked'), 'true'); assert.equal(radios[2].tabIndex, 0); assert.equal(radios[0].tabIndex, -1);
    radios[2].emit('keydown', { key: 'Home' }); assert.equal(radios[0].getAttribute('aria-checked'), 'true');
    radios[0].emit('keydown', { key: 'ArrowLeft' }); assert.equal(radios[2].getAttribute('aria-checked'), 'true');
    await nodesIn(createDialog).find(node => node.tagName === 'FORM').emit('submit');
    assert.deepEqual(posts[0], { path: '/api/shares', data: { book_id: 'original-book', mode: 'time', days: 7 } });
    assert.equal(old.status, 'closed'); assert.equal(old.token, token); assert.equal(shares.length, 2); assert.equal(loads, 2);
    assert.equal(nodesIn(createDialog).find(node => node.getAttribute('aria-label') === '分享链接').value, 'http://192.168.1.2:7878/share/' + newToken);
    nodesIn(createDialog).find(node => node.getAttribute('aria-label') === '关闭').emit('click');
    card = nodesIn(host).find(node => node.className === 'sharing-card');
    nodesIn(card).find(node => node.textContent === '删除记录').emit('click'); assert.equal(confirm.open, true); assert.equal(posts.length, 1);
    assert.ok(nodesIn(confirm).some(node => node.textContent.includes('仅删除分享记录，作品不受影响')));
    nodesIn(confirm).find(node => node.tagName === 'BUTTON' && node.textContent === '取消').emit('click'); assert.equal(confirm.open, false); assert.equal(posts.length, 1);
    nodesIn(card).find(node => node.textContent === '删除记录').emit('click');
    await nodesIn(confirm).find(node => node.tagName === 'BUTTON' && node.textContent === '删除记录').emit('click');
    assert.deepEqual(posts[1], { path: '/api/shares/' + old.id + '/delete', data: {} }); assert.equal(shares.length, 1); assert.equal(shares[0].token, newToken); assert.equal(confirm.open, false);
    for (const dialog of [createDialog, confirm]) { const close = nodesIn(dialog).find(node => node.className === 'sharing-close'); assert.equal(close.textContent, ''); assert.ok(nodesIn(close).some(node => node.tagName === 'USE' && node.getAttribute('href') === '#i-close')); }
  } finally { ui.reset(); }
});

test('leaving management closes its re-share dialog and ignores late responses while standalone sharing stays independent', async () => {
  const f = fixture(); let finish; const posted = new Promise(resolve => { finish = resolve; });
  const old = { id: 'd'.repeat(32), book_id: 'book', title: 'Work', token, status: 'closed', created_at: 1720000000 };
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares: [old] }), post: async () => posted });
  try {
    const dialog = f.doc.body.children.find(node => node.tagName === 'DIALOG' && node.className === 'sharing-dialog');
    const host = f.doc.body.children.find(node => node.tagName === 'SECTION');
    ui.showCreate({ id: 'standalone', title: 'Independent work' }); ui.hideManagement(); assert.equal(dialog.open, true);
    nodesIn(dialog).find(node => node.getAttribute('aria-label') === '关闭').emit('click');
    ui.showManagement(); await tick(); nodesIn(host).find(node => node.textContent === '重新分享').emit('click');
    const submitting = nodesIn(dialog).find(node => node.tagName === 'FORM').emit('submit'); ui.hideManagement(); assert.equal(dialog.open, false); assert.equal(host.hidden, true);
    finish({ share: { token: 'R'.repeat(43), expires_at: 2100000000 }, password: 'must-not-reappear' }); await submitting;
    assert.equal(dialog.open, false); assert.equal(host.hidden, true); assert.equal(nodesIn(dialog).find(node => node.getAttribute('aria-label') === '分享访问密码').value, '');
  } finally { ui.reset(); }
});

test('passwords are fetched only on demand for active or closed records and visit totals never turn into limits', async () => {
  const f = fixture(), posts = [];
  const shares = [
    { id: 'e'.repeat(32), token, book_id: 'book', title: 'Active', status: 'active', visits: 12, password_available: true },
    { id: 'f'.repeat(32), token, book_id: 'book', title: 'Closed', status: 'closed', visits: 3, password_available: true },
    { id: '0'.repeat(32), token, book_id: 'book', title: 'Legacy', status: 'closed', visits: 1, password_available: false }
  ];
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares }), post: async (path, data) => { posts.push({ path, data }); return { password: 'view-password-' + posts.length }; } });
  try {
    ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION');
    const cards = nodesIn(host).filter(node => node.className === 'sharing-card'); assert.equal(posts.length, 0);
    assert.ok(nodesIn(cards[0]).some(node => node.textContent === '访问 12 次'));
    assert.ok(nodesIn(cards[1]).some(node => node.textContent === '访问 3 次'));
    const old = nodesIn(cards[2]).find(node => node.textContent === '原密码不可查看'); assert.equal(old.disabled, true);
    const reveal = nodesIn(cards[0]).find(node => node.textContent === '查看密码'); const field = nodesIn(cards[0]).find(node => node.className === 'sharing-password-value'); const panel = nodesIn(cards[0]).find(node => node.className === 'sharing-password-panel');
    await reveal.emit('click'); assert.deepEqual(posts[0], { path: '/api/shares/' + shares[0].id + '/password', data: {} }); assert.equal(field.value, 'view-password-1'); assert.equal(panel.hidden, false); assert.equal(reveal.textContent, '隐藏密码'); assert.equal(reveal.getAttribute('aria-expanded'), 'true');
    reveal.emit('click'); assert.equal(field.value, ''); assert.equal(panel.hidden, true); assert.equal(reveal.textContent, '查看密码');
    await reveal.emit('click'); assert.equal(posts.length, 2); assert.equal(field.value, 'view-password-2');
    const closedReveal = nodesIn(cards[1]).find(node => node.textContent === '查看密码'); await closedReveal.emit('click'); assert.equal(posts[2].path, '/api/shares/' + shares[1].id + '/password');
    ui.hideManagement(); for (const card of cards.slice(0, 2)) { assert.equal(nodesIn(card).find(node => node.className === 'sharing-password-value').value, ''); assert.equal(nodesIn(card).find(node => node.className === 'sharing-password-panel').hidden, true); }
    assert.ok(!nodesIn(host).some(node => /剩余/.test(node.textContent)));
  } finally { ui.reset(); }
});

test('password requests ignore duplicate clicks and recover after an error without retaining a secret', async () => {
  const f = fixture(); let resolve, reject, calls = 0; const pending = new Promise((done, fail) => { resolve = done; reject = fail; });
  const share = { id: '1'.repeat(32), token, title: 'Work', status: 'active', visits: 0, password_available: true };
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares: [share] }), post: async () => { calls++; return calls === 1 ? pending : { password: 'recovered-password' }; } });
  try {
    ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION');
    const reveal = nodesIn(host).find(node => node.textContent === '查看密码'); const field = nodesIn(host).find(node => node.className === 'sharing-password-value');
    const first = reveal.emit('click'); await reveal.emit('click'); assert.equal(calls, 1); assert.equal(reveal.disabled, true);
    reject(new Error('暂时无法获取密码')); await first; assert.equal(reveal.disabled, false); assert.equal(reveal.textContent, '查看密码'); assert.equal(field.value, '');
    assert.equal(nodesIn(host).find(node => node.className === 'sharing-password-error').textContent, '暂时无法获取密码');
    await reveal.emit('click'); assert.equal(calls, 2); assert.equal(field.value, 'recovered-password'); assert.equal(nodesIn(host).find(node => node.className === 'sharing-password-error').hidden, true);
  } finally { ui.reset(); }
});

test('manual reload clears visible passwords immediately while automatic refresh waits for hidden passwords', async () => {
  const f = fixture(); let loads = 0; const share = { id: '2'.repeat(32), token, title: 'Work', status: 'active', visits: 5, password_available: true };
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => { loads++; return { shares: [share] }; }, post: async () => ({ password: 'visible-password' }) });
  try {
    ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION'); const autoRefresh = [...f.timers.values()][0];
    const reveal = nodesIn(host).find(node => node.textContent === '查看密码'); await reveal.emit('click'); const field = nodesIn(host).find(node => node.className === 'sharing-password-value');
    autoRefresh(); await tick(); assert.equal(loads, 1); assert.equal(field.value, 'visible-password');
    const reload = nodesIn(host).find(node => node.textContent === '刷新').emit('click'); assert.equal(field.value, ''); await reload; assert.equal(loads, 2);
    autoRefresh(); await tick(); assert.equal(loads, 3);
  } finally { ui.reset(); }
});

test('password responses cannot restore secrets after route exit, reload, or account reset', async () => {
  for (const action of ['route', 'reload', 'account']) {
    const f = fixture(); let finish; let administrator = true; const pending = new Promise(resolve => { finish = resolve; });
    const share = { id: '3'.repeat(32), token, title: 'Work', status: 'closed', visits: 0, password_available: true };
    const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => administrator, api: async () => ({ shares: [share] }), post: async () => pending });
    try {
      ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION'); const reveal = nodesIn(host).find(node => node.textContent === '查看密码'); const field = nodesIn(host).find(node => node.className === 'sharing-password-value');
      const fetching = reveal.emit('click');
      if (action === 'route') ui.hideManagement();
      if (action === 'reload') await nodesIn(host).find(node => node.textContent === '刷新').emit('click');
      if (action === 'account') { administrator = false; ui.reset(); }
      finish({ password: 'late-private-password' }); await fetching;
      assert.equal(field.value, ''); assert.ok(!nodesIn(host).some(node => node.value === 'late-private-password'));
    } finally { ui.reset(); }
  }
});

test('exiting deletes the guest session and a fresh page asks for its password again', async () => {
  const storage = new Map(); let authorized = true;
  const responder = url => { if (url.endsWith('/manifest')) return authorized ? response(access) : response({}, 401); if (url.endsWith('/logout')) { authorized = false; return response({ logged_out: true }); } if (url.endsWith('/unlock')) { authorized = true; return response(access); } };
  const f = fixture({ storage, responder }); await f.reader.start();
  const exiting = f.get('share-exit').emit('click');
  assert.equal(f.get('share-image-host').children.length, 0); assert.equal(f.created.length, f.revoked.length); assert.equal(f.timers.size, 0); assert.equal(f.get('share-unlock-form').hidden, true);
  await exiting; assert.equal(f.get('share-unlock-form').hidden, false); assert.equal(f.get('share-reader').hidden, true); assert.equal(f.get('share-gate-message').hidden, true); assert.equal(storage.has('gravelle-share-exit:' + token), false);
  const logout = f.requests.find(item => item.url.endsWith('/logout')); assert.equal(logout.options.method, 'POST'); assert.deepEqual(JSON.parse(logout.options.body), {});
  const refreshed = fixture({ storage, responder }); await refreshed.reader.start(); assert.equal(refreshed.created.length, 0); assert.equal(refreshed.get('share-unlock-form').hidden, false);
  refreshed.get('share-password').value = 'valid-password'; await refreshed.get('share-unlock-form').emit('submit'); assert.equal(refreshed.get('share-reader').hidden, false);
  f.reader.reset(); refreshed.reader.reset();
});

test('failed logout remains visibly retryable and refresh retries logout before any automatic reading', async () => {
  const storage = new Map(); let offline = true;
  const responder = url => { if (url.endsWith('/logout')) { if (offline) throw new Error('offline'); return response({ logged_out: true }); } };
  const f = fixture({ storage, responder }); await f.reader.start(); await f.get('share-exit').emit('click');
  assert.equal(f.get('share-access-title').textContent, '退出未完成'); assert.equal(f.get('share-gate-retry').textContent, '重试退出'); assert.equal(f.get('share-unlock-form').hidden, true); assert.equal(f.get('share-unlock').disabled, true); assert.equal(storage.get('gravelle-share-exit:' + token), '1');
  const refreshed = fixture({ storage, responder }); await refreshed.reader.start(); assert.ok(refreshed.requests.every(item => item.url.endsWith('/logout'))); assert.equal(refreshed.created.length, 0); assert.equal(refreshed.get('share-unlock-form').hidden, true);
  offline = false; await refreshed.get('share-gate-retry').emit('click'); assert.equal(refreshed.get('share-unlock-form').hidden, false); assert.equal(refreshed.get('share-unlock').disabled, false); assert.equal(storage.has('gravelle-share-exit:' + token), false);
  f.reader.reset(); refreshed.reader.reset();
});

test('late manifest responses cannot resume reading during or after logout', async () => {
  let started = false, finish, finishLogout; const pending = new Promise(resolve => { finish = resolve; }); const pendingLogout = new Promise(resolve => { finishLogout = resolve; });
  const f = fixture({ responder(url) { if (started && url.endsWith('/manifest')) return pending; if (url.endsWith('/logout')) return pendingLogout; } }); await f.reader.start(); started = true;
  const checking = f.reader.checkSession(), exiting = f.get('share-exit').emit('click'); finish(response(access)); await checking;
  assert.equal(f.get('share-unlock-form').hidden, true); assert.equal(f.get('share-access-title').textContent, '退出阅读'); assert.equal(f.get('share-reader').hidden, true); assert.equal(f.created.length, f.revoked.length);
  finishLogout(response({ logged_out: true })); await exiting; assert.equal(f.get('share-unlock-form').hidden, false); assert.equal(f.get('share-access-title').textContent, '输入访问密码'); assert.equal(f.get('share-reader').hidden, true); f.reader.reset();
});

test('logout waits for a late unlock cookie then deletes it without displaying the late work', async () => {
  let finish; const pending = new Promise(resolve => { finish = resolve; });
  const f = fixture({ locked: true, responder(url) { if (url.endsWith('/unlock')) return pending; } }); await f.reader.start();
  f.get('share-password').value = 'valid-password'; const unlocking = f.get('share-unlock-form').emit('submit');
  const exiting = f.get('share-exit').emit('click'); await tick(); assert.ok(!f.requests.some(item => item.url.endsWith('/logout'))); assert.equal(f.get('share-unlock').disabled, true);
  finish(response(access)); await unlocking; await exiting;
  assert.equal(f.requests.filter(item => item.url.endsWith('/logout')).length, 1); assert.equal(f.created.length, 0); assert.equal(f.get('share-reader').hidden, true); assert.equal(f.get('share-unlock-form').hidden, false); f.reader.reset();
});

test('back-forward cache restoration during logout cannot bypass the pending exit', async () => {
  let finish; const pending = new Promise(resolve => { finish = resolve; });
  const f = fixture({ responder(url) { if (url.endsWith('/logout')) return pending; } }); await f.reader.start(); const exiting = f.get('share-exit').emit('click');
  f.win.pagehide(); f.win.pageshow({ persisted: true }); await tick(); assert.equal(f.requests.filter(item => item.url.endsWith('/manifest')).length, 1); assert.equal(f.get('share-reader').hidden, true);
  finish(response({ logged_out: true })); await exiting; assert.equal(f.get('share-unlock-form').hidden, false); assert.equal(f.get('share-reader').hidden, true); f.reader.reset();
});

test('visible access passwords copy directly and hidden or discarded fields cannot copy', async () => {
  const f = fixture(), copied = [], messages = []; let posts = 0;
  f.win.navigator.clipboard = { async writeText(value) { copied.push(value); } };
  const share = { id: '4'.repeat(32), token, title: 'Work', status: 'active', password_available: true };
  const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares: [share] }), post: async () => { posts++; return { password: 'copy-this-password' }; }, toast: value => messages.push(value) });
  try {
    ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION'); const reveal = nodesIn(host).find(node => node.textContent === '查看密码'); const copy = nodesIn(host).find(node => node.getAttribute('aria-label') === '复制访问密码');
    assert.equal(copy.disabled, true); await copy.emit('click'); assert.equal(copied.length, 0);
    await reveal.emit('click'); assert.equal(copy.disabled, false); await copy.emit('click'); assert.deepEqual(copied, ['copy-this-password']); assert.equal(posts, 1); assert.equal(messages.at(-1), '密码已复制');
    reveal.emit('click'); assert.equal(copy.disabled, true); await copy.emit('click'); assert.equal(copied.length, 1);
    await reveal.emit('click'); ui.hideManagement(); await copy.emit('click'); assert.equal(copied.length, 1); assert.equal(copy.disabled, true);
  } finally { ui.reset(); }
});

test('password clipboard failure selects the visible field but stale failures do not restore it', async () => {
  for (const hideWhileCopying of [false, true]) {
    const f = fixture(), messages = []; let fail; const pending = new Promise((resolve, reject) => { fail = reject; });
    f.win.navigator.clipboard = { writeText() { return pending; } };
    const share = { id: '5'.repeat(32), token, title: 'Work', status: 'active', password_available: true };
    const ui = Management.create({ document: f.doc, window: f.win, isAdmin: () => true, api: async () => ({ shares: [share] }), post: async () => ({ password: 'selectable-password' }), toast: value => messages.push(value) });
    try {
      ui.showManagement(); await tick(); const host = f.doc.body.children.find(node => node.tagName === 'SECTION'); await nodesIn(host).find(node => node.textContent === '查看密码').emit('click'); const field = nodesIn(host).find(node => node.className === 'sharing-password-value');
      const copying = nodesIn(host).find(node => node.getAttribute('aria-label') === '复制访问密码').emit('click'); if (hideWhileCopying) ui.hideManagement(); fail(new Error('clipboard blocked')); await copying;
      if (hideWhileCopying) { assert.equal(field.value, ''); assert.notEqual(field.selected, true); assert.equal(messages.length, 0); }
      else { assert.equal(field.selected, true); assert.equal(messages.at(-1), '请选中密码后复制'); }
    } finally { ui.reset(); }
  }
});

test('anonymous document has no account app or external scripts, and never stores credentials', () => {
  const html = fs.readFileSync(require.resolve('../static/share.html'), 'utf8'); const js = fs.readFileSync(require.resolve('../static/share-reader.js'), 'utf8'); const css = fs.readFileSync(require.resolve('../static/sharing-ui.css'), 'utf8');
  assert.ok(!html.includes('/app.js')); assert.ok(!html.includes('https://')); assert.match(html, /no-referrer/); assert.ok(html.indexOf('/vendor/noble-gcm.js') < html.indexOf('/share-crypto.js'));
  assert.ok(!html.includes('class="share-eyebrow"')); assert.ok(!js.includes('无需账号，输入分享者提供的密码即可阅读。'));
  assert.ok(!js.includes('localStorage')); assert.match(js, /sessionStorage\.setItem\(storageKey, String\(page\)\)/);
  assert.match(css, /sharing-body\{[^}]*overflow:auto/); assert.match(css, /sharing-footer\{flex:none/); assert.match(css, /sharing-header\{[^}]*flex:none/);
});

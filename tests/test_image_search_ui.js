'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Search = require('../static/image-search-ui.js');
const Routes = require('../static/page-router.js');

test('image search mode survives copied URLs and nested reader return paths', () => {
  const source = { view: 'search', image: true, query: '特集', sort: 'title' };
  assert.deepEqual(Routes.parse(Routes.hash(source)), source);
  const route = { view: 'reader', id: 'book', page: 7, from: source };
  assert.deepEqual(Routes.parse(Routes.hash(route)), route);
  assert.deepEqual(Routes.base(route), source);
  assert.deepEqual(Routes.parse('#/search?type=image&file=private.jpg&path=secret&password=hidden'), { view: 'search', image: true });
  assert.deepEqual(Routes.parse('#/libraries?type=image'), { view: 'libraries' });
  assert.deepEqual(Routes.parse('#/search?type=other'), { view: 'search' });
  const settings = { view: 'settings', section: 'image-search' };
  assert.deepEqual(Routes.parse(Routes.hash(settings)), settings);
});

test('uploads reject empty or oversized data before decoding without trusting file MIME', () => {
  for (const file of [null, {}, { size: NaN }, { size: 0 }, { size: -1 }]) assert.ok(Search.fileError(file));
  assert.equal(Search.fileError({ size: Search.maxBytes, type: '' }), '');
  assert.ok(Search.fileError({ size: Search.maxBytes + 1, type: 'image/png' }));
  assert.equal(Search.imageType(Uint8Array.from([255, 216, 255, 224])), 'image/jpeg');
  assert.equal(Search.imageType(Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10])), 'image/png');
  assert.equal(Search.imageType(Uint8Array.from([82, 73, 70, 70, 8, 0, 0, 0, 87, 69, 66, 80])), 'image/webp');
  for (const bytes of [[], [255, 216], [137, 80, 78, 71], [60, 115, 118, 103], [71, 73, 70, 56]]) assert.equal(Search.imageType(Uint8Array.from(bytes)), '');
});

test('replacing or cancelling a query makes late results unusable', () => {
  const gate = Search.createRequestGate();
  const previous = gate.begin(); assert.equal(previous.current(), true);
  const latest = gate.begin(); assert.equal(previous.signal.aborted, true); assert.equal(previous.current(), false); assert.equal(latest.current(), true);
  gate.cancel(); assert.equal(latest.signal.aborted, true); assert.equal(latest.current(), false); assert.equal(previous.current(), false);
  gate.cancel(); const next = gate.begin(); assert.equal(next.current(), true);
});

test('only valid unique page matches enter the result list and page zero is retained', () => {
  const book = { id: 'book-a', title: '<script>', models: [{ name: '模特' }] };
  const exact = { book, page: 0, match: 'exact' }; const similar = { book, page: 3, match: 'similar', distance: 5 };
  assert.deepEqual(Search.normalizeResults({ results: [exact, { ...exact }, similar, { book, page: -1, match: 'exact' }, { book, page: 1.5, match: 'exact' }, { book, page: 1, match: 'unknown' }, { book: {}, page: 1, match: 'exact' }, null] }), [exact, similar]);
  assert.deepEqual(Search.normalizeResults({ results: 'bad' }), []);
});

test('search availability messages distinguish disabled, partial, paused and empty indexes', () => {
  assert.match(Search.statusMessage({ enabled: false }, false), /管理员/);
  assert.doesNotMatch(Search.statusMessage({ enabled: false }, true), /管理员/);
  assert.match(Search.statusMessage({ enabled: true, available: false, indexing: true }), /正在建立/);
  assert.match(Search.statusMessage({ enabled: true, available: true, indexing: true }), /已收录/);
  assert.match(Search.statusMessage({ enabled: true, available: true, paused: true }), /暂停/);
  assert.match(Search.statusMessage({ enabled: true, available: false }), /暂无/);
  assert.match(Search.statusMessage({ enabled: true, available: true }), /可访问/);
});

test('administrator maintenance stages distinguish catalogue work from completed fingerprints', () => {
  const status = { enabled: true, available: false, indexing: true, progress: { done: 0, total: 12000 }, activity: { stage: 'reconciling', done: 320, total: 12000 } };
  assert.match(Search.statusMessage(status, true), /核对图片：320 \/ 12000/);
  assert.deepEqual(Search.indexProgress(status), { done: 320, total: 12000, percent: 320 / 12000 * 100, label: '图片核对进度' });
  assert.doesNotMatch(Search.statusMessage(status, false), /核对|320/);
  for (const [stage, text] of [['waiting', /等待后台任务/], ['reading', /读取图片/], ['decoding', /图片特征/], ['stopped', /任务已停止/], ['error', /正在重试/]]) {
    status.activity.stage = stage;
    assert.match(Search.statusMessage(status, true), text);
  }
  status.paused = true;
  assert.match(Search.statusMessage(status, true), /已暂停/);
  assert.deepEqual(Search.indexProgress(status), { done: 0, total: 12000, percent: 0, label: '图片索引进度' });
});

test('maintenance progress rejects malformed values and old servers retain index progress', () => {
  for (const [done, total] of [[NaN, Infinity], [-1, -5], ['999', '1000'], [1.5, {}]]) {
    const status = { enabled: true, indexing: true, activity: { stage: 'reconciling', done, total } };
    assert.deepEqual(Search.indexProgress(status), { done: 0, total: 0, percent: 0, label: '图片核对进度' });
    assert.doesNotMatch(Search.statusMessage(status, true), /NaN|Infinity|undefined/);
  }
  assert.deepEqual(Search.indexProgress({ progress: { done: 3, total: 10 } }), { done: 3, total: 10, percent: 30, label: '图片索引进度' });
  assert.equal(Search.indexProgress({ progress: { done: 20, total: 10 } }).percent, 100);
});

test('settings update existing controls while catalogue progress changes', async () => {
  const fixture = browserFixture(); let done = 2;
  const payload = () => ({ enabled: true, indexing: true, available: false, summary: { indexed: 0, pending: 20, failed: 0, total: 20 }, progress: { done: 0, total: 20 }, activity: { stage: 'reconciling', done, total: 20 } });
  const ui = Search.create({ ...fixture, api: async () => payload(), post: async () => ({}), openBook: () => {}, isAdmin: () => true });
  ui.showSettings(); await flush();
  const progress = fixture.find(fixture.settingsHost, item => item.tagName === 'progress');
  const refresh = fixture.find(fixture.settingsHost, item => item.textContent === '刷新');
  assert.equal(progress.value, 10); assert.equal(progress.attributes['aria-label'], '图片核对进度');
  done = 12; await refresh.dispatch('click'); await flush();
  assert.strictEqual(fixture.find(fixture.settingsHost, item => item.tagName === 'progress'), progress);
  assert.strictEqual(fixture.find(fixture.settingsHost, item => item.textContent === '刷新'), refresh);
  assert.equal(progress.value, 60);
  assert.match(fixture.find(fixture.settingsHost, item => item.className === 'image-search-index-counts').textContent, /已索引 0/);
  ui.reset();
});

// A minimal browser surface exercises asynchronous account boundaries without
// depending on a browser, real source images, cookies or a running server.
function browserFixture() {
  class Element {
    constructor(tag, document) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {}; this.hidden = false; this.value = ''; this.textContent = ''; this.className = ''; this.open = false; this.classList = { add: () => {}, remove: () => {}, toggle: () => {} }; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; if (key === 'src') this.src = ''; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    dispatch(type, event = {}) { return Promise.all((this.listeners[type] || []).map(handler => handler({ target: this, ...event }))); }
    close() { this.open = false; }
    showModal() { this.open = true; }
    contains(item) { return this.children.includes(item); }
    closest() { return null; }
    remove() {}
  }
  const document = { listeners: {}, createElement: tag => new Element(tag, document), querySelector: () => null, addEventListener(type, handler) { this.listeners[type] = handler; } };
  document.body = new Element('body', document);
  const searchHost = new Element('section', document); const settingsHost = new Element('section', document);
  function find(root, predicate) { if (predicate(root)) return root; for (const child of root.children) { const result = find(child, predicate); if (result) return result; } return null; }
  return { document, searchHost, settingsHost, find };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function pngFile() { const file = new Blob([Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10, 0])], { type: 'image/png' }); file.name = 'sample.png'; return file; }

test('reset revokes preview and blocks an upload that finishes after switching accounts', async () => {
  const fixture = browserFixture();
  const ui = Search.create({ ...fixture, api: async () => ({ enabled: true, available: true }), post: async () => ({}), openBook: () => {}, isAdmin: () => false });
  ui.showSearch(); await flush();
  const input = fixture.find(fixture.searchHost, item => item.id === 'image-search-file');
  let release; const slow = { size: 10, name: 'slow.png', slice: () => ({ arrayBuffer: () => new Promise(resolve => { release = resolve; }) }) };
  input.files = [slow]; input.dispatch('change'); ui.reset();
  release(Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]).buffer); await flush();
  const preview = fixture.find(fixture.searchHost, item => item.tagName === 'img');
  assert.equal(preview.src || '', '');
  assert.equal(fixture.find(fixture.searchHost, item => item.className === 'image-search-preview').hidden, true);
  ui.reset();
});

test('cancelled searches cannot show old results or keep references after logout', async () => {
  const fixture = browserFixture(); let resolveQuery; let querySignal;
  const api = async (path, options) => path.endsWith('/status') ? { enabled: true, available: true, indexing: false } : new Promise(resolve => { resolveQuery = resolve; querySignal = options.signal; });
  const ui = Search.create({ ...fixture, api, post: async () => ({}), openBook: () => {}, isAdmin: () => false });
  ui.showSearch(); await flush();
  const input = fixture.find(fixture.searchHost, item => item.id === 'image-search-file'); input.files = [pngFile()]; input.dispatch('change'); await flush();
  const preview = fixture.find(fixture.searchHost, item => item.tagName === 'img'); const previewUrl = preview.src;
  assert.match(previewUrl, /^blob:/);
  const run = fixture.find(fixture.searchHost, item => item.textContent === '开始搜索'); assert.equal(run.disabled, false);
  run.dispatch('click'); assert.ok(querySignal); ui.reset(); assert.equal(querySignal.aborted, true);
  resolveQuery({ results: [{ book: { id: 'private', title: 'Private' }, page: 2, match: 'exact' }] }); await flush();
  assert.equal(fixture.find(fixture.searchHost, item => item.className === 'image-search-results').children.length, 0);
  assert.equal(preview.src, ''); assert.equal(await fetch(previewUrl).then(() => true, () => false), false);
  ui.reset();
});

test('catalog permission changes discard visible matches and pending responses', async () => {
  const fixture = browserFixture(); let count = 0; let resolveQuery;
  const api = async path => path.endsWith('/status') ? { enabled: true, available: true } : ++count === 1 ? { results: [{ book: { id: 'private', title: 'Private' }, page: 0, match: 'exact' }] } : new Promise(resolve => { resolveQuery = resolve; });
  const ui = Search.create({ ...fixture, api, post: async () => ({}), openBook: () => {}, isAdmin: () => false });
  ui.syncCatalog('authorized'); ui.showSearch(); await flush(); const input = fixture.find(fixture.searchHost, item => item.id === 'image-search-file'); input.files = [pngFile()]; input.dispatch('change'); await flush();
  const run = fixture.find(fixture.searchHost, item => item.textContent === '开始搜索'); await run.dispatch('click');
  const results = fixture.find(fixture.searchHost, item => item.className === 'image-search-results'); assert.equal(results.children.length, 1);
  ui.syncCatalog('revoked'); assert.equal(results.children.length, 0);
  run.dispatch('click'); ui.syncCatalog('changed-again'); resolveQuery({ results: [{ book: { id: 'private', title: 'Private' }, page: 0, match: 'exact' }] }); await flush(); assert.equal(results.children.length, 0);
  ui.reset();
});

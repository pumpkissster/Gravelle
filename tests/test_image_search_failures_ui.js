'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Search = require('../static/image-search-ui.js');
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  class Element {
    constructor(tag, document) { Object.assign(this, { tagName: tag, ownerDocument: document, children: [], listeners: {}, attributes: {}, dataset: {}, hidden: false, value: '', textContent: '', className: '', open: false, classList: { add() {}, remove() {}, toggle() {} } }); }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    dispatch(type, event = {}) { return Promise.all((this.listeners[type] || []).map(handler => handler({ target: this, ...event }))); }
    close() { this.open = false; }
    showModal() { this.open = true; }
    contains(item) { return item === this || this.children.some(child => child.contains?.(item)); }
  }
  const document = { createElement: tag => new Element(tag, document), querySelector: () => null, addEventListener() {}, activeElement: null };
  document.body = new Element('body', document);
  const searchHost = new Element('section', document), settingsHost = new Element('section', document);
  function all(root, predicate) { return [root, ...root.children.flatMap(child => all(child, () => true))].filter(predicate); }
  return { document, searchHost, settingsHost, all, find: predicate => all(settingsHost, predicate)[0] };
}
const item = (page = 0, retryable = true) => ({ book_id: 'book-a', page_key: `album/${page}.png`, page, book_title: '<album>', library_name: '写真库', filename: `${page}.png`, reason: retryable ? '合成解码失败' : '当前图片实际格式为 AVIF，图片索引暂不支持', actual_format: retryable ? '' : 'AVIF', failure_code: retryable ? 'decode_error' : 'unsupported_format', retryable });
const expandFailures = async view => { await view.find(node => node.attributes['aria-controls'] === 'image-search-failure-panel').dispatch('click'); await flush(); };
const status = count => ({ enabled: true, available: true, indexing: false, summary: { failed: count, indexed: 2, pending: 0, total: count + 2 }, progress: { done: count + 2, total: count + 2 } });

test('failure rows validate compound keys, page zero and unsupported retry flags', () => {
  const good = item(), unsupported = item(1, false);
  assert.deepEqual(Search.normalizeFailures({ items: [good, good, unsupported, { ...good, page: -1 }, { ...good, retryable: 'yes' }, null] }), [good, unsupported]);
  assert.notEqual(Search.failureKey(good), Search.failureKey({ ...good, book_id: 'another' }));
});

test('failure details start collapsed and only fetch when the count button is expanded', async () => {
  const view = fixture(); let reads = 0;
  const ui = Search.create({ ...view, api: async path => path.includes('/failures') ? (reads++, { items: [item()], total: 1 }) : status(1), post: async () => ({}), openBook() {}, isAdmin: () => true });
  try {
    ui.showSettings(); await flush(); await flush();
    const toggle = view.find(node => node.attributes['aria-controls'] === 'image-search-failure-panel');
    const panel = view.find(node => node.id === 'image-search-failure-panel');
    assert.equal(toggle.textContent, '失败 1 张'); assert.equal(toggle.attributes['aria-expanded'], 'false'); assert.equal(panel.hidden, true); assert.equal(reads, 0);
    await view.find(node => node.textContent === '刷新').dispatch('click'); await flush();
    assert.equal(reads, 0);
    await toggle.dispatch('click'); await flush();
    assert.equal(toggle.attributes['aria-expanded'], 'true'); assert.equal(panel.hidden, false); assert.equal(reads, 1);
    await toggle.dispatch('click');
    assert.equal(toggle.attributes['aria-expanded'], 'false'); assert.equal(panel.hidden, true);
    await view.find(node => node.textContent === '刷新').dispatch('click'); await flush();
    assert.equal(reads, 1);
    ui.hideSettings(); ui.showSettings(); await flush();
    assert.equal(toggle.attributes['aria-expanded'], 'false'); assert.equal(panel.hidden, true); assert.equal(reads, 1);
  } finally { ui.reset(); }
});

test('collapsing failure details aborts an in-flight list and ignores its late rows', async () => {
  const view = fixture(); let release, signal;
  const ui = Search.create({ ...view, api: async (path, options) => path.includes('/failures') ? new Promise(resolve => { release = resolve; signal = options.signal; }) : status(1), post: async () => ({}), openBook() {}, isAdmin: () => true });
  try {
    ui.showSettings(); await flush();
    const toggle = view.find(node => node.attributes['aria-controls'] === 'image-search-failure-panel');
    const opening = toggle.dispatch('click'); await flush();
    await toggle.dispatch('click'); assert.equal(signal.aborted, true);
    release({ items: [item()], total: 1 }); await opening; await flush();
    assert.equal(view.find(node => node.className === 'image-search-failure-list').children.length, 0);
    assert.equal(view.find(node => node.id === 'image-search-failure-panel').hidden, true);
  } finally { ui.reset(); }
});

test('failure list supports exact-page opening and single/current-page batch retry', async () => {
  const view = fixture(); const opened = [], posts = []; let rows = [item(), item(1, false)];
  const ui = Search.create({ ...view, api: async path => path.includes('/failures') ? { items: rows, total: rows.length } : status(rows.length), post: async (path, body) => { posts.push({ path, body }); rows = rows.filter(row => !body.items.some(value => Search.failureKey(value) === Search.failureKey(row))); return { retried: body.items.length, skipped: 0, snapshot: status(rows.length) }; }, openBook() {}, onOpenFailure: (...args) => opened.push(args), isAdmin: () => true });
  try {
    ui.showSettings(); await flush(); await expandFailures(view);
    assert.equal(view.all(view.settingsHost, node => node.className === 'image-search-failure-row').length, 2);
    await view.find(node => node.className === 'image-search-failure-book').dispatch('click');
    assert.deepEqual(opened, [['book-a', 0]]);
    const checks = view.all(view.settingsHost, node => node.className === 'image-search-failure-check');
    assert.equal(checks[1].disabled, true);
    const all = view.find(node => node.attributes['aria-label'] === '选择本页可重试图片'); all.checked = true; await all.dispatch('change');
    await view.find(node => node.textContent === '重试所选').dispatch('click');
    assert.deepEqual(posts, [{ path: '/api/image-search/retry', body: { items: [{ book_id: 'book-a', page_key: 'album/0.png' }] } }]);
    assert.equal(view.all(view.settingsHost, node => node.className === 'image-search-failure-row').length, 1);
    assert.equal(view.find(node => node.className === 'image-search-failure-unsupported').textContent, '格式暂不支持');
  } finally { ui.reset(); }
});

test('failure pagination clears selection and uses bounded server pages', async () => {
  const view = fixture(), urls = [];
  const ui = Search.create({ ...view, api: async path => { if (path.includes('/failures')) { urls.push(path); return { items: [item(path.includes('page=2&') ? 20 : 0)], total: 41 }; } return status(41); }, post: async () => ({}), openBook() {}, isAdmin: () => true });
  try {
    ui.showSettings(); await flush(); await expandFailures(view);
    const check = view.find(node => node.className === 'image-search-failure-check'); check.checked = true; await check.dispatch('change');
    await view.find(node => node.textContent === '下一页').dispatch('click');
    assert.deepEqual(urls, ['/api/image-search/failures?page=1&page_size=20', '/api/image-search/failures?page=2&page_size=20']);
    assert.equal(view.find(node => node.className === 'image-search-failure-check').checked, false);
    assert.equal(view.find(node => node.textContent === '重试所选').disabled, true);
    assert.equal(view.find(node => node.textContent === '第 2 / 3 页').tagName, 'span');
  } finally { ui.reset(); }
});

test('status polling preserves selected failure row DOM without refetching unchanged failures', async () => {
  const realSetTimeout = global.setTimeout, realClearTimeout = global.clearTimeout, ticks = [];
  global.setTimeout = callback => { ticks.push(callback); return ticks.length; }; global.clearTimeout = () => {};
  const view = fixture(); let reads = 0, count = 1;
  const ui = Search.create({ ...view, api: async path => path.includes('/failures') ? (reads++, { items: [item()], total: 1 }) : status(count), post: async () => ({}), openBook() {}, isAdmin: () => true });
  try {
    ui.showSettings(); await flush(); await expandFailures(view);
    const row = view.find(node => node.className === 'image-search-failure-row');
    const check = view.find(node => node.className === 'image-search-failure-check'); check.checked = true; await check.dispatch('change');
    await ticks.pop()(); await flush();
    assert.strictEqual(view.find(node => node.className === 'image-search-failure-row'), row); assert.equal(reads, 1);
    count = 2; await ticks.pop()(); await flush();
    assert.strictEqual(view.find(node => node.className === 'image-search-failure-row'), row); assert.equal(reads, 1); assert.equal(check.checked, true);
  } finally { ui.reset(); global.setTimeout = realSetTimeout; global.clearTimeout = realClearTimeout; }
});

test('logout aborts failure reads and ignores a retry response from the previous account', async () => {
  const view = fixture(); let resolveRead, readSignal;
  const ui = Search.create({ ...view, api: async (path, options) => path.includes('/failures') ? new Promise(resolve => { resolveRead = resolve; readSignal = options.signal; }) : status(1), post: async () => ({}), openBook() {}, isAdmin: () => true });
  ui.showSettings(); await flush(); const opening = view.find(node => node.attributes['aria-controls'] === 'image-search-failure-panel').dispatch('click'); await flush(); assert.ok(readSignal); ui.reset(); assert.equal(readSignal.aborted, true);
  resolveRead({ items: [item()], total: 1 }); await opening; await flush();
  assert.equal(view.find(node => node.className === 'image-search-failure-list').children.length, 0);
  let resolveRetry;
  const second = fixture(); const next = Search.create({ ...second, api: async path => path.includes('/failures') ? { items: [item()], total: 1 } : status(1), post: async () => new Promise(resolve => { resolveRetry = resolve; }), openBook() {}, isAdmin: () => true });
  next.showSettings(); await flush(); await expandFailures(second);
  const retry = second.find(node => node.textContent === '重试'); const pending = retry.dispatch('click'); next.reset();
  resolveRetry({ retried: 1, snapshot: status(0) }); await pending; await flush();
  assert.equal(second.find(node => node.className === 'image-search-failure-list').children.length, 0);
  next.reset();
});

'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Routes = require('../static/page-router.js');
const Transcoding = require('../static/transcoding-ui.js');

test('optimization filters accept only bounded text and known statuses', () => {
  assert.deepEqual(Transcoding.normalizeFilters({ query: '作品', status: 'running', library: '库-1', password: 'ignored' }), { query: '作品', status: 'running', library: '库-1' });
  assert.deepEqual(Transcoding.normalizeFilters({ query: '写'.repeat(201), status: 'constructor', library: 'x'.repeat(201) }), { query: '写'.repeat(200), status: '', library: '' });
  for (const value of [null, undefined, [], false, 'pending', { query: 42, status: {}, library: 1 }]) {
    assert.deepEqual(Transcoding.normalizeFilters(value), { query: '', status: '', library: '' });
  }
  for (const status of Object.keys(Transcoding.statusLabels)) assert.equal(Transcoding.normalizeFilters({ status }).status, status);
});

function optimizationFixture() {
  class Element {
    constructor(tag, document) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.style = {}; this.value = ''; this.className = ''; this.open = false; this.classList = { add() {}, toggle() {} }; }
    append(...items) { items.forEach(item => { item.remove(); item.parent = this; this.children.push(item); }); }
    replaceChildren(...items) { this.children.forEach(item => { item.parent = null; }); this.children = []; this.append(...items); }
    insertBefore(item, next) { item.remove(); const position = next ? this.children.indexOf(next) : this.children.length; item.parent = this; this.children.splice(position, 0, item); }
    after(item) { this.parent.insertBefore(item, this.parent.children[this.parent.children.indexOf(this) + 1]); }
    remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    dispatch(type) { return Promise.all((this.listeners[type] || []).map(handler => handler({ target: this }))); }
    querySelectorAll(selector) { const match = item => item.tagName === selector.split('[')[0] && (!selector.includes('[open]') || item.open) && (!selector.includes('[data-transcode-id]') || item.dataset.transcodeId !== undefined); return this.children.flatMap(item => [...(match(item) ? [item] : []), ...item.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    get elements() { return ['input', 'select', 'button'].flatMap(tag => this.querySelectorAll(tag)); }
    close() { this.open = false; }
    showModal() { this.open = true; }
  }
  const document = { activeElement: null, hidden: false, createElement: tag => new Element(tag, document) };
  document.body = new Element('body', document); const host = new Element('section', document); document.body.append(host);
  function find(root, predicate) { if (predicate(root)) return root; for (const child of root.children) { const result = find(child, predicate); if (result) return result; } return null; }
  return { document, host, find };
}
const optimizationFlush = () => new Promise(resolve => setImmediate(resolve));

test('restored optimization filters wait for available libraries and render only matching works', async () => {
  const fixture = optimizationFixture(); let resolveSnapshot; let renders = 0;
  const ui = Transcoding.create({ host: fixture.host, api: () => new Promise(resolve => { resolveSnapshot = resolve; }), post: async () => ({}), toast() {}, onRender: () => { renders += 1; } });
  assert.equal(ui.isLoaded(), false);
  assert.deepEqual(ui.restoreFilters({ query: '目标', status: 'pending', library: 'a' }), { query: '目标', status: 'pending', library: 'a' });
  assert.equal(renders, 0);
  ui.show();
  resolveSnapshot({ rules: Transcoding.defaultRules, books: [
    { id: 'a1', title: '目标一', library_id: 'a', status: 'pending' },
    { id: 'a2', title: '目标二', library_id: 'a', status: 'ready' },
    { id: 'b1', title: '目标三', library_id: 'b', status: 'pending' }
  ] });
  await optimizationFlush();
  assert.equal(ui.isLoaded(), true);
  const list = fixture.find(fixture.host, item => item.className === 'transcode-book-list');
  assert.equal(list.children.length, 1); assert.equal(list.querySelector('input').dataset.transcodeId, 'a1'); assert.equal(renders, 1);
  assert.deepEqual(ui.getFilters(), { query: '目标', status: 'pending', library: 'a' });
  assert.deepEqual(ui.restoreFilters({ library: 'removed' }), { query: '', status: '', library: '' });
  assert.equal(list.children.length, 3);
  ui.reset(); assert.deepEqual(ui.getFilters(), { query: '', status: '', library: '' }); assert.equal(ui.isLoaded(), false); ui.hide();
});

test('filter changes notify after rendering and unchanged polls preserve filter controls', async () => {
  const fixture = optimizationFixture(); const snapshot = { rules: Transcoding.defaultRules, books: [] }; const events = [];
  const ui = Transcoding.create({ host: fixture.host, api: async () => snapshot, post: async () => ({}), toast() {}, onRender: () => events.push('render'), onFiltersChange: filters => events.push(filters) });
  ui.restoreFilters({ library: 'missing' }); assert.equal(ui.getFilters().library, 'missing');
  ui.show(); await optimizationFlush(); assert.equal(ui.getFilters().library, '');
  const query = fixture.find(fixture.host, item => item.placeholder === '搜索作品');
  const status = fixture.find(fixture.host, item => item.tagName === 'select' && item.children.some(option => option.textContent === '全部状态'));
  query.value = '新搜索'; events.length = 0; await query.dispatch('input');
  assert.deepEqual(events, ['render', { query: '新搜索', status: '', library: '' }]);
  status.value = 'failed'; events.length = 0; await status.dispatch('change');
  assert.deepEqual(events, ['render', { query: '新搜索', status: 'failed', library: '' }]);
  events.length = 0; await ui.refresh(); assert.equal(events.length, 0);
  assert.equal(fixture.find(fixture.host, item => item.placeholder === '搜索作品'), query);
  assert.deepEqual(ui.getFilters(), { query: '新搜索', status: 'failed', library: '' }); ui.reset(); ui.hide();
});

test('settings sections and transcode rules have distinct refreshable addresses', () => {
  for (const section of ['interface', 'performance', 'reading', 'transcoding']) {
    const route = { view: 'settings', section };
    assert.deepEqual(Routes.parse(Routes.hash(route)), route);
  }
  const rules = { view: 'settings', section: 'transcoding', tab: 'rules' };
  assert.equal(Routes.hash(rules), '#/settings/transcoding?tab=rules');
  assert.deepEqual(Routes.parse(Routes.hash(rules)), rules);
  assert.deepEqual(Routes.base({ view: 'book', id: 'b', from: rules }), rules);
});

test('settings routes discard unknown tabs and unrelated or sensitive query parameters', () => {
  assert.deepEqual(Routes.parse('#/settings/transcoding?tab=raw&original=1&password=secret'), { view: 'settings', section: 'transcoding' });
  assert.deepEqual(Routes.parse('#/settings/users?tab=rules'), { view: 'settings' });
  for (const route of ['#/settings/unknown', '#/settings/%2Fetc', '#/settings/transcoding/extra']) {
    assert.deepEqual(Routes.parse(route), { view: 'home' });
  }
});

test('legacy service and user links return to their panels on settings home', () => {
  for (const section of ['service', 'users']) {
    assert.deepEqual(Routes.parse('#/settings/' + section), { view: 'settings' });
    assert.deepEqual(Routes.normalize({ view: 'settings', section }), { view: 'settings' });
    assert.equal(Routes.hash({ view: 'settings', section }), '#/settings');
  }
});

test('displayed progress counts successfully processed matched images only', () => {
  assert.equal(Transcoding.progress({ matched_count: 12, success_count: 10, page_count: 100 }), 83);
  assert.equal(Transcoding.progress({ matched_count: 0, success_count: 0, page_count: 100 }), 0);
  assert.equal(Transcoding.progress({ matched_count: 2, success_count: 9 }), 100);
  assert.equal(Transcoding.progress({ matched_count: 2, success_count: -1 }), 0);
});

test('bulk task actions are limited to compatible current states', () => {
  for (const status of ['queued', 'running']) assert.equal(Transcoding.eligible({ status }, 'pause'), true);
  for (const status of ['pending', 'original', 'ready', 'failed']) assert.equal(Transcoding.eligible({ status }, 'pause'), false);
  assert.equal(Transcoding.eligible({ status: 'paused' }, 'resume'), true);
  assert.equal(Transcoding.eligible({ status: 'running' }, 'resume'), false);
  assert.equal(Transcoding.eligible({ status: 'failed' }, 'retry'), true);
  assert.equal(Transcoding.eligible({ status: 'cancelled' }, 'retry'), true);
  assert.equal(Transcoding.eligible({ status: 'original', matched_count: 12 }, 'transcode'), true);
  assert.equal(Transcoding.eligible({ status: 'queued', matched_count: 12 }, 'transcode'), false);
  assert.equal(Transcoding.eligible({ status: 'pending', matched_count: 0 }, 'transcode'), false);
  assert.equal(Transcoding.eligible({ status: 'pending' }, 'unsafe'), false);
});

test('actual derivative storage is formatted as bytes without estimating original savings', () => {
  assert.equal(Transcoding.bytes(0), '0 B');
  assert.equal(Transcoding.bytes(1024), '1 KiB');
  assert.equal(Transcoding.bytes(2 * 1024 * 1024), '2 MiB');
  assert.equal(Transcoding.defaultRules.enabled, false);
  assert.equal(Transcoding.defaultRules.workers, 1);
});

test('historical pending works on login only establish a reminder baseline', () => {
  const tracker = Transcoding.createPendingTracker();
  assert.equal(tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1 }), null);
  assert.equal(tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1 }), null);
  tracker.reset();
  assert.equal(tracker.observe({ pending_book_ids: ['another-user'], scanning: false, last_scan: 8 }), null);
});

test('a fast completed import reports only new pending works once', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1, libraries: [{ id: 'a', last_scan: 1 }] });
  tracker.expectScan();
  const completed = { pending_book_ids: ['old', 'new'], scanning: false, last_scan: 2, libraries: [{ id: 'a', last_scan: 1 }, { id: 'b', last_scan: 2 }] };
  assert.deepEqual(tracker.observe(completed), { ids: ['new'], count: 1 });
  assert.equal(tracker.observe(completed), null);
  assert.equal(tracker.observe({ ...completed, last_scan: 3 }), null);
});

test('partial scan results wait until completion and are summarized together', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1 });
  assert.equal(tracker.observe({ pending_book_ids: ['old', 'first'], scanning: true, last_scan: 1 }), null);
  assert.equal(tracker.observe({ pending_book_ids: ['old', 'first', 'second'], scanning: true, last_scan: 2 }), null);
  assert.deepEqual(tracker.observe({ pending_book_ids: ['old', 'first', 'second'], scanning: false, last_scan: 3 }), { ids: ['first', 'second'], count: 2 });
});

test('rules and task changes update the baseline without inventing a scan popup', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: [], scanning: false, last_scan: 1 });
  assert.equal(tracker.observe({ pending_book_ids: ['rule-change'], scanning: false, last_scan: 1 }), null);
  assert.equal(tracker.observe({ pending_book_ids: ['rule-change'], scanning: false, last_scan: 2 }), null);
  tracker.observe({ pending_book_ids: [], scanning: false, last_scan: 2 });
  assert.deepEqual(tracker.observe({ pending_book_ids: ['rule-change'], scanning: false, last_scan: 3 }), { ids: ['rule-change'], count: 1 });
});

test('login during an existing scan stays quiet unless the administrator starts a new scan', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: ['partial'], scanning: true, last_scan: 1 });
  assert.equal(tracker.observe({ pending_book_ids: ['partial', 'rest'], scanning: false, last_scan: 2 }), null);
  tracker.reset();
  tracker.observe({ pending_book_ids: ['partial'], scanning: true, last_scan: 1 });
  tracker.expectScan();
  assert.deepEqual(tracker.observe({ pending_book_ids: ['partial', 'new'], scanning: false, last_scan: 2 }), { ids: ['new'], count: 1 });
});

test('heartbeat timestamps do not replace displayed rows, but progress and errors do', () => {
  const book = { id: 'a', title: '测试作品', status: 'running', matched_count: 10, success_count: 2, updated_at: 1 };
  assert.equal(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, updated_at: 2, rule_revision: 9 }));
  assert.notEqual(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, success_count: 3 }));
  assert.notEqual(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, error: '处理失败' }));
  assert.equal(Transcoding.statusLabels.running, '优化中');
});

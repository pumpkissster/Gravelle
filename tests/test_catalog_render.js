'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Catalog = require('../static/catalog-render.js');

test('a 1200-book grid preserves full scroll height and mounts only nearby and focused rows', () => {
  const rowHeight = row => row % 3 ? 240 : 258;
  const layout = Catalog.createGridLayout({ count: 1200, columns: 5, rowHeight, gap: 20 });
  const segments = layout.window({ top: layout.offset(725) + 40, height: 780, pinned: [0, 1199] });
  const active = segments.filter(part => !part.spacer);
  assert.ok(active.reduce((count, part) => count + part.end - part.start, 0) <= 65);
  for (const index of [0, 725, 1199]) assert.ok(active.some(part => part.start <= index && part.end > index));
  let total = (segments.length - 1) * 20;
  for (const part of segments) total += part.spacer ? part.height : rowHeight(part.start / 5);
  assert.equal(total, layout.height);
  assert.equal(segments[0].start, 0);
  assert.equal(segments.at(-1).end, 1200);
});

test('changing from a wide grid to two columns keeps the saved book addressable near the viewport', () => {
  const anchor = 832;
  const wide = Catalog.createGridLayout({ count: 1001, columns: 16, rowHeight: 295, gap: 25 });
  const narrow = Catalog.createGridLayout({ count: 1001, columns: 2, rowHeight: 330, gap: 20 });
  assert.ok(narrow.offset(anchor) > wide.offset(anchor));
  for (const layout of [wide, narrow]) {
    const visible = layout.window({ top: layout.offset(anchor), height: 700 });
    assert.ok(visible.some(part => !part.spacer && part.start <= anchor && part.end > anchor));
    const end = layout.window({ top: layout.height - 350, height: 700 });
    assert.deepEqual(end.at(-1), { start: 1000 - (1000 % layout.columns), end: 1001, spacer: false });
  }
});

function fixture() {
  const document = { activeElement: null };
  class Element {
    constructor(id) { this.id = id; this.children = []; this.parentElement = null; this.ownerDocument = document; this.moves = 0; this.dataset = {}; this.attributes = {}; this.style = {}; this.listeners = {}; this.classList = { toggle() {} }; }
    contains(value) { return value === this || this.children.some(child => child.contains(value)); }
    remove() {
      if (!this.parentElement) return;
      if (this.contains(document.activeElement)) document.activeElement = null;
      const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this), 1); this.parentElement = null;
    }
    insertBefore(value, next) { this.moves += 1; value.remove(); const at = next ? this.children.indexOf(next) : this.children.length; assert.ok(at >= 0); this.children.splice(at, 0, value); value.parentElement = this; }
    append(...values) { values.forEach(value => this.insertBefore(value, null)); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    dispatch(type) { return Promise.all((this.listeners[type] || []).map(listener => listener())); }
    replaceWith(value) { const parent = this.parentElement; assert.ok(parent); parent.insertBefore(value, this); this.remove(); }
    focus(options) { document.activeElement = this; this.focusOptions = options; }
  }
  return { document, Element, container: new Element('list') };
}
function clocks() {
  let next = 0; const timers = new Map();
  return {
    timers,
    schedule(fn, delay) { const id = ++next; timers.set(id, { fn, delay }); return id; },
    unschedule(id) { timers.delete(id); },
    run() { const pending = [...timers.values()]; timers.clear(); pending.forEach(item => item.fn()); }
  };
}
const status = revision => ({ revision, scanning: false, cover_pending: false });
function apiFixture() {
  const calls = []; let revision = 'one'; const data = { '/api/libraries': { libraries: [] }, '/api/library': { books: [] }, '/api/models': { models: [] }, '/api/bookmarks': { bookmarks: [] } };
  return { calls, data, setRevision(value) { revision = value; }, async api(path) { calls.push(path); return path.endsWith('/status') ? status(revision) : data[path]; } };
}
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

test('unchanged cards retain their images, controls and focus without any DOM moves', () => {
  const { Element, container, document } = fixture(); let created = 0;
  const list = Catalog.createKeyedList({ create(item) { created += 1; const element = new Element(item.id); element.append(new Element('cover')); element.append(new Element('favorite')); return element; }, update(element, item) { element.book = item; } });
  list.render(container, [{ id: 'a', title: 'A' }, { id: 'b', title: 'B' }]);
  const [first, second] = container.children; const cover = first.children[0]; const favorite = first.children[1]; favorite.focus(); container.moves = 0;
  list.render(container, [{ id: 'a', title: 'Renamed', favorite: true }, { id: 'b', title: 'B' }]);
  assert.equal(created, 2); assert.equal(container.moves, 0); assert.equal(first.children[0], cover); assert.equal(container.children[1], second); assert.equal(document.activeElement, favorite);
  assert.equal(first.book.title, 'Renamed'); assert.equal(first.book.favorite, true);
});

test('sorting reuses card nodes and restores a nested focused control after moving it', () => {
  const { Element, container, document } = fixture();
  const list = Catalog.createKeyedList({ create(item) { const element = new Element(item.id); element.append(new Element('open')); return element; } });
  list.render(container, [{ id: 'a' }, { id: 'b' }, { id: 'c' }]); const [a, b, c] = container.children; b.children[0].focus();
  list.render(container, [{ id: 'b' }, { id: 'c' }, { id: 'a' }]);
  assert.deepEqual(container.children, [b, c, a]); assert.equal(document.activeElement, b.children[0]); assert.deepEqual(b.children[0].focusOptions, { preventScroll: true });
});

test('removed cards are discarded and account reset cannot reuse the old account nodes', () => {
  const { Element, container } = fixture(); const list = Catalog.createKeyedList({ create: item => new Element(item.id) });
  list.render(container, [{ id: 'a' }, { id: 'b' }]); const old = container.children[0];
  list.render(container, [{ id: 'a' }]); assert.equal(list.size, 1);
  list.clear(); list.render(container, [{ id: 'a' }]); assert.notEqual(container.children[0], old); assert.equal(old.parentElement, null);
});

test('flow appends only new cards and updates reused actions with the latest book object', () => {
  const { Element, container } = fixture(); let created = 0;
  const list = Catalog.createKeyedList({ create(item) { created += 1; const element = new Element(item.id); element.open = () => element.current; return element; }, update(element, item) { element.current = item; } });
  list.render(container, [{ id: 'a', title: 'old' }]); const a = container.children[0]; list.append(container, [{ id: 'b' }, { id: 'c' }]);
  const latest = { id: 'a', title: 'new', page_count: 40 }; list.render(container, [latest, { id: 'b' }, { id: 'c' }]);
  assert.equal(created, 3); assert.equal(container.children[0], a); assert.equal(a.open(), latest);
});

test('real book-card handlers keep the correct book after filtering, sorting, reloading and flow append', async () => {
  const { Element, container } = fixture(); const actions = [];
  const state = { view: 'library', sort: 'title' }; const selection = { enabled: false, busy: false, ids: new Set() };
  const node = (tag, className = '', textContent = '') => Object.assign(new Element(tag), { className, textContent });
  const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
  const start = source.indexOf('  function makeBookCard('), end = source.indexOf('  function renderContinue(', start);
  assert.ok(start >= 0 && end > start);
  const scope = { node, state, selection, Batch: { kind: () => 'books' }, isAdmin: () => true,
    icon: name => node('svg', name), makeCover(book, className) { const wrap = node('div', className); wrap.append(node('img', '', book.id)); return wrap; },
    selectionCheckbox(id) { return Object.assign(node('input'), { value: id }); },
    toggleSelection(id) { actions.push(['select', id]); }, showBookDetail(book) { actions.push(['open', book.id, book.title]); },
    async toggleFavorite(book) { actions.push(['favorite', book.id, book.title]); }, toast(message) { assert.fail(message); },
    typeLabels: { photo: '写真' }, formatDate: String, formatAddedDate: String };
  vm.createContext(scope); vm.runInContext(source.slice(start, end), scope, { filename: 'app.js:book-card' });
  const list = Catalog.createKeyedList({ create: scope.makeBookCard, update: scope.updateBookCard });
  const books = Array.from({ length: 80 }, (_, index) => ({ id: String(index).padStart(3, '0'), title: 'Work A ' + String(index).padStart(3, '0'), page_count: 2, cover_version: 'one' }));
  list.render(container, books.slice(0, 48));
  list.render(container, books.filter(book => book.title.includes('A 05')));
  list.render(container, books.filter(book => book.title.includes('A 05')).slice().reverse());
  list.render(container, books.slice(0, 48)); list.append(container, books.slice(48));
  const target = container.children.find(card => card.catalogBook.id === '020');
  await target.catalogParts.open.dispatch('click');
  assert.deepEqual(actions.pop(), ['open', '020', 'Work A 020']);
  const fresh = books.map(book => ({ ...book, title: book.id === '020' ? 'Renamed A 020' : book.title }));
  list.render(container, fresh.slice().reverse());
  assert.equal(container.children.find(card => card.catalogBook.id === '020'), target);
  for (const card of container.children) {
    assert.equal(card.catalogParts.open.dataset.bookId, card.catalogBook.id);
    assert.equal(card.catalogParts.open.attributes['aria-label'], `查看《${card.catalogBook.title}》详情，共 2 页`);
  }
  await target.catalogParts.open.dispatch('click'); assert.deepEqual(actions.pop(), ['open', '020', 'Renamed A 020']);
  await target.catalogParts.favorite.dispatch('click'); assert.deepEqual(actions.pop(), ['favorite', '020', 'Renamed A 020']);
  selection.enabled = true; await target.catalogParts.open.dispatch('click'); assert.deepEqual(actions.pop(), ['select', '020']);
  const cover = target.catalogParts.cover;
  list.render(container, fresh); assert.equal(target.catalogParts.cover, cover);
  list.render(container, fresh.map(book => book.id === '020' ? { ...book, cover_version: 'two' } : book));
  assert.notEqual(target.catalogParts.cover, cover); assert.equal(target.catalogBook.id, '020');
});

test('search merges a burst of typing into one 150ms commit', () => {
  const timer = clocks(); const commits = []; const search = Catalog.createSearchInput(value => commits.push(value), timer);
  search.input('a'); search.input('ab'); search.input('abc'); assert.equal(timer.timers.size, 1); assert.equal([...timer.timers.values()][0].delay, 150); assert.deepEqual(commits, []);
  timer.run(); assert.deepEqual(commits, ['abc']); search.flush(); assert.deepEqual(commits, ['abc']);
});

test('IME composition suppresses incomplete searches and commits the finished text once', () => {
  const timer = clocks(); const commits = []; const search = Catalog.createSearchInput(value => commits.push(value), timer);
  search.input('draft'); search.compositionStart(); search.input('xi', true); search.flush(); timer.run(); assert.deepEqual(commits, []);
  search.input('写真', true); search.compositionEnd('写真'); search.input('写真'); timer.run(); assert.deepEqual(commits, ['写真']);
});

test('search flushes before explicit navigation and cancels unfinished work on account change', () => {
  const timer = clocks(); const commits = []; const search = Catalog.createSearchInput(value => commits.push(value), timer);
  search.input('finished'); search.flush(); timer.run(); assert.deepEqual(commits, ['finished']);
  search.input('private'); search.cancel(); timer.run(); search.flush(); assert.deepEqual(commits, ['finished']); assert.equal(timer.timers.size, 0);
});

test('catalogue loader downloads a full snapshot only on first load, changes or explicit force', async () => {
  const source = apiFixture(); const loader = Catalog.createLoader(source.api);
  assert.equal((await loader.load()).unchanged, false); assert.equal(source.calls.length, 5);
  const unchanged = await loader.load({ loaded: true }); assert.equal(unchanged.unchanged, true); assert.equal(source.calls.length, 6);
  source.setRevision('two'); assert.equal((await loader.load({ loaded: true })).unchanged, false); assert.equal(source.calls.length, 11);
  assert.equal((await loader.load({ loaded: true, force: true })).unchanged, false); assert.equal(source.calls.length, 16);
});

test('an old server falls back to the original endpoints until a reset tries capability discovery again', async () => {
  const calls = []; const loader = Catalog.createLoader(async path => { calls.push(path); if (path.endsWith('/status')) throw Object.assign(new Error('missing'), { status: 404 }); return {}; });
  await loader.load(); await loader.load({ loaded: true }); assert.equal(calls.filter(path => path.endsWith('/status')).length, 1); assert.equal(calls.length, 9);
  loader.reset(); await loader.load(); assert.equal(calls.filter(path => path.endsWith('/status')).length, 2);
});

test('unauthorized status requests are not treated as legacy servers or followed by data requests', async () => {
  const calls = []; const loader = Catalog.createLoader(async path => { calls.push(path); throw Object.assign(new Error('signed out'), { status: 401 }); });
  await assert.rejects(loader.load(), error => error.status === 401); assert.deepEqual(calls, ['/api/catalog/status']);
});

test('a failed snapshot does not acknowledge its revision and retries the full request', async () => {
  const source = apiFixture(); let fail = true;
  const loader = Catalog.createLoader(async path => { if (path === '/api/models' && fail) throw new Error('offline'); return source.api(path); });
  await assert.rejects(loader.load()); fail = false; source.calls.length = 0; const result = await loader.load({ loaded: true });
  assert.equal(result.unchanged, false); assert.equal(source.calls.length, 5);
});

test('reset while checking status suppresses all old account data requests', async () => {
  const pending = deferred(); const calls = [];
  const loader = Catalog.createLoader(async path => { calls.push(path); return pending.promise; });
  const response = loader.load(); loader.reset(); pending.resolve(status('private')); assert.deepEqual(await response, { stale: true }); assert.equal(calls.length, 1);
});

test('reset during data loading discards the old snapshot and its revision', async () => {
  const pending = deferred(); let released = false; const source = apiFixture();
  const loader = Catalog.createLoader(path => path === '/api/library' && !released ? pending.promise : source.api(path));
  const response = loader.load(); await new Promise(resolve => setImmediate(resolve)); loader.reset(); released = true; pending.resolve({ books: [{ id: 'private' }] });
  assert.deepEqual(await response, { stale: true }); assert.equal((await loader.load({ loaded: true })).unchanged, false);
});

test('concurrent catalogue checks cannot let a late old response replace the new revision', async () => {
  const pending = deferred(); let calls = 0; const source = apiFixture(); source.setRevision('new');
  const loader = Catalog.createLoader(path => path.endsWith('/status') && calls++ === 0 ? pending.promise : source.api(path));
  const old = loader.load(); const latest = await loader.load(); assert.equal(latest.status.revision, 'new'); pending.resolve(status('old'));
  assert.deepEqual(await old, { stale: true }); assert.equal((await loader.load({ loaded: true })).unchanged, true);
});

test('a concurrent mutation during snapshot reads is detected on the next check', async () => {
  let revision = 'before'; const calls = [];
  const loader = Catalog.createLoader(async path => { calls.push(path); if (path.endsWith('/status')) return status(revision); revision = 'after'; return {}; });
  await loader.load(); assert.equal((await loader.load({ loaded: true })).unchanged, false); assert.equal(calls.length, 10);
});

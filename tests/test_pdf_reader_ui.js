'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Model = require('../static/reader-model.js');
const Performance = require('../static/performance-settings.js');

// Exercise the actual reader loader with controllable image completion order.
// The rest of the application (authentication, lists and dialogs) is irrelevant here.
function fixture() {
  const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
  const start = source.indexOf('  function loadReaderImage(index) {');
  const end = source.indexOf('  function scrollProtectedPages()', start);
  assert.ok(start >= 0 && end > start);
  const requests = []; const wrap = { images: [] }; const viewport = { clientWidth: 390, clientHeight: 680, scrollTop: 217 };
  const book = { id: 'pdf', format: 'pdf', page_revision: 'a'.repeat(24), title: 'test', page_count: 3 };
  const reader = { open: true, book, token: 1, epoch: 1, rows: [], sizes: [[595, 842], [595, 842], [595, 842]].map(Model.pageDimensions), pdfUpgrades: new Map(), visible: new Set([0]) };
  reader.pageCache = Performance.createCache(0, entry => entry.cancel?.());
  const state = { prefs: { mode: 'single', fit: 'height' } };
  const context = {
    reader, state, Model, Performance, window: { innerWidth: 390, innerHeight: 680, devicePixelRatio: 1 }, queueMicrotask,
    $: selector => selector === '#page-image-wrap' ? wrap : viewport,
    currentSpread: () => [0], configureReaderCache() {}, pageMemoryEstimate: () => 1024,
    trimScrollImages() {}, scheduleVisibleScroll() {}, makePageError() { return { error: true }; },
    pageUrl: (book, index, width) => Model.pageSource(book, index, width),
    node() {
      const image = { dataset: {}, listeners: {}, style: {}, classList: { add() {} }, children: [], naturalWidth: 800, naturalHeight: 1132, parentElement: null, src: '',
        removeAttribute(name) { if (name === 'src') this.src = ''; },
        addEventListener(type, handler) { this.listeners[type] = handler; }, removeEventListener(type) { delete this.listeners[type]; },
        replaceChildren(...children) { this.children = children; }, scrollIntoView() {},
        replaceWith(next) { const index = wrap.images.indexOf(this); assert.ok(index >= 0); wrap.images[index] = next; this.parentElement = null; next.parentElement = wrap; }
      };
      Object.defineProperty(image, 'src', { get() { return this.currentSource || ''; }, set(value) { this.currentSource = value; if (value) requests.push(image); } });
      return image;
    }
  };
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  const scrollStart = source.indexOf('  function loadScrollPage(index,'); const scrollEnd = source.indexOf('  function scheduleVisibleScroll()', scrollStart);
  vm.runInContext(source.slice(scrollStart, scrollEnd), context);
  function finish(image, width = 800) { image.naturalWidth = width; image.naturalHeight = Math.round(width * 842 / 595); (image.onload || image.listeners.load)(); }
  async function show() { const pending = context.loadReaderImage(0); finish(requests.at(-1)); const image = await pending; image.parentElement = wrap; wrap.images = [image]; return image; }
  return { context, requests, wrap, viewport, reader, state, finish, show };
}

test('PDF resize reuses sufficient pages and atomically replaces a decoded sharper page without losing scroll', async () => {
  const f = fixture(); const first = await f.show(); const originalURL = first.src;
  assert.ok(originalURL.includes('width=800'));
  f.viewport.clientWidth = 400; f.context.refreshPdfResolution(); assert.equal(f.requests.length, 1);
  f.context.window.devicePixelRatio = 3; f.context.refreshPdfResolution();
  const sharper = f.requests.at(-1); assert.equal(f.requests.length, 2); assert.ok(sharper.src.includes('width=1600'));
  assert.equal(f.wrap.images[0], first); assert.equal(first.src, originalURL); assert.equal(f.viewport.scrollTop, 217);
  f.context.refreshPdfResolution(); assert.equal(f.requests.length, 2);
  f.finish(sharper, 1600); await Promise.resolve();
  assert.equal(f.wrap.images[0], sharper); assert.equal(first.src, ''); assert.equal(f.viewport.scrollTop, 217);
  f.context.window.devicePixelRatio = 1; f.context.refreshPdfResolution(); assert.equal(f.requests.length, 2);
});

test('failed PDF size upgrade keeps the working page and can retry without a blank image', async () => {
  const f = fixture(); const first = await f.show();
  f.context.window.devicePixelRatio = 3; const pending = f.context.loadReaderImage(0); const result = assert.rejects(pending, /PDF/);
  f.requests.at(-1).onerror(); await result;
  assert.equal(f.reader.pageCache.peek(0).image, first); assert.equal(f.wrap.images[0], first); assert.notEqual(first.src, '');
  const retry = f.context.loadReaderImage(0); f.finish(f.requests.at(-1), 1600); await retry; assert.equal(f.requests.length, 3);
});

test('resize during the first PDF render cannot cancel the initial page or strand its loading promise', async () => {
  const f = fixture(); const pending = f.context.loadReaderImage(0); const first = f.requests.at(-1);
  f.context.window.devicePixelRatio = 3; f.context.refreshPdfResolution();
  assert.equal(f.requests.length, 1); assert.notEqual(first.src, ''); f.finish(first); assert.equal(await pending, first);
  first.parentElement = f.wrap; f.wrap.images = [first];
  f.context.refreshPdfResolution(); assert.equal(f.requests.length, 2); assert.equal(f.wrap.images[0], first);
  f.finish(f.requests.at(-1), 1600); await Promise.resolve(); assert.notEqual(f.wrap.images[0], first);
});

test('repeated PDF enlargements cancel the obsolete pending render and retain only one upgrade per page', async () => {
  const f = fixture(); const first = await f.show(); f.state.prefs.fit = 'width';
  f.viewport.clientWidth = 1200; const obsolete = f.context.loadReaderImage(0); const cancelled = assert.rejects(obsolete, /取消/); const second = f.requests.at(-1);
  f.viewport.clientWidth = 2800; const latest = f.context.loadReaderImage(0); await cancelled;
  assert.equal(second.src, ''); assert.equal(second.onload, null); assert.equal(f.reader.pdfUpgrades.size, 1); assert.equal(f.wrap.images[0], first);
  const final = f.requests.at(-1); assert.ok(final.src.includes('width=3200')); f.finish(final, 3200); await latest;
  assert.equal(f.reader.pdfUpgrades.size, 0); assert.equal(f.wrap.images[0], final);
});

test('image-folder reader URLs and loaders do not start PDF replacements on resize', async () => {
  const f = fixture(); f.reader.book.format = 'folder'; const first = await f.show();
  assert.equal(first.src, '/api/books/pdf/pages/0'); f.context.window.devicePixelRatio = 4; f.context.refreshPdfResolution(); assert.equal(f.requests.length, 1);
});

test('continuous PDF resize upgrades visible pages only and keeps a loaded page when the larger render fails', () => {
  const f = fixture(); f.state.prefs.mode = 'webtoon';
  f.reader.rows = Array.from({ length: 3 }, () => ({ element: f.context.node('div'), loaded: false, loading: false, failed: false }));
  f.context.loadScrollPage(0); const first = f.requests.at(-1); f.finish(first);
  assert.equal(f.reader.rows[0].loaded, true); assert.equal(f.reader.rows[0].element.children[0], first);
  f.context.window.devicePixelRatio = 3; f.context.refreshPdfResolution(); const upgrade = f.requests.at(-1);
  assert.equal(f.requests.length, 2); assert.equal(f.reader.rows[0].element.children[0], first); assert.equal(f.reader.rows[1].loading, false);
  upgrade.listeners.error(); assert.equal(f.reader.rows[0].loaded, true); assert.equal(f.reader.rows[0].failed, false); assert.equal(f.reader.rows[0].element.children[0], first);
  f.context.refreshPdfResolution(); const retry = f.requests.at(-1); f.finish(retry, 1600);
  assert.equal(f.reader.rows[0].element.children[0], retry); assert.equal(f.reader.rows[0].width, 1600); assert.equal(f.viewport.scrollTop, 217);
});

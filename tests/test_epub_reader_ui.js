'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Epub = require('../static/epub-reader.js');

class Element {
  constructor(name = 'div') { this.tagName = name; this.dataset = {}; this.attributes = {}; this.children = []; this.handlers = new Map(); this.style = { overflow: '', setProperty() {} }; this.hidden = false; this.disabled = false; this.textContent = ''; this.parentElement = null; }
  addEventListener(type, handler) { this.handlers.set(type, handler); }
  removeEventListener(type, handler) { if (this.handlers.get(type) === handler) this.handlers.delete(type); }
  emit(type, event = {}) { return this.handlers.get(type)?.(event); }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? (name.startsWith('data-') ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] ?? null : null); }
  append(...children) { children.forEach(child => { child.parentElement = this; this.children.push(child); }); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  contains(child) { return this.children.includes(child); }
  get childElementCount() { return this.children.length; }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolledIntoView = true; }
  remove() { this.removed = true; }
  closest(selector) {
    if (selector === '[data-action]' && this.dataset.action) return this;
    if (selector === '[data-setting]' && this.dataset.setting) return this;
    if (selector === '[data-page]' && this.dataset.page !== undefined) return this;
    if (selector === 'a' && this.tagName === 'a') return this;
    return null;
  }
  querySelector(selector) { if (selector === 'span') return this.span || (this.span = new Element('span')); return null; }
}

function fixture({ responder, saveProgress } = {}) {
  const nodes = new Map(); const steps = []; const requests = []; const saves = []; const routes = []; const closed = []; const errors = [];
  const timers = new Map(); const frames = []; let timerId = 0; let currentKey = 'user-a';
  const frame = new Element('iframe'); const child = new Element('window'); const content = new Element('document'); const images = [new Element('img'), new Element('img')];
  const scroll = new Element('html'); Object.assign(scroll, { scrollTop: 0, clientHeight: 600, scrollHeight: 2600 }); content.scrollingElement = content.documentElement = scroll;
  content.querySelector = selector => selector === 'meta[name="gravelle-epub-document"]' ? { content: String(frame.marker) } : null;
  content.querySelectorAll = selector => selector === 'img' ? images : [];
  content.getElementById = fragment => fragment === 'note' ? { scrollIntoView() { scroll.scrollTop = 1300; } } : null;
  frame.contentDocument = content; frame.contentWindow = child;
  Object.defineProperty(frame, 'srcdoc', { get() { return this.source || ''; }, set(value) { this.source = value; this.marker = /name="gravelle-epub-document" content="(\d+)"/.exec(value)?.[1]; } });
  const dialog = new Element('dialog'); dialog.showModal = () => { dialog.open = true; }; dialog.close = () => { dialog.open = false; };
  dialog.querySelector = selector => {
    if (selector === '.epub-frame') return frame;
    if (selector === '[data-role="toc"] [aria-current="location"]') return nodes.get('[data-role="toc"]')?.children.find(node => node.getAttribute('aria-current') === 'location') || null;
    if (selector === '[data-role="toc"] button') return nodes.get('[data-role="toc"]')?.children[0] || null;
    if (selector === '#epub-settings button[data-setting]') return steps[0];
    if (!nodes.has(selector)) {
      const node = new Element(); const action = /data-action="([^"]+)"/.exec(selector)?.[1]; if (action) node.dataset.action = action; nodes.set(selector, node);
    }
    return nodes.get(selector);
  };
  dialog.querySelectorAll = selector => selector === '[data-setting]' ? steps : [];
  for (const [setting, deltas] of [['font', [-2, 2]], ['line', [-.2, .2]], ['width', [-80, 80]]]) for (const delta of deltas) { const step = new Element('button'); Object.assign(step.dataset, { setting, step: String(delta) }); steps.push(step); }
  const doc = new Element('document'); doc.body = new Element('body'); doc.body.style.overflow = 'auto'; doc.visibilityState = 'visible';
  doc.createElement = name => name === 'dialog' ? dialog : new Element(name);
  const storage = new Map();
  const win = new Element('window'); Object.assign(win, {
    requestAnimationFrame(callback) { frames.push(callback); }, setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; }, clearTimeout(id) { timers.delete(id); },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
  });
  doc.defaultView = win;
  const api = (path, options) => { requests.push({ path, options }); return responder ? responder(path, options, requests.length) : Promise.resolve({ html: '<h1>Chapter</h1><p>Body</p>', title: 'Chapter', source_version: 'a'.repeat(64) }); };
  const reader = Epub.create({ document: doc, window: win, api, settingsKey: () => currentKey,
    saveProgress: (book, payload, options) => { saves.push({ book, payload, options }); return saveProgress ? saveProgress(book, payload, options, saves.length) : Promise.resolve(); },
    onRoute: (book, at) => routes.push({ book, at }), onClose: (book, at) => closed.push({ book, at }), onSaveError: error => errors.push(error), onBookmark: (book, at) => { f.bookmark = { book, at }; },
  });
  function animate() { let count = 0; while (frames.length) { assert.ok(++count < 100); frames.shift()(); } }
  function loaded() { frame.onload?.(); animate(); }
  function click(action) { const control = dialog.querySelector(`[data-action="${action}"]`); dialog.emit('click', { target: control }); return control; }
  function tick() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); }
  function scrolled(top) { scroll.scrollTop = top; child.emit('scroll'); }
  const f = { reader, nodes, steps, requests, saves, routes, closed, errors, frame, child, content, images, scroll, dialog, doc, win, storage, animate, loaded, click, tick, scrolled, key(value) { currentKey = value; }, book: { id: 'epub-id', title: '<Untrusted title>', format: 'epub', page_count: 3, page_revision: 'a'.repeat(24), epub_chapters: [{ index: 0, title: 'One' }, { index: 1, title: 'Two' }, { index: 2, title: 'Three' }], epub_toc: [{ index: 0, title: '<TOC title>', depth: 0 }, { index: 1, title: 'Note', fragment: 'note', depth: 1 }] } };
  return f;
}
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

test('EPUB reading settings use bounded enumerated values and never interpolate arbitrary CSS', () => {
  assert.deepEqual(Epub.preferences(), { font: 20, line: 1.8, width: 720, theme: 'dark' });
  assert.deepEqual(Epub.preferences({ font: 999, line: 0, width: 'url(evil)', theme: 'bogus' }), { font: 32, line: 1.4, width: 720, theme: 'dark' });
  assert.deepEqual(Epub.preferences({ font: 15, line: 1.91, width: 597, theme: 'light' }), { font: 16, line: 2, width: 600, theme: 'light' });
});
test('chapter wrapper enforces CSP and trusted external CSS without inline styles or scripts', () => {
  const html = Epub.chapterDocument('<p>正文</p>', '</title><script>bad()</script>', { font: 24, theme: 'light' }, 7);
  assert.match(html, /default-src 'none'/); assert.match(html, /style-src 'self'/); assert.match(html, /img-src 'self'/);
  assert.match(html, /base-uri 'none'/); assert.match(html, /form-action 'none'/); assert.match(html, /href="\/epub-reader\.css"/);
  assert.doesNotMatch(html, /<script|<style|style=/i); assert.match(html, /&lt;\/title&gt;&lt;script&gt;/); assert.match(html, /data-font="24"/);
  const source = require('node:fs').readFileSync(require.resolve('../static/epub-reader.js'), 'utf8');
  assert.match(source, /sandbox="allow-same-origin"/); assert.doesNotMatch(source, /allow-scripts|allow-top-navigation|allow-popups/);
});
test('internal chapter links reject malformed indices and clamp fragments without treating external hrefs as navigation', () => {
  const anchor = new Element('a'); anchor.setAttribute('href', 'https://example.invalid'); assert.equal(Epub.linkTarget(anchor, 3), null);
  for (const index of ['-1', '1x', '03', '3', '', '1.5']) { anchor.dataset.epubIndex = index; assert.equal(Epub.linkTarget(anchor, 3), null); }
  anchor.dataset.epubIndex = '2'; anchor.dataset.epubFragment = 'hello\u0000world'; assert.deepEqual(Epub.linkTarget(anchor, 3), { page: 2, fragment: 'helloworld' });
});
test('EPUB position handles short chapters, overscroll, and invalid chapter routes', () => {
  assert.equal(Epub.scrollPosition({ scrollTop: 100, scrollHeight: 100, clientHeight: 100 }), 0);
  assert.equal(Epub.scrollPosition({ scrollTop: 100, scrollHeight: 300, clientHeight: 100 }), .5);
  assert.equal(Epub.scrollPosition({ scrollTop: -10, scrollHeight: 300, clientHeight: 100 }), 0);
  assert.equal(Epub.scrollPosition({ scrollTop: 400, scrollHeight: 300, clientHeight: 100 }), 1);
  assert.equal(Epub.chapterIndex(10, 3), 2); assert.equal(Epub.chapterIndex('bad', 3), 0);
});
test('opening restores chapter ratio and uses source revision while text titles remain text nodes', async () => {
  const f = fixture(); await f.reader.open(f.book, { page: 1, position: .4 }); f.loaded();
  assert.equal(f.requests[0].path, '/api/books/epub-id/epub/chapters/1?v=' + 'a'.repeat(24));
  assert.equal(f.reader.isOpen(), true); assert.equal(f.scroll.scrollTop, 800); assert.equal(f.reader.location().bookId, 'epub-id');
  assert.equal(f.nodes.get('[data-role="title"]').textContent, '<Untrusted title>');
  assert.equal(f.nodes.get('[data-role="toc"]').children[0].textContent, '<TOC title>'); assert.equal(f.routes.length, 1);
  await f.reader.close({ notify: false });
});
test('route restoration does not recursively emit a replacement route or refetch an already loaded chapter', async () => {
  const f = fixture(); await f.reader.open(f.book, { page: 1, position: .2, fromRoute: true }); f.loaded(); assert.equal(f.routes.length, 0);
  await f.reader.navigate(1, { position: .6, fromRoute: true }); f.animate(); assert.equal(f.requests.length, 1); assert.equal(f.routes.length, 0); assert.equal(f.scroll.scrollTop, 1200);
  await f.reader.navigate(1, { position: 0, fragment: 'note' }); f.animate(); assert.equal(f.scroll.scrollTop, 1300); assert.equal(f.routes.length, 1);
});
test('chapter navigation cancels obsolete fetches and ignores out-of-order responses', async () => {
  const pending = []; const f = fixture({ responder: () => new Promise(resolve => pending.push(resolve)) });
  const first = f.reader.open(f.book); await settle(); const second = f.reader.navigate(1);
  assert.equal(f.requests[0].options.signal.aborted, true);
  pending[1]({ html: '<p>Second</p>', title: 'Second' }); await second; f.loaded();
  pending[0]({ html: '<p>Obsolete</p>', title: 'Obsolete' }); await first; assert.match(f.frame.srcdoc, /Second/); assert.doesNotMatch(f.frame.srcdoc, /Obsolete/); assert.equal(f.reader.location().page, 1);
});
test('blank-document and stale loads cannot mark a chapter ready after navigation or close', async () => {
  const f = fixture(); await f.reader.open(f.book); const onload = f.frame.onload; const marker = f.frame.marker;
  f.frame.marker = undefined; f.loaded(); assert.equal(f.frame.hidden, true); assert.equal(f.nodes.get('[data-action="next"]').disabled, true);
  f.frame.marker = marker; f.loaded(); assert.equal(f.frame.hidden, false);
  await f.reader.close({ notify: false }); onload(); f.animate(); assert.equal(f.reader.isOpen(), false); assert.equal(f.frame.srcdoc, ''); assert.equal(f.doc.body.style.overflow, 'auto');
});
test('stale source response is displayed as a retryable error instead of rendering mismatched content', async () => {
  const f = fixture({ responder: async () => ({ html: '<p>changed</p>', source_version: 'b'.repeat(64) }) });
  await f.reader.open(f.book); assert.equal(f.frame.srcdoc, ''); assert.equal(f.nodes.get('[data-role="message"]').textContent, '作品已更新，请重新打开。'); assert.equal(f.nodes.get('[data-action="retry"]').hidden, false);
});
test('progress saves track scrolling and close waits for final captured position before notifying the host', async () => {
  let resolve; const f = fixture({ saveProgress: () => new Promise(done => { resolve = done; }) });
  await f.reader.open(f.book); f.loaded(); f.scrolled(1450); const closing = f.reader.close();
  assert.equal(f.reader.isOpen(), false); assert.equal(f.closed.length, 0); await settle();
  assert.deepEqual(f.saves[0].payload, { page: 0, position: .725, completed: false }); assert.equal(f.saves[0].options.keepalive, true);
  resolve(); await closing; assert.equal(f.closed.length, 1); assert.equal(f.closed[0].at.position, .725);
});
test('failed progress remains retryable and an authentication close discards the account pending retry', async () => {
  const f = fixture({ saveProgress: (book, payload, opts, count) => count === 1 ? Promise.reject(new Error('offline')) : Promise.resolve() });
  await f.reader.open(f.book); f.loaded(); f.scrolled(900); f.tick(); await settle();
  assert.equal(f.errors.length, 1); assert.equal(f.nodes.get('[data-role="save-status"]').disabled, false); assert.match(f.nodes.get('[data-role="save-status"]').textContent, /重试/);
  await f.reader.flush(); assert.equal(f.saves.length, 2); assert.equal(f.saves[1].payload.position, .45); assert.equal(f.nodes.get('[data-role="save-status"]').disabled, true);
  f.scrolled(1100); await f.reader.close({ save: false, notify: false }); await f.reader.flush(); assert.equal(f.saves.length, 2);
});
test('text preferences are read per account on every open and reflow keeps the current ratio', async () => {
  const f = fixture(); f.storage.set('user-a', JSON.stringify({ font: 24, line: 2, width: 880, theme: 'light' }));
  await f.reader.open(f.book, { position: .55 }); f.loaded(); assert.equal(f.scroll.dataset.font, '24'); assert.equal(f.dialog.dataset.theme, 'light');
  f.dialog.emit('click', { target: f.steps[1] }); f.animate(); assert.equal(f.scroll.dataset.font, '26'); assert.equal(f.reader.location().position, .55);
  await f.reader.close({ save: false, notify: false }); f.key('user-b'); await f.reader.open(f.book); f.loaded(); assert.equal(f.scroll.dataset.font, '20'); assert.equal(f.dialog.dataset.theme, 'dark');
});
test('chapter links are captured inside the sandbox and unsupported external links never navigate', async () => {
  const f = fixture(); await f.reader.open(f.book); f.loaded();
  const external = new Element('a'); external.setAttribute('href', 'https://example.invalid'); let prevented = 0;
  f.content.emit('click', { target: external, preventDefault() { prevented++; }, stopPropagation() {} }); assert.equal(prevented, 1); assert.equal(f.requests.length, 1);
  const internal = new Element('a'); internal.dataset.epubIndex = '1'; internal.dataset.epubFragment = 'note';
  f.content.emit('click', { target: internal, preventDefault() { prevented++; }, stopPropagation() {} }); await settle(); f.loaded();
  assert.equal(f.requests.length, 2); assert.equal(f.reader.location().page, 1); assert.equal(f.scroll.scrollTop, 1300);
});
test('bookmarks report the exact chapter position and final short chapter completion is stored as complete', async () => {
  const f = fixture(); await f.reader.open(f.book, { page: 2 }); f.loaded(); f.scrolled(600); f.click('bookmark'); assert.equal(f.bookmark.at.position, .3); assert.equal(f.bookmark.at.page, 2);
  f.scroll.scrollHeight = f.scroll.clientHeight; f.click('next'); await settle(); assert.deepEqual(f.saves.at(-1).payload, { page: 2, position: 1, completed: true });
  assert.equal(f.reader.location().completed, true); assert.equal(f.reader.location().position, 1); await f.reader.close({ notify: false }); assert.equal(f.saves.at(-1).payload.position, 1);
});
test('queued progress writes and failed retries never cross an account change', async () => {
  let resolveFirst; const f = fixture({ saveProgress: (book, payload, opts, count) => count === 1 ? new Promise(resolve => { resolveFirst = resolve; }) : Promise.resolve() });
  await f.reader.open(f.book); f.loaded(); f.scrolled(400); const first = f.reader.flush(); await settle(); assert.equal(f.saves.length, 1);
  f.scrolled(800); const queued = f.reader.flush(); await settle(); assert.equal(f.saves.length, 1);
  await f.reader.close({ save: false, notify: false }); f.key('user-b'); await f.reader.open(f.book); f.loaded(); f.scrolled(1200); await f.reader.flush();
  assert.equal(f.saves.length, 2); assert.equal(f.saves[1].payload.position, .6);
  resolveFirst(); await first; await queued; assert.equal(f.saves.length, 2); assert.equal(f.errors.length, 0);
});
test('a delayed close notification cannot return from a newly opened reader route', async () => {
  let resolve; const f = fixture({ saveProgress: () => new Promise(done => { resolve = done; }) });
  await f.reader.open(f.book); f.loaded(); const oldClose = f.reader.close(); await settle();
  await f.reader.open({ ...f.book, id: 'new-book' }); f.loaded(); resolve(); await oldClose; assert.equal(f.closed.length, 0); assert.equal(f.reader.location().bookId, 'new-book');
});
test('lazy chapter images restore the requested ratio as dimensions arrive without delaying chapter readiness', async () => {
  const f = fixture(); await f.reader.open(f.book, { position: .6 }); f.loaded();
  assert.equal(f.frame.hidden, false); assert.equal(f.scroll.scrollTop, 1200); assert.equal(f.nodes.get('[data-action="next"]').disabled, false);
  f.scroll.scrollHeight = 4600; f.images[0].emit('load'); f.animate(); assert.equal(f.scroll.scrollTop, 2400); assert.equal(f.reader.location().position, .6);
  f.scroll.scrollHeight = 5600; f.images[1].emit('error'); f.animate(); assert.equal(f.scroll.scrollTop, 3000); assert.equal(f.reader.location().position, .6); assert.equal(f.saves.length, 0);
});
test('manual wheel, touch, pointer or keyboard input stops image restoration and cancels already queued compensation', async () => {
  for (const input of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
    const f = fixture(); await f.reader.open(f.book, { position: .6 }); f.loaded();
    f.scroll.scrollHeight = 4600; f.images[0].emit('load');
    f.content.emit(input, { key: 'PageDown' }); f.scroll.scrollTop = 2700; f.animate(); assert.equal(f.scroll.scrollTop, 2700, input);
    f.scroll.scrollHeight = 5600; f.images[1].emit('load'); f.animate(); assert.equal(f.scroll.scrollTop, 2700, input);
  }
});
test('a direct manual scroll stops later image compensation and obsolete image listeners cannot move another chapter', async () => {
  const f = fixture(); await f.reader.open(f.book, { position: .6 }); f.loaded(); const obsolete = f.images[0].handlers.get('load');
  f.scrolled(1600); f.scroll.scrollHeight = 4600; f.images[1].emit('load'); f.animate(); assert.equal(f.scroll.scrollTop, 1600);
  await f.reader.navigate(1, { position: .2 }); f.loaded(); assert.equal(f.scroll.scrollTop, 800); f.scroll.scrollHeight = 5600; obsolete(); f.animate(); assert.equal(f.scroll.scrollTop, 800);
});
test('same-chapter route restoration updates pending image recovery to the new requested location', async () => {
  const f = fixture(); await f.reader.open(f.book, { position: .6 }); f.loaded();
  await f.reader.navigate(0, { position: .2, fromRoute: true }); f.animate(); f.scroll.scrollHeight = 4600; f.images[0].emit('load'); f.animate(); assert.equal(f.scroll.scrollTop, 800);
});
test('an authentication close discards failed saves even when the previous reader is already closed', async () => {
  const f = fixture({ saveProgress: () => Promise.reject(new Error('offline')) });
  await f.reader.open(f.book); f.loaded(); await f.reader.close(); assert.equal(f.saves.length, 1);
  await f.reader.close({ save: false, notify: false }); await f.reader.flush(); assert.equal(f.saves.length, 1);
});
test('a second host close invalidates an older close callback and waits for its captured save before reopening', async () => {
  let resolve; const f = fixture({ saveProgress: () => new Promise(done => { resolve = done; }) });
  await f.reader.open(f.book); f.loaded(); const originalClose = f.reader.close(); await settle();
  let waiting = true; const hostClose = f.reader.close({ notify: false, save: true }).then(() => { waiting = false; }); await settle(); assert.equal(waiting, true);
  resolve(); await originalClose; await hostClose; assert.equal(waiting, false); assert.equal(f.closed.length, 0);
  await f.reader.open(f.book); f.loaded(); assert.equal(f.reader.isOpen(), true);
});
test('reopening the saved completed final chapter keeps completion until the user chooses an earlier location', async () => {
  const f = fixture(); f.book.progress = { page: 2, position: 1, completed: true };
  await f.reader.open(f.book); f.loaded(); assert.equal(f.reader.location().completed, true); assert.equal(f.nodes.get('[data-action="next"]').disabled, true);
  await f.reader.close({ notify: false }); assert.deepEqual(f.saves.at(-1).payload, { page: 2, position: 1, completed: true });
  await f.reader.open(f.book, { page: 2, position: .5 }); f.loaded(); assert.equal(f.reader.location().completed, false);
});

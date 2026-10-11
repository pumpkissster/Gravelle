'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { defaults, normalize, paginate, catalogWindow, createQueue, createCache } = require('../static/performance-settings.js');
const Performance = require('../static/performance-settings.js');
const tick = () => new Promise(resolve => setImmediate(resolve));

test('device preferences accept bounded integers and reject invalid saved values', () => {
  assert.deepEqual(normalize(), defaults);
  assert.deepEqual(normalize({ webtoonStep: 20, coverConcurrency: 12, imageCacheLimit: 999, bookPageSize: 1, chapterPageSize: 0 }), { webtoonStep: 20, coverConcurrency: 12, imageCacheLimit: 999, bookPageSize: 1, chapterPageSize: 0 });
  assert.deepEqual(normalize({ webtoonStep: 0, coverConcurrency: 13, imageCacheLimit: -1, bookPageSize: 1.5, chapterPageSize: '20' }), defaults);
  [null, false, 7, 'invalid', []].forEach(value => assert.deepEqual(normalize(value), defaults));
});

test('valid number input saves immediately, keeps the same controls, and rejects empty or invalid edits', () => {
  const previousDocument = global.document; const controls = [];
  const makeElement = tag => ({ tag, children: [], handlers: {}, value: '', append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }, setAttribute() {}, addEventListener(event, handler) { this.handlers[event] = handler; }, checkValidity() { const value = Number(this.value); return String(this.value).trim() !== '' && value >= this.min && value <= this.max; }, reportValidity() { this.reported = true; } });
  const panel = makeElement('div'); panel.contains = element => controls.includes(element);
  global.document = { getElementById: () => panel, createElement: tag => { const element = makeElement(tag); if (tag === 'input') controls.push(element); return element; } };
  try {
    Performance.render(); const originalChildren = panel.children.slice();
    const input = controls.find(element => element.id === 'performance-bookPageSize');
    input.value = '12'; input.handlers.input(); assert.equal(Performance.get().bookPageSize, 12); assert.deepEqual(panel.children, originalChildren);
    global.document.activeElement = input; Performance.render(); assert.equal(controls.length, 5); assert.deepEqual(panel.children, originalChildren);
    input.value = ''; input.handlers.input(); assert.equal(Performance.get().bookPageSize, 12); input.handlers.blur(); assert.equal(input.value, 12);
    input.value = '1000'; input.handlers.change(); assert.equal(Performance.get().bookPageSize, 12); assert.equal(input.reported, true); assert.equal(input.value, 12);
    input.value = '1.5'; input.handlers.input(); assert.equal(Performance.get().bookPageSize, 12);
  } finally { global.document = previousDocument; Performance.update(defaults); }
});

test('pagination clamps after filtering and handles empty catalogs without invalid pages', () => {
  assert.deepEqual(paginate(26, 2, 10), { page: 2, count: 3, size: 10, start: 20, end: 26 });
  assert.deepEqual(paginate(3, 2, 10), { page: 0, count: 1, size: 10, start: 0, end: 3 });
  assert.deepEqual(paginate(0, -5, 0), { page: 0, count: 1, size: 1, start: 0, end: 0 });
});

test('zero capacity reveals a continuous catalog in growing batches without dropping earlier books', () => {
  const first = catalogWindow(53, 9, 0, 0, 20);
  assert.deepEqual(first, { page: 0, count: 1, size: 20, start: 0, end: 20, flow: true, hasMore: true });
  const second = catalogWindow(53, 9, 0, first.end + first.size, first.size);
  assert.equal(second.start, 0);
  assert.equal(second.end, 40);
  assert.equal(second.hasMore, true);
  const last = catalogWindow(53, 9, 0, second.end + second.size, second.size);
  assert.equal(last.start, 0);
  assert.equal(last.end, 53);
  assert.equal(last.hasMore, false);
});

test('continuous windows clamp at batch boundaries, empty catalogs, and smaller filtered results', () => {
  assert.deepEqual(catalogWindow(0, 5, 0, 80, 20), { page: 0, count: 1, size: 20, start: 0, end: 0, flow: true, hasMore: false });
  assert.equal(catalogWindow(20, 0, 0, 0, 20).hasMore, false);
  assert.equal(catalogWindow(21, 0, 0, 0, 20).hasMore, true);
  assert.equal(catalogWindow(5, 0, 0, 80, 20).end, 5);
  assert.equal(catalogWindow(5, 0, 0, 80, 20).hasMore, false);
});

test('positive catalog capacity retains pagination and clamps after filtering', () => {
  for (const [total, page, capacity] of [[26, 2, 10], [3, 2, 10], [0, 5, 10], [21, -1, 10]]) {
    assert.deepEqual(catalogWindow(total, page, capacity, 999, 20), { ...paginate(total, page, capacity), flow: false, hasMore: false });
  }
});

test('catalog windows normalize malformed numbers without exposing invalid ranges', () => {
  assert.deepEqual(catalogWindow(NaN, Infinity, -10, -1, NaN), { page: 0, count: 1, size: 24, start: 0, end: 0, flow: true, hasMore: false });
  assert.deepEqual(catalogWindow('26.9', '2.8', '10.9', 0, 20), { page: 2, count: 3, size: 10, start: 20, end: 26, flow: false, hasMore: false });
  assert.equal(catalogWindow(5, 0, 0, Infinity, 0).end, 1);
  assert.equal(catalogWindow(5, 0, 0, 3.9, -20).end, 3);
  assert.equal(catalogWindow(50, 0, 0, undefined).end, 24);
});

test('cover queue enforces concurrency, preserves order and releases failed loads', async () => {
  const queue = createQueue(2); const gates = []; const started = []; let running = 0; let peak = 0;
  const jobs = [0, 1, 2, 3].map(index => queue.add(() => new Promise((resolve, reject) => { started.push(index); running += 1; peak = Math.max(peak, running); gates[index] = () => { running -= 1; if (index === 1) reject(new Error('bad image')); else resolve(index); }; }), index));
  const settled = Promise.allSettled(jobs);
  await tick(); assert.deepEqual(started, [0, 1]); assert.equal(queue.stats().active, 2);
  gates[1](); await tick(); assert.deepEqual(started, [0, 1, 2]);
  gates[0](); await tick(); gates[2](); await tick(); gates[3]();
  const result = await settled; await tick(); assert.equal(result[1].status, 'rejected'); assert.equal(peak, 2); assert.deepEqual(queue.stats(), { active: 0, pending: 0, limit: 2 });
});

test('detached queued covers are cancelled and a changed limit starts waiting loads', async () => {
  const queue = createQueue(1); const gates = {}; const started = [];
  const task = key => queue.add(signal => new Promise(resolve => { started.push(key); gates[key] = resolve; signal.addEventListener('abort', () => resolve(false), { once: true }); }), key);
  const first = task('first'); const detached = task('detached'); const last = task('last');
  queue.cancel(key => key === 'detached'); assert.equal(await detached, false); await tick(); assert.deepEqual(started, ['first']);
  queue.setLimit(2); await tick(); assert.deepEqual(started, ['first', 'last']);
  queue.cancel(key => key === 'first'); assert.equal(await first, false); gates.last(true); assert.equal(await last, true); await tick(); assert.equal(queue.stats().active, 0);
});

test('image cache evicts old pages while protecting displayed spreads, and zero keeps all pages', () => {
  const evicted = []; const cache = createCache(2, (_value, key) => evicted.push(key));
  cache.set(0, 'cover'); cache.set(1, 'one'); cache.get(0); cache.set(2, 'two'); cache.trim([2]);
  assert.deepEqual(cache.keys(), [0, 2]); assert.deepEqual(evicted, [1]);
  cache.setLimit(1, [0, 2]); assert.equal(cache.size, 2, 'both visible pages survive even when the requested limit is one');
  cache.trim([2]); assert.deepEqual(cache.keys(), [2]);
  cache.setLimit(0); cache.set(3, 'three'); cache.set(4, 'four'); cache.trim(); assert.equal(cache.size, 3);
  cache.clear(); assert.equal(cache.size, 0); assert.deepEqual(evicted, [1, 0, 2, 3, 4]);
});

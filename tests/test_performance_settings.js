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

test('performance settings migrate legacy preferences only for the original administrator and isolate login changes', () => {
  const previousStorage = global.localStorage;
  const records = new Map([['sumi-performance', JSON.stringify({ ...defaults, bookPageSize: 25, imageCacheLimit: 8 })]]);
  global.localStorage = { getItem: key => records.get(key) ?? null, setItem: (key, value) => records.set(key, value) };
  try {
    Performance.setUser({ id: 2, role: 'user' }); assert.deepEqual(Performance.get(), defaults);
    Performance.update({ bookPageSize: 12 });
    assert.equal(JSON.parse(records.get('gravelle-user-2-sumi-performance')).bookPageSize, 12);
    Performance.setUser({ id: 1, role: 'admin' }); assert.equal(Performance.get().bookPageSize, 25); assert.equal(Performance.get().imageCacheLimit, 8);
    Performance.update({ bookPageSize: 30 });
    Performance.setUser({ id: 2, role: 'user' }); assert.equal(Performance.get().bookPageSize, 12); assert.equal(Performance.get().imageCacheLimit, 0);
    Performance.setUser(null); assert.deepEqual(Performance.get(), defaults);
    Performance.update({ bookPageSize: 99 });
    assert.equal(records.has('gravelle-user-signed-out-sumi-performance'), false);
    Performance.setUser({ id: 1, role: 'admin' }); assert.equal(Performance.get().bookPageSize, 30);
    records.set('gravelle-user-3-sumi-performance', '{broken'); Performance.setUser({ id: 3, role: 'user' }); assert.deepEqual(Performance.get(), defaults);
  } finally { Performance.setUser(null); global.localStorage = previousStorage; }
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
    global.document.activeElement = input; Performance.render(); assert.equal(controls.length, 4); assert.deepEqual(panel.children, originalChildren);
    assert.equal(controls.some(element => element.id === 'performance-chapterPageSize'), false);
    input.value = ''; input.handlers.input(); assert.equal(Performance.get().bookPageSize, 12); input.handlers.blur(); assert.equal(input.value, 12);
    input.value = '1000'; input.handlers.change(); assert.equal(Performance.get().bookPageSize, 12); assert.equal(input.reported, true); assert.equal(input.value, 12);
    input.value = '1.5'; input.handlers.input(); assert.equal(Performance.get().bookPageSize, 12);
  } finally { global.document = previousDocument; Performance.update(defaults); }
});

function performanceFixture() {
  const makeElement = tag => ({
    tag, children: [], handlers: {}, value: '', textContent: '', hidden: false, disabled: false,
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    setAttribute() {}, addEventListener(event, handler) { this.handlers[event] = handler; },
    checkValidity() { const value = Number(this.value); return String(this.value).trim() !== '' && Number.isInteger(value) && value >= this.min && value <= this.max; },
    reportValidity() { this.reported = true; }
  });
  const panel = makeElement('div');
  function find(predicate, root = panel) { if (predicate(root)) return root; for (const item of root.children) { const found = find(predicate, item); if (found) return found; } return null; }
  const controls = () => { const found = []; const walk = root => { if (root.tag === 'input') found.push(root); root.children.forEach(walk); }; walk(panel); return found; };
  panel.contains = element => Boolean(find(item => item === element));
  return { panel, controls, find, document: { activeElement: null, getElementById: () => panel, createElement: makeElement } };
}

async function withPerformanceFixture(run) {
  const previousDocument = global.document; const previousStorage = global.localStorage;
  const fixture = performanceFixture(); const records = new Map();
  global.document = fixture.document;
  global.localStorage = { getItem: key => records.get(key) ?? null, setItem: (key, value) => records.set(key, value) };
  try { await run(fixture, records); }
  finally { Performance.setUser(null); Performance.configureServer(null); global.document = previousDocument; global.localStorage = previousStorage; }
}

test('child accounts see four device controls without requesting or rendering server task settings', async () => withPerformanceFixture(async fixture => {
  let reads = 0; let writes = 0;
  Performance.configureServer({ api: async () => { reads += 1; }, post: async () => { writes += 1; }, isAdmin: () => false });
  Performance.setUser({ id: 2, role: 'user' }); Performance.render(); Performance.render(); await tick();
  assert.equal(fixture.panel.children.length, 1);
  assert.equal(fixture.panel.children[0].children[0].textContent, '加载与列表');
  assert.equal(fixture.controls().length, 4);
  assert.equal(fixture.find(item => item.id === 'performance-taskConcurrency'), null);
  assert.equal(fixture.find(item => item.id === 'performance-chapterPageSize'), null);
  assert.equal(Performance.get().chapterPageSize, 0, 'the stored compatibility preference remains available');
  assert.equal(reads, 0); assert.equal(writes, 0);
}));

test('administrator task controls load once, preserve unfinished edits, save explicitly and use the returned server value', async () => withPerformanceFixture(async (fixture, records) => {
  let resolveRead; let resolveSave; let reads = 0; const writes = [];
  Performance.configureServer({
    api: path => { assert.equal(path, '/api/performance'); reads += 1; return new Promise(resolve => { resolveRead = resolve; }); },
    post: (path, body) => { writes.push({ path, body }); return new Promise(resolve => { resolveSave = resolve; }); }, isAdmin: () => true
  });
  Performance.setUser({ id: 1, role: 'admin' }); Performance.render(); Performance.render(); await tick();
  assert.equal(fixture.panel.children.length, 2);
  assert.deepEqual(fixture.panel.children.map(card => card.children[0].textContent), ['后台任务', '加载与列表']);
  const input = fixture.find(item => item.id === 'performance-taskConcurrency');
  const form = fixture.find(item => item.tag === 'form');
  const save = fixture.find(item => item.type === 'submit');
  assert.equal(reads, 1); assert.equal(input.disabled, true); assert.equal(save.disabled, true); assert.equal(input.max, 8);
  resolveRead({ taskConcurrency: 3, maxTaskConcurrency: 8 }); await tick();
  assert.equal(input.value, 3); assert.equal(input.disabled, false);
  input.value = '4'; const originalChildren = fixture.panel.children.slice(); Performance.render();
  assert.deepEqual(fixture.panel.children, originalChildren); assert.equal(input.value, '4'); assert.equal(reads, 1); assert.equal(writes.length, 0);
  const saving = form.handlers.submit({ preventDefault() {} });
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(input.disabled, true); assert.equal(save.disabled, true); assert.equal(save.textContent, '保存中…');
  assert.deepEqual(writes, [{ path: '/api/performance', body: { taskConcurrency: 4 } }]);
  resolveSave({ taskConcurrency: 5, maxTaskConcurrency: 8 }); await saving;
  assert.equal(input.value, 5); assert.equal(save.disabled, false);
  assert.equal(fixture.find(item => item.className === 'performance-server-status').textContent, '已保存');
  Performance.update({ bookPageSize: 12 });
  assert.equal(JSON.parse(records.get('gravelle-user-1-sumi-performance')).bookPageSize, 12);
  assert.equal(Object.hasOwn(Performance.get(), 'taskConcurrency'), false);
  for (const value of records.values()) assert.equal(Object.hasOwn(JSON.parse(value), 'taskConcurrency'), false);
}));

test('failed server reads can retry and failed or invalid saves restore the last confirmed limit', async () => withPerformanceFixture(async fixture => {
  let reads = 0; let writes = 0; let failSave = true;
  Performance.configureServer({ api: async () => { reads += 1; if (reads === 1) throw new Error('服务暂时不可用'); return { taskConcurrency: 2, maxTaskConcurrency: 8 }; }, post: async (_path, body) => { writes += 1; if (failSave) throw new Error('无权保存'); return { ...body, maxTaskConcurrency: 8 }; }, isAdmin: () => true });
  Performance.setUser({ id: 1, role: 'admin' }); Performance.render(); await tick();
  const input = fixture.find(item => item.id === 'performance-taskConcurrency'); const form = fixture.find(item => item.tag === 'form');
  const retry = fixture.find(item => item.textContent === '重试');
  assert.equal(input.disabled, true); assert.equal(retry.hidden, false);
  assert.match(fixture.find(item => item.className?.includes('performance-server-status')).textContent, /读取后台任务设置失败：服务暂时不可用/);
  Performance.render(); await tick(); assert.equal(reads, 1, 'catalog refresh does not repeatedly retry a failed request');
  await retry.handlers.click(); assert.equal(input.value, 2); assert.equal(input.disabled, false); assert.equal(retry.hidden, true);
  input.value = '6'; await form.handlers.submit({ preventDefault() {} });
  assert.equal(input.value, 2); assert.equal(input.disabled, false);
  assert.match(fixture.find(item => item.className?.includes('performance-server-status')).textContent, /保存后台任务设置失败：无权保存/);
  for (const value of ['', '9', '1.5', '0']) {
    input.value = value; await form.handlers.submit({ preventDefault() {} }); assert.equal(input.value, 2);
  }
  assert.equal(writes, 1, 'invalid limits never reach the server');
  failSave = false; input.value = '8'; await form.handlers.submit({ preventDefault() {} }); assert.equal(input.value, 8);
}));

test('late administrator reads cannot populate another account and a queued read is cancelled on logout', async () => withPerformanceFixture(async fixture => {
  let allowed = true; const pending = []; let reads = 0;
  Performance.configureServer({ api: () => { reads += 1; return new Promise(resolve => pending.push(resolve)); }, post: async () => {}, isAdmin: () => allowed });
  Performance.setUser({ id: 1, role: 'admin' }); Performance.render(); await tick();
  allowed = false; Performance.setUser({ id: 2, role: 'user' }); Performance.render();
  pending.shift()({ taskConcurrency: 8, maxTaskConcurrency: 8 }); await tick();
  assert.equal(fixture.find(item => item.id === 'performance-taskConcurrency'), null); assert.equal(reads, 1);
  allowed = true; Performance.setUser({ id: 3, role: 'admin' }); Performance.render();
  Performance.setUser(null); Performance.render(); await tick();
  assert.equal(reads, 1, 'a request queued before logout must not be sent after it');
  Performance.setUser({ id: 4, role: 'admin' }); Performance.render(); await tick();
  const input = fixture.find(item => item.id === 'performance-taskConcurrency'); assert.equal(input.disabled, true);
  pending.shift()({ taskConcurrency: 4, maxTaskConcurrency: 8 }); await tick(); assert.equal(input.value, 4);
}));

test('a save response from the previous administrator cannot replace the next account settings', async () => withPerformanceFixture(async fixture => {
  let readCount = 0; let resolveSave;
  Performance.configureServer({ api: async () => ({ taskConcurrency: ++readCount === 1 ? 1 : 4, maxTaskConcurrency: 8 }), post: () => new Promise(resolve => { resolveSave = resolve; }), isAdmin: () => true });
  Performance.setUser({ id: 1, role: 'admin' }); Performance.render(); await tick();
  fixture.find(item => item.id === 'performance-taskConcurrency').value = '2';
  const saving = fixture.find(item => item.tag === 'form').handlers.submit({ preventDefault() {} });
  Performance.setUser({ id: 3, role: 'admin' }); Performance.render(); await tick();
  const currentInput = fixture.find(item => item.id === 'performance-taskConcurrency'); assert.equal(currentInput.value, 4);
  resolveSave({ taskConcurrency: 8, maxTaskConcurrency: 8 }); await saving;
  assert.equal(currentInput.value, 4); assert.equal(currentInput.disabled, false);
  assert.equal(fixture.find(item => item.className === 'performance-server-status').textContent, '');
}));

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

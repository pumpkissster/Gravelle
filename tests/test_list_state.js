'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createStore, normalizeSnapshot } = require('../static/list-state.js');

function storage() {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), values };
}
const sample = { x: 10, y: 2500, page: 3, revealed: 80, capacity: 24, anchor: { id: 'book-1', offset: -30 } };

test('a fresh store restores the same account and exact filtered route after reload', () => {
  const session = storage();
  const hash = '#/libraries/library-1?q=%E5%86%99%E7%9C%9F&sort=release';
  assert.equal(createStore({ storage: session, accountId: 'user-1' }).write(hash, sample), true);
  assert.deepEqual(createStore({ storage: session, accountId: 'user-1' }).read(hash), sample);
  assert.equal(createStore({ storage: session, accountId: 'user-1' }).read('#/libraries/library-1?sort=title'), null);
});

test('account keys isolate positions and encode special characters without collisions', () => {
  const session = storage();
  createStore({ storage: session, accountId: 'person:/1' }).write('#/home', sample);
  assert.equal(createStore({ storage: session, accountId: 'person%3A%2F1' }).read('#/home'), null);
  assert.equal(createStore({ storage: session, accountId: 'other' }).read('#/home'), null);
  assert.ok(session.values.has('gravelle-list-state:v1:person%3A%2F1'));
  const anonymous = createStore({ storage: session, accountId: null });
  anonymous.write('#/home', sample);
  assert.deepEqual(anonymous.read('#/home'), sample);
  assert.equal(createStore({ storage: session }).read('#/home'), null);
  assert.equal(session.values.size, 1);
});

test('writes retain the latest bounded routes and updating a route makes it newest', () => {
  const session = storage();
  const store = createStore({ storage: session, accountId: 'user', limit: 2 });
  store.write('#/home', sample);
  store.write('#/libraries', sample);
  store.write('#/home', { ...sample, y: 600 });
  store.write('#/recent', sample);
  assert.equal(store.read('#/libraries'), null);
  assert.equal(store.read('#/home').y, 600);
  const restored = createStore({ storage: session, accountId: 'user', limit: 2 });
  assert.equal(restored.read('#/libraries'), null);
  assert.equal(restored.read('#/home').y, 600);
  assert.deepEqual(restored.read('#/recent'), sample);
  const defaults = createStore();
  for (let index = 0; index < 41; index++) defaults.write('#/libraries/' + index, sample);
  assert.equal(defaults.read('#/libraries/0'), null);
  assert.deepEqual(defaults.read('#/libraries/40'), sample);
});

test('numbers and anchors are bounded, finite, copied and never carry unrelated data', () => {
  const value = normalizeSnapshot({ x: Infinity, y: 1e20, page: -3, revealed: 1e20, capacity: 5000, password: 'never save', image: 'data:image/png;base64,secret', anchor: { id: '作品', offset: -1e20, secret: 'never save' } });
  assert.deepEqual(value, { x: 0, y: 1e7, page: 0, revealed: 1e6, capacity: 999, anchor: { id: '作品', offset: -1e7 } });
  assert.deepEqual(normalizeSnapshot({ x: -2, y: '300', page: NaN, revealed: -1, capacity: 2.9, anchor: { id: 'x'.repeat(201), offset: Infinity } }), { x: 0, y: 0, page: 0, revealed: 0, capacity: 2 });
  assert.deepEqual(normalizeSnapshot({ anchor: { id: 'b', offset: NaN } }).anchor, { id: 'b', offset: 0 });
  for (const invalid of [null, undefined, false, 8, 'position', []]) assert.equal(normalizeSnapshot(invalid), null);
  const session = storage();
  const store = createStore({ storage: session, accountId: 'user' });
  const original = structuredClone(sample);
  store.write('#/home', original);
  original.anchor.id = 'changed';
  original.y = 0;
  const returned = store.read('#/home');
  returned.anchor.offset = 900;
  returned.y = 0;
  assert.deepEqual(store.read('#/home'), sample);
  assert.ok(![...session.values.values()].join('').includes('password'));
});

test('optimization filters survive reload with only bounded known string fields', () => {
  const session = storage();
  const hash = '#/settings/transcoding';
  const filters = { query: 'q'.repeat(600), status: 's'.repeat(60), library: 'l'.repeat(300), password: 'never-save', image: 'data:secret' };
  const store = createStore({ storage: session, accountId: 'user' });
  store.write(hash, { ...sample, filters });
  filters.query = 'changed';
  const restored = createStore({ storage: session, accountId: 'user' });
  const expected = { query: 'q'.repeat(500), status: 's'.repeat(50), library: 'l'.repeat(200) };
  const snapshot = restored.read(hash);
  assert.deepEqual(snapshot.filters, expected);
  snapshot.filters.status = 'changed';
  assert.deepEqual(restored.read(hash).filters, expected);
  assert.ok(![...session.values.values()].join('').includes('never-save'));
  assert.deepEqual(normalizeSnapshot({ filters: { query: null, status: 8, library: '' } }).filters, { library: '' });
  assert.equal(normalizeSnapshot({ filters: [] }).filters, undefined);
});

test('invalid routes and snapshots do not write or replace a valid position', () => {
  const session = storage();
  const store = createStore({ storage: session, accountId: 'user' });
  store.write('#/home', sample);
  for (const invalid of [null, undefined, 5, '', '/home', 'https://example.com/', '#/home' + 'x'.repeat(30000)]) {
    assert.equal(store.write(invalid, sample), false);
    assert.equal(store.read(invalid), null);
  }
  assert.equal(store.write('#/home', null), false);
  assert.deepEqual(store.read('#/home'), sample);
  assert.equal(session.values.size, 1);
});

test('denied, absent and full storage keep navigation working in memory', () => {
  const denied = { getItem() { throw Error('denied'); }, setItem() { throw Error('full'); } };
  for (const session of [undefined, null, denied, {}]) {
    const store = createStore({ storage: session, accountId: 'user' });
    assert.equal(store.read('#/home'), null);
    assert.equal(store.write('#/home', sample), true);
    assert.deepEqual(store.read('#/home'), sample);
  }
});

test('malformed persisted state is ignored and valid entries are sanitized and pruned', () => {
  const session = storage();
  const key = 'gravelle-list-state:v1:user';
  for (const malformed of ['{', 'null', '[]', '{"entries":{}}', '{"entries":[null,["/home",{}],["#/home",null]]}']) {
    session.values.set(key, malformed);
    assert.equal(createStore({ storage: session, accountId: 'user' }).read('#/home'), null);
  }
  session.values.set(key, JSON.stringify({ entries: [['#/old', sample], ['#/home', { ...sample, y: 8e20 }], ['#/recent', sample], ['#/home', sample]] }));
  const store = createStore({ storage: session, accountId: 'user', limit: 2 });
  assert.equal(store.read('#/old'), null);
  assert.deepEqual(store.read('#/home'), sample);
  assert.deepEqual(store.read('#/recent'), sample);
});

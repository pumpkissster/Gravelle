'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Routes = require('../static/page-router.js');

test('all navigation views have stable copyable routes', () => {
  for (const view of ['home', 'libraries', 'recent', 'bookmarks', 'favorite', 'search', 'models', 'settings']) {
    assert.deepEqual(Routes.parse(Routes.hash({ view })), { view });
  }
  for (const view of ['library', 'model', 'book', 'reader']) {
    assert.deepEqual(Routes.parse(Routes.hash({ view, id: '中文 空格#?&' })), { view, id: '中文 空格#?&' });
  }
});
test('search and sorting round-trip without interpreting user input as a path', () => {
  const route = { view: 'search', query: '加藤 & # / +?', sort: 'release', series: '特集' };
  assert.deepEqual(Routes.parse(Routes.hash(route)), route);
  assert.deepEqual(Routes.parse('#/search?q=a&password=secret&sort=bad'), { view: 'search', query: 'a' });
  assert.equal(Routes.hash(Routes.parse('#/search?username=person&password=secret')), '#/search');
});
test('reader keeps its page and detail return route with the original filtered page underneath', () => {
  const origin = { view: 'library', id: '2', query: '特集', sort: 'title' };
  const detail = { view: 'book', id: 'book-1', from: origin };
  const route = { view: 'reader', id: 'book-1', page: 4, from: detail };
  const parsed = Routes.parse(Routes.hash(route));
  assert.deepEqual(parsed, route);
  assert.deepEqual(Routes.returnTo(parsed), detail);
  assert.deepEqual(Routes.base(parsed), origin);
  assert.deepEqual(Routes.returnTo({ view: 'reader', id: 'book-1' }), { view: 'book', id: 'book-1' });
});
test('unknown, malformed, external and oversized route values safely fall back', () => {
  for (const input of ['', '#', '#/', '#/missing', '#/libraries/1/extra', '#/books', '#/read/%ZZ', '#/models/%2Fetc', '#//example.com', 'https://example.com', '#/settings/extra', '#/search?q=' + 'x'.repeat(30000)]) {
    assert.deepEqual(Routes.parse(input), { view: 'home' });
  }
  assert.deepEqual(Routes.parse('#/read/book?page=-1'), { view: 'reader', id: 'book' });
  assert.deepEqual(Routes.parse('#/read/book?page=3.5'), { view: 'reader', id: 'book' });
  assert.deepEqual(Routes.parse('#/read/book?page=999999999'), { view: 'reader', id: 'book' });
  assert.deepEqual(Routes.parse('#/books/book?from=https%3A%2F%2Fexample.com'), { view: 'book', id: 'book', from: { view: 'home' } });
});
test('bounded non-Latin search values survive nested reader return links', () => {
  const from = { view: 'search', query: '写'.repeat(500), series: '真'.repeat(500) };
  const route = { view: 'reader', id: 'b', page: 3, from: { view: 'book', id: 'b', from } };
  assert.deepEqual(Routes.parse(Routes.hash(route)), route);
});
test('return routes cannot recursively stack readers or infinitely nest details', () => {
  assert.deepEqual(Routes.normalize({ view: 'book', id: 'a', from: { view: 'book', id: 'b' } }), { view: 'book', id: 'a' });
  assert.deepEqual(Routes.normalize({ view: 'reader', id: 'a', from: { view: 'reader', id: 'b' } }), { view: 'reader', id: 'a' });
});
test('unavailable or unauthorized catalog targets never render an inaccessible page', () => {
  const catalog = { libraries: [{ id: 1 }], models: [{ id: 'm' }], books: [{ id: 'b' }] };
  for (const [view, target] of [['library', 'libraries'], ['model', 'models']]) {
    assert.deepEqual(Routes.resolve({ view, id: 'missing' }, catalog), { route: { view: target }, unavailable: true });
  }
  const route = { view: 'book', id: 'b', from: { view: 'library', id: 'missing' } };
  assert.deepEqual(Routes.resolve(route, catalog), { route: { view: 'book', id: 'b', from: { view: 'libraries' } }, unavailable: false });
});
test('hidden library work links are delegated to server authorization instead of rejected by visible catalog', () => {
  const catalog = { libraries: [], models: [], books: [] };
  for (const view of ['book', 'reader']) {
    const route = { view, id: 'authorized-but-personally-hidden' };
    assert.deepEqual(Routes.resolve(route, catalog), { route, unavailable: false });
  }
});

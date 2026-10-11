'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeSort, compareBooks, libraryBooks, nextBook, readingSequence, nextInSequence, recommendations, canFinish, atScrollBottom } = require('../static/reader-end.js');
const books = [
  { id: 'a', library_id: 1, title: '写真 10', added_at: 10, modified: 3, release_date: '2026-05-10', page_count: 4, progress: { updated_at: 20 } },
  { id: 'b', library_id: '1', title: '写真 2', added_at: 30, modified: 1, release_date: '2026-01-01', page_count: 2 },
  { id: 'c', library_id: 1, title: '写真 1', added_at: 20, modified: 2, release_date: '2026-06-10', page_count: 3, progress: { updated_at: 10 } },
  { id: 'foreign', library_id: 2, title: '外部', added_at: 99, page_count: 1 },
  { id: 'empty', library_id: 1, title: '空', added_at: 99, page_count: 0 }
];
test('all six catalogue sorts produce the same full-library reading sequence', () => {
  const cases = { added: ['b', 'c', 'a'], added_asc: ['a', 'c', 'b'], title: ['c', 'b', 'a'], updated: ['a', 'c', 'b'], release: ['c', 'a', 'b'], modified: ['a', 'c', 'b'] };
  for (const [sort, ids] of Object.entries(cases)) {
    assert.deepEqual(libraryBooks(books, '1', sort).map(book => book.id), ids);
    assert.equal(nextBook(books, books.find(book => book.id === ids[0]), sort).id, ids[1]);
  }
  assert.deepEqual(books.map(book => book.id), ['a', 'b', 'c', 'foreign', 'empty']);
});
test('equal metadata sorts fall back to natural names and retain stable equal-title order', () => {
  const equal = [{ id: 1, library_id: 1, page_count: 1, title: '写真 10' }, { id: 2, library_id: 1, page_count: 1, title: '写真 2', added_at: 0 }, { id: 3, library_id: 1, page_count: 1, title: '写真 2' }];
  for (const sort of ['added', 'added_asc', 'release', 'modified', 'updated']) assert.deepEqual(libraryBooks(equal, 1, sort).map(book => book.id), [2, 3, 1]);
  assert.equal(compareBooks(equal[1], equal[2], 'title'), 0);
});
test('next never wraps, crosses libraries, includes empty folders or guesses a missing current book', () => {
  assert.equal(nextBook(books, books[0], 'added'), null);
  assert.equal(nextBook(books, { id: 'missing', library_id: 1 }), null);
  assert.equal(nextBook(books, null), null);
  assert.equal(nextBook(books, books[3]), null);
  assert.equal(normalizeSort('bad'), 'added');
});
test('recommendations stay in the library, exclude current and cap at six', () => {
  assert.deepEqual(recommendations(books, books[2], 'added').map(book => book.id), ['a', 'b']);
  const many = Array.from({ length: 12 }, (_, id) => ({ id, title: String(id), library_id: 1, page_count: 1 }));
  assert.equal(recommendations(many, many[0], 'title', 99).length, 6);
  assert.deepEqual(recommendations(many, many[0], 'title', 0), []);
  assert.deepEqual(recommendations(books, books[3]), []);
});
test('reading chains keep the original library order when progress changes and skip removed books', () => {
  const sequence = readingSequence(books, books[0], 'updated');
  const changed = books.map(book => ({ ...book, progress: { updated_at: book.id === 'c' ? 100 : 0 } }));
  assert.equal(nextInSequence(changed, books[0], sequence).id, 'c');
  assert.equal(nextInSequence(changed, books[2], sequence).id, 'b');
  assert.equal(nextInSequence(changed.filter(book => book.id !== 'c'), books[0], sequence).id, 'b');
  assert.equal(nextInSequence(changed, books[1], sequence), null);
  assert.equal(nextInSequence(changed, books[3], sequence), null);
});
test('paged finish requires the displayed final single page or spread to have loaded', () => {
  assert.equal(canFinish({ pageCount: 5, loadedPage: 4, pages: [4] }), true);
  assert.equal(canFinish({ mode: 'double', pageCount: 5, loadedPage: 4, pages: [3, 4] }), true);
  assert.equal(canFinish({ mode: 'double', pageCount: 5, loadedPage: 4, pages: [2, 3] }), false);
  assert.equal(canFinish({ pageCount: 5, loadedPage: null, pages: [4] }), false);
  assert.equal(canFinish({ pageCount: 5, loadedPage: 3, pages: [4] }), false);
  assert.equal(canFinish({ pageCount: 1, loadedPage: 0, pages: [0] }), true);
  assert.equal(canFinish({ pageCount: 0, loadedPage: 0, pages: [0] }), false);
});
test('webtoon finish requires loaded final image, physical bottom and no restoration', () => {
  const boundary = { mode: 'webtoon', pageCount: 5, loadedPage: 4, atBottom: true };
  assert.equal(canFinish(boundary), true);
  assert.equal(canFinish({ ...boundary, atBottom: false }), false);
  assert.equal(canFinish({ ...boundary, restoring: 4 }), false);
  assert.equal(canFinish({ ...boundary, loadedPage: 3 }), false);
  assert.equal(atScrollBottom({ scrollTop: 199, clientHeight: 800, scrollHeight: 1000 }), true);
  assert.equal(atScrollBottom({ scrollTop: 195, clientHeight: 800, scrollHeight: 1000 }), false);
  assert.equal(atScrollBottom({ scrollTop: 0, clientHeight: 800, scrollHeight: 800 }), true);
  assert.equal(atScrollBottom({}), false);
});

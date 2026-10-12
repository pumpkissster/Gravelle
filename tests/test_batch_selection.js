'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Batch = require('../static/batch-selection.js');

test('model and search batch administration never becomes available to child accounts', () => {
  for (const view of ['model', 'search']) {
    assert.equal(Batch.kind(view, false), null);
    assert.deepEqual(Batch.actions(view, false), []);
    assert.equal(Batch.kind(view, true), 'books');
    assert.deepEqual(Batch.actions(view, true), ['models', 'remove']);
  }
  assert.deepEqual(Batch.actions('library', false), ['favorite', 'random']);
  assert.deepEqual(Batch.actions('library', true), ['remove', 'favorite', 'random']);
  assert.equal(Batch.kind('libraries', false), null);
  assert.deepEqual(Batch.actions('home', true), []);
});

test('selection survives pagination but drops results outside the current filter after edits', () => {
  const selected = new Set(['1', '2', '3']);
  assert.deepEqual([...Batch.retain(selected, [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4 }])], ['1', '2', '3']);
  assert.deepEqual([...Batch.retain(selected, [{ id: 2 }, { id: 4 }])], ['2']);
  assert.deepEqual([...selected], ['1', '2', '3']);
  assert.deepEqual([...Batch.retain(selected, [])], []);
});

test('bulk replacement only preselects identical associations and never assigns a union to unrelated works', () => {
  const books = [
    { models: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] },
    { models: [{ id: 'c' }, { id: 'a' }] },
    { models: [{ id: 'a' }, { id: 'd' }] }
  ];
  assert.deepEqual([...Batch.initialModelIds(books)], []);
  assert.deepEqual([...Batch.initialModelIds([books[0], {}])], []);
  assert.deepEqual([...Batch.initialModelIds([])], []);
  assert.deepEqual([...Batch.initialModelIds([books[0]])], ['a', 'b', 'c']);
  assert.deepEqual([...Batch.initialModelIds([books[0], { models: [{ id: 'c' }, { id: 'a' }, { id: 'b' }] }])], ['a', 'b', 'c']);
});

'use strict';

// Run with: node --test tests/test_reader_model.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSpreads, spreadForPage, visualPages, effectiveMode } = require('../static/reader-model.js');

test('default spreads preserve a standalone cover and logical page order', () => {
  assert.deepEqual(buildSpreads(0), []);
  assert.deepEqual(buildSpreads(1), [[0]]);
  assert.deepEqual(buildSpreads(6), [[0], [1, 2], [3, 4], [5]]);
  assert.deepEqual(buildSpreads(7), [[0], [1, 2], [3, 4], [5, 6]]);
});

test('cover pairing and offset alter only the intended body grouping', () => {
  assert.deepEqual(buildSpreads(6, { coverAlone: false }), [[0, 1], [2, 3], [4, 5]]);
  assert.deepEqual(buildSpreads(6, { coverAlone: true, offset: true }), [[0], [1], [2, 3], [4, 5]]);
  assert.deepEqual(buildSpreads(6, { coverAlone: false, offset: true }), [[0], [1, 2], [3, 4], [5]]);
});

test('landscape pages stand alone and pairing resumes afterward', () => {
  const pages = [
    { width: 800, height: 1200 }, { width: 800, height: 1200 },
    { width: 1600, height: 1200 }, { width: 800, height: 1200 },
    { width: 800, height: 1200 }, { width: 1600, height: 1200 },
    { width: 800, height: 1200 },
  ];
  assert.deepEqual(buildSpreads(pages), [[0], [1], [2], [3, 4], [5], [6]]);
});

test('consecutive wide pages and browser image dimensions are handled', () => {
  const pages = [
    { naturalWidth: 1300, naturalHeight: 1000 },
    { naturalWidth: 1300, naturalHeight: 1000 },
    { naturalWidth: 700, naturalHeight: 1000 },
    { naturalWidth: 700, naturalHeight: 1000 },
  ];
  assert.deepEqual(buildSpreads(pages, { coverAlone: false }), [[0], [1], [2, 3]]);
});

test('wide threshold is inclusive and configurable', () => {
  const pages = [{ width: 1150, height: 1000 }, { width: 700, height: 1000 }];
  assert.deepEqual(buildSpreads(pages, { coverAlone: false }), [[0], [1]]);
  assert.deepEqual(buildSpreads(pages, { coverAlone: false, wideRatio: 1.2 }), [[0, 1]]);
});

test('every logical page appears once across cover and offset combinations', () => {
  for (let count = 0; count < 25; count += 1) {
    for (const coverAlone of [false, true]) {
      for (const offset of [false, true]) {
        const spreads = buildSpreads(count, { coverAlone, offset });
        assert.deepEqual(spreads.flat(), Array.from({ length: count }, (_, index) => index));
        assert.ok(spreads.every(spread => spread.length >= 1 && spread.length <= 2));
      }
    }
  }
});

test('progress anchored to either half of a pair resolves to the same spread', () => {
  const spreads = [[0], [1, 2], [3], [4, 5]];
  assert.equal(spreadForPage(spreads, 0), 0);
  assert.equal(spreadForPage(spreads, 1), 1);
  assert.equal(spreadForPage(spreads, 2), 1);
  assert.equal(spreadForPage(spreads, 3), 2);
  assert.equal(spreadForPage(spreads, 5), 3);
  assert.equal(spreadForPage(spreads, -10), 0);
  assert.equal(spreadForPage(spreads, 999), 3);
});

test('RTL reverses display position without mutating logical progress order', () => {
  const spread = [3, 4];
  const rtl = visualPages(spread, 'rtl');
  assert.deepEqual(rtl, [4, 3]);
  assert.deepEqual(visualPages(spread, 'ltr'), [3, 4]);
  assert.deepEqual(visualPages([0], 'rtl'), [0]);
  assert.deepEqual(spread, [3, 4]);
  assert.notEqual(rtl, spread);
});

test('double-page mode needs both sufficient width and a landscape viewport', () => {
  assert.equal(effectiveMode('double', 1280, 800), 'double');
  assert.equal(effectiveMode('double', 779, 500), 'single');
  assert.equal(effectiveMode('double', 1024, 1366), 'single');
  assert.equal(effectiveMode('double', 900, 800), 'single');
  assert.equal(effectiveMode('double', 780, 600), 'double');
});

test('single and webtoon choices survive viewport changes', () => {
  for (const dimensions of [[390, 844], [1366, 768], [800, 1280]]) {
    assert.equal(effectiveMode('single', ...dimensions), 'single');
    assert.equal(effectiveMode('webtoon', ...dimensions), 'webtoon');
  }
});

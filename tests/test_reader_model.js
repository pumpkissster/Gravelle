'use strict';

// Run with: node --test tests/test_reader_model.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSpreads, spreadForPage, visualPages, effectiveMode, effectiveDirection, canScrollPage, createWheelPager, swipePageDelta, pageDimensions, pdfPageWidth, pageSource } = require('../static/reader-model.js');

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

test('vertical page direction belongs to single mode and preserves the stored choice across modes', () => {
  for (const stored of ['ltr', 'rtl', 'ttb']) assert.equal(effectiveDirection('webtoon', stored), 'ttb');
  assert.equal(effectiveDirection('single', 'ttb'), 'ttb');
  assert.equal(effectiveDirection('double', 'ttb'), 'ltr');
  assert.equal(effectiveDirection('double', 'rtl'), 'rtl');
  assert.equal(effectiveDirection('single', 'invalid'), 'ltr');
  // The selected mode, rather than a narrow viewport's single-page fallback,
  // controls direction: double remains horizontal on phones.
  assert.equal(effectiveMode('double', 390, 844), 'single');
  assert.equal(effectiveDirection('double', 'ttb'), 'ltr');
  assert.deepEqual(visualPages([2], 'ttb'), [2]);
});

test('tall pages can pan within their boundaries before a fresh gesture turns', () => {
  const viewport = { scrollTop: 0, scrollHeight: 1800, clientHeight: 600 };
  assert.equal(canScrollPage(1, viewport), true);
  assert.equal(canScrollPage(-1, viewport), false);
  viewport.scrollTop = 1199;
  assert.equal(canScrollPage(1, viewport), false);
  assert.equal(canScrollPage(-1, viewport), true);
  assert.equal(canScrollPage(1, { scrollTop: -2, scrollHeight: 600, clientHeight: 600 }), false);
});

test('wheel momentum turns at most one page until a new gesture', () => {
  const wheel = createWheelPager();
  assert.equal(wheel.next(15, 0), 0);
  assert.equal(wheel.next(30, 12), 1);
  for (const time of [28, 70, 120, 270]) assert.equal(wheel.next(80, time), 0);
  assert.equal(wheel.next(-60, 500), -1);
  wheel.reset();
  assert.equal(wheel.next(60, 510), 1);
});

test('scrolling or loading consumes the current wheel gesture without an edge jump', () => {
  const wheel = createWheelPager();
  assert.equal(wheel.next(120, 0, { scrolling: true }), 0);
  assert.equal(wheel.next(120, 40), 0);
  assert.equal(wheel.next(120, 90), 0);
  assert.equal(wheel.next(120, 300), 1);
  assert.equal(wheel.next(120, 600, { blocked: true }), 0);
  assert.equal(wheel.next(120, 680), 0);
  assert.equal(wheel.next(120, 900), 1);
});

test('vertical swipes distinguish a page turn from horizontal movement, panning and pinch', () => {
  assert.equal(swipePageDelta({ dx: 5, dy: -100, vertical: true }), 1);
  assert.equal(swipePageDelta({ dx: 5, dy: 100, vertical: true }), -1);
  assert.equal(swipePageDelta({ dx: -100, dy: 5, vertical: true }), 0);
  assert.equal(swipePageDelta({ dx: 70, dy: -80, vertical: true }), 0);
  assert.equal(swipePageDelta({ dx: 0, dy: -50, vertical: true }), 0);
  assert.equal(swipePageDelta({ dx: 0, dy: -100, vertical: true, blocked: true }), 0);
  assert.equal(swipePageDelta({ dx: -100, dy: 5, direction: 'rtl' }), -1);
  assert.equal(swipePageDelta({ dx: -100, dy: 5, direction: 'ltr' }), 1);
});

test('PDF resolution follows the displayed page on phones, HiDPI desktops and double spreads', () => {
  const portrait = [595, 842];
  assert.equal(pdfPageWidth({ width: 390, height: 680, pixelRatio: 3, size: portrait }), 1600);
  assert.equal(pdfPageWidth({ width: 390, height: 680, pixelRatio: 1, size: portrait }), 800);
  assert.equal(pdfPageWidth({ width: 1920, height: 1000, pixelRatio: 2, size: portrait }), 1600);
  assert.equal(pdfPageWidth({ width: 1920, height: 1000, pixelRatio: 2, size: portrait, fit: 'width' }), 4096);
  assert.equal(pdfPageWidth({ width: 1920, height: 1000, pixelRatio: 2, pages: 2, size: portrait, fit: 'width' }), 2400);
  assert.equal(pdfPageWidth({ width: 390, height: 680, pixelRatio: 3, size: portrait, scroll: true }), 1600);
  assert.equal(pdfPageWidth({ width: 9000, height: 9000, pixelRatio: 10, fit: 'width' }), 4096);
});

test('PDF page metadata gives double-page grouping before any image is downloaded', () => {
  const sizes = [[595, 842], [1190, 842], [595, 842], [595, 842]];
  assert.deepEqual(buildSpreads(sizes.map(pageDimensions)), [[0], [1], [2, 3]]);
  for (const input of [null, [], [0, 40], [Infinity, 40], { width: 10, height: NaN }]) assert.equal(pageDimensions(input), null);
});

test('PDF dimensions and source revisions use distinct browser cache URLs while image URLs stay unchanged', () => {
  const pdf = { id: 'one/two', format: 'pdf', page_revision: 'a'.repeat(24) };
  assert.equal(pageSource(pdf, 5), '/api/books/one%2Ftwo/pages/5?width=1600&v=' + 'a'.repeat(24));
  assert.notEqual(pageSource(pdf, 5, 800), pageSource(pdf, 5, 2400));
  assert.notEqual(pageSource(pdf, 5, 800), pageSource({ ...pdf, page_revision: 'b'.repeat(24) }, 5, 800));
  for (const format of ['folder', 'zip', 'cbz']) assert.equal(pageSource({ ...pdf, format }, 5, 2400), '/api/books/one%2Ftwo/pages/5');
  assert.equal(pageSource({ id: 'list-pdf', format: 'pdf', modified: 10, size: 500 }, 1, 800), '/api/books/list-pdf/pages/1?width=800');
  assert.equal(pageSource({ ...pdf, page_revision: 'bad:old' }, 1, 800), '/api/books/one%2Ftwo/pages/1?width=800');
});

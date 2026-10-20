/* Logical page groups are independent of their physical display direction. */
(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  else root.SumiReaderModel = model;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function buildSpreads(pages, options = {}) {
    const metadata = Array.isArray(pages) ? pages : Array.from({ length: Math.max(0, Math.floor(Number(pages) || 0)) }, () => ({}));
    const groups = [];
    const cover = options.coverAlone !== false;
    const threshold = options.wideRatio || 1.15;
    const wide = page => {
      const item = metadata[page] || {};
      const width = Number(item.width || item.naturalWidth);
      const height = Number(item.height || item.naturalHeight);
      return width > 0 && height > 0 && width / height >= threshold;
    };
    let page = 0;
    if (metadata.length && cover) { groups.push([page]); page += 1; }
    if (page < metadata.length && options.offset) { groups.push([page]); page += 1; }
    while (page < metadata.length) {
      if (wide(page) || page + 1 >= metadata.length || wide(page + 1)) groups.push([page++]);
      else { groups.push([page, page + 1]); page += 2; }
    }
    return groups;
  }
  function spreadForPage(spreads, page) {
    if (!spreads.length) return -1;
    const index = spreads.findIndex(spread => spread.includes(Number(page)));
    if (index >= 0) return index;
    return Number(page) < spreads[0][0] ? 0 : spreads.length - 1;
  }
  function visualPages(spread, direction) { return direction === 'rtl' ? [...spread].reverse() : [...spread]; }
  function effectiveMode(mode, width, height) {
    if (mode !== 'double') return mode === 'webtoon' ? 'webtoon' : 'single';
    return Number(width) >= 780 && Number(width) / Math.max(1, Number(height)) >= 1.15 ? 'double' : 'single';
  }
  function effectiveDirection(mode, direction) {
    if (mode === 'webtoon') return 'ttb';
    if (direction === 'ttb') return mode === 'single' ? 'ttb' : 'ltr';
    return direction === 'rtl' ? 'rtl' : 'ltr';
  }
  function canScrollPage(delta, viewport = {}) {
    const top = Math.max(0, Number(viewport.scrollTop) || 0);
    const end = Math.max(0, (Number(viewport.scrollHeight) || 0) - (Number(viewport.clientHeight) || 0));
    return delta > 0 ? top < end - 2 : delta < 0 && top > 2;
  }
  // Trackpad momentum belongs to the gesture that started it. A gesture used to
  // scroll inside a tall page cannot also advance to the following page.
  function createWheelPager({ idleMs = 180, threshold = 40 } = {}) {
    let lastAt = null; let consumed = false; let distance = 0;
    return {
      reset() { lastAt = null; consumed = false; distance = 0; },
      next(delta, now, { scrolling = false, blocked = false } = {}) {
        if (lastAt === null || now - lastAt > idleMs) { consumed = false; distance = 0; }
        lastAt = now;
        if (scrolling || blocked) { consumed = true; return 0; }
        if (consumed || !Number.isFinite(delta)) return 0;
        distance += delta;
        if (Math.abs(distance) < threshold) return 0;
        consumed = true;
        return Math.sign(distance);
      }
    };
  }
  function swipePageDelta({ dx, dy, vertical = false, direction = 'ltr', blocked = false } = {}) {
    if (blocked) return 0;
    const primary = vertical ? dy : dx; const cross = vertical ? dx : dy;
    if (!Number.isFinite(primary) || !Number.isFinite(cross) || Math.abs(primary) <= 55 || Math.abs(primary) <= Math.abs(cross) * 1.5) return 0;
    return -Math.sign(primary) * (!vertical && direction === 'rtl' ? -1 : 1);
  }
  const pdfWidths = Object.freeze([800, 1600, 2400, 3200, 4096]);
  function pageDimensions(value) {
    const width = Number(Array.isArray(value) ? value[0] : value?.width);
    const height = Number(Array.isArray(value) ? value[1] : value?.height);
    return Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0 ? { width, height } : null;
  }
  function pdfPageWidth({ width, height, pixelRatio = 1, pages = 1, fit = 'height', scroll = false, size } = {}) {
    const available = Math.max(1, Number(width) || 800) / Math.max(1, Number(pages) || 1);
    const dimensions = pageDimensions(size);
    const display = !scroll && fit === 'height' && dimensions && Number(height) > 0 ? Math.min(available, Number(height) * dimensions.width / dimensions.height) : available;
    const ratio = Number.isFinite(Number(pixelRatio)) && Number(pixelRatio) > 0 ? Number(pixelRatio) : 1;
    const target = display * ratio;
    return pdfWidths.find(value => value >= target) || pdfWidths.at(-1);
  }
  function pageSource(book, index, width = 1600) {
    const base = '/api/books/' + encodeURIComponent(book.id) + '/pages/' + index;
    if (book.format !== 'pdf') return base;
    const bucket = pdfWidths.find(value => value >= Number(width)) || pdfWidths.at(-1);
    const revision = typeof book.page_revision === 'string' && /^[a-f0-9]{24}$/.test(book.page_revision) ? book.page_revision : '';
    return base + '?width=' + bucket + (revision ? '&v=' + encodeURIComponent(revision) : '');
  }
  return { buildSpreads, spreadForPage, visualPages, effectiveMode, effectiveDirection, canScrollPage, createWheelPager, swipePageDelta, pageDimensions, pdfPageWidth, pageSource };
}));

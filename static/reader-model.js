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
  return { buildSpreads, spreadForPage, visualPages, effectiveMode };
}));

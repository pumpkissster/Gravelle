/* Logical page groups are independent of their physical display direction. */
(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  else root.GravelleReaderModel = model;
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
  function swipePageDelta({ dx, dy, vertical = false, direction = 'ltr', blocked = false, threshold = 55 } = {}) {
    if (blocked) return 0;
    const primary = vertical ? dy : dx; const cross = vertical ? dx : dy;
    if (!Number.isFinite(primary) || !Number.isFinite(cross) || Math.abs(primary) <= threshold || Math.abs(primary) <= Math.abs(cross) * 1.5) return 0;
    return -Math.sign(primary) * (!vertical && direction === 'rtl' ? -1 : 1);
  }
  function animatePageOffset(surface, from, to, duration, hold = false) {
    if (surface.ownerDocument?.defaultView?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) return null;
    return surface.animate?.([{ translate: `${from}px` }, { translate: `${to}px` }], {
      duration, easing: 'cubic-bezier(.22,.61,.36,1)', fill: hold ? 'forwards' : 'none',
    }) || null;
  }
  function bindMousePager(viewport, surface, getContext, turnPage, previewPage) {
    let origin = null;
    let suppressClick = false;
    let returnAnimation = null;
    let neighborReturn = null;
    let activePreview = null;
    const previews = new Map();
    function positionPreview(preview, offset = 0) {
      preview.layer.style.translate = `${preview.rest + offset}px`;
      preview.layer.toggleAttribute('data-drag-active', Boolean(offset));
    }
    function measurePreview(preview) {
      const { layer, side } = preview;
      const bounds = layer.getBoundingClientRect();
      // Start the picture itself at the edge, including in fit-to-height mode.
      const inset = Math.max(0, side > 0 ? layer.firstElementChild.getBoundingClientRect().left - bounds.left
        : bounds.right - layer.lastElementChild.getBoundingClientRect().right);
      preview.rest = side * (bounds.width - inset);
    }
    function resetVisual(clear = true) {
      returnAnimation?.cancel();
      neighborReturn?.cancel();
      returnAnimation = neighborReturn = null;
      surface.style.translate = '';
      if (!clear) for (const preview of previews.values()) positionPreview(preview);
      activePreview = null;
      if (clear) {
        for (const preview of previews.values()) preview.layer.remove();
        previews.clear();
      }
    }
    function settle(offset) {
      surface.style.translate = '';
      if (offset) returnAnimation = animatePageOffset(surface, offset, 0, 180);
      if (!activePreview) return;
      const preview = activePreview;
      activePreview = null;
      const rest = preview.rest;
      positionPreview(preview);
      if (!offset) return;
      const animation = animatePageOffset(preview.layer, rest + offset, rest, 180);
      if (animation) {
        preview.layer.setAttribute('data-drag-active', '');
        neighborReturn = animation;
        animation.finished.then(() => {
          if (neighborReturn !== animation) return;
          neighborReturn = null;
          preview.layer.removeAttribute('data-drag-active');
        }, () => {});
      }
    }
    function prepareSide(side, direction) {
      const known = previews.get(side);
      if (known) {
        if (!known.ready || Array.from(known.layer.children).every(image => image.naturalWidth)) return known;
        known.layer.remove();
        previews.delete(side);
      }
      const images = previewPage(side * (direction === 'rtl' ? -1 : 1));
      if (!images) return null;
      const layer = surface.ownerDocument.createElement('div');
      layer.className = 'page-image-wrap page-turn-layer reader-drag-neighbor';
      layer.setAttribute('aria-hidden', 'true');
      layer.inert = true;
      const preview = { layer, side, rest: side * viewport.clientWidth, ready: false };
      previews.set(side, preview);
      positionPreview(preview);
      surface.after(layer);
      Promise.resolve(images).then(async pages => {
        if (!pages.length || previews.get(side) !== preview) return;
        await Promise.all(pages.map(image => image.decode?.()));
        if (previews.get(side) !== preview || pages.some(image => !image.naturalWidth)) return;
        layer.dataset.pages = pages.length;
        layer.replaceChildren(...pages.map(image => surface.contains(image) ? image.cloneNode(false) : image));
        measurePreview(preview);
        preview.ready = true;
        positionPreview(preview);
        if (origin && activePreview === preview && currentContext(origin)) slide(origin, origin.distance);
      }).catch(() => {});
      return preview;
    }
    function prepare() {
      const context = previewPage && getContext();
      if (context) for (const side of [-1, 1]) prepareSide(side, context.direction);
    }
    function slide(start, distance) {
      start.distance = distance;
      if (previewPage) {
        const side = -Math.sign(distance);
        const preview = side ? prepareSide(side, start.direction) : null;
        if (activePreview && activePreview !== preview) positionPreview(activePreview);
        activePreview = preview;
        // Keep the current image in place until the whole adjacent spread is ready.
        if (!preview?.ready) distance = 0;
        else positionPreview(preview, distance);
      }
      start.offset = distance;
      surface.style.translate = `${distance}px`;
    }
    function currentContext(start) {
      const context = getContext();
      if (context?.revision !== start.revision || context?.direction !== start.direction || Math.abs(viewport.scrollTop - start.top) > 2) return null;
      return context;
    }
    const clampOffset = (dx, width) => Math.min(width, Math.max(-width, dx));
    viewport.addEventListener('pointerdown', event => {
      suppressClick = false;
      if (event.pointerType === 'mouse') origin = null;
      if (event.pointerType !== 'mouse' || event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey ||
          event.target?.closest?.('button:not(.tap-zone), a, input, select, textarea, [contenteditable]')) return;
      const context = getContext();
      if (!context) return;
      if (!context.blocked) {
        resetVisual(false);
        for (const preview of previews.values()) if (preview.ready) { measurePreview(preview); positionPreview(preview); }
      }
      origin = { ...context, id: event.pointerId, x: event.clientX, y: event.clientY, top: viewport.scrollTop,
        width: viewport.clientWidth, dragged: false, horizontal: false, offset: 0, distance: 0 };
    });
    viewport.addEventListener('pointermove', event => {
      if (!origin || origin.id !== event.pointerId) return;
      const dx = event.clientX - origin.x, dy = event.clientY - origin.y;
      if (!origin.dragged) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) <= 10) return;
        origin.dragged = true;
        origin.horizontal = Math.abs(dx) > Math.abs(dy) * 1.5;
        // Capture after movement so a regular click still reaches its original tap zone.
        viewport.setPointerCapture(event.pointerId);
      }
      const context = currentContext(origin);
      if (!origin.horizontal || !context || context.blocked) return;
      slide(origin, clampOffset(dx, origin.width));
    });
    viewport.addEventListener('pointerup', event => {
      if (!origin || origin.id !== event.pointerId) return;
      const start = origin;
      origin = null;
      const dx = event.clientX - start.x, dy = event.clientY - start.y;
      suppressClick = start.dragged || Math.max(Math.abs(dx), Math.abs(dy)) > 10;
      if (!suppressClick) return;
      const context = currentContext(start);
      if (!context) {
        if (start.offset) resetVisual();
        return;
      }
      const threshold = Math.min(160, Math.max(64, start.width * .12));
      const delta = swipePageDelta({ dx, dy, direction: context.direction, threshold,
        blocked: context.blocked || event.button !== 0 || (start.dragged && !start.horizontal) });
      if (delta) {
        slide(start, clampOffset(dx, start.width));
        if (turnPage(delta, start.offset, activePreview?.ready ? activePreview.layer : null)) return;
      }
      if (start.dragged) settle(start.offset);
    });
    viewport.addEventListener('pointerleave', () => { if (!origin?.dragged) origin = null; });
    for (const type of ['pointercancel', 'lostpointercapture']) {
      viewport.addEventListener(type, event => {
        if (!origin || origin.id !== event.pointerId) return;
        const start = origin;
        origin = null;
        if (start.dragged && currentContext(start)) settle(start.offset);
      });
    }
    viewport.addEventListener('click', event => {
      if (!suppressClick || !event.detail) return;
      suppressClick = false;
      event.preventDefault();
      event.stopPropagation();
    }, true);
    return { reset: resetVisual, prepare };
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
  function createFullscreen(doc, button, onChange) {
    // A dialog cannot be a fullscreen target. Using the document also keeps
    // secondary reader dialogs, such as the bookmark editor, available.
    const target = doc.documentElement;
    const request = target?.requestFullscreen || target?.webkitRequestFullscreen;
    const exit = doc.exitFullscreen || doc.webkitExitFullscreen;
    const fullscreenElement = () => doc.fullscreenElement || doc.webkitFullscreenElement;
    let mode = '', pending = false, cancelled = false;

    function setMode(next) {
      if (next === mode) return;
      mode = next;
      const label = mode === 'immersive' ? '退出沉浸阅读' : mode === 'native' ? '退出全屏' : '进入全屏';
      button.setAttribute('aria-label', label);
      button.setAttribute('aria-pressed', String(Boolean(mode)));
      button.title = label + ' (Enter)';
      onChange(mode);
    }

    function exitNative() {
      try { return Promise.resolve(exit.call(doc)).then(syncNative, () => {}); }
      catch (_) {}
    }

    function syncNative() {
      if (fullscreenElement() === target) {
        if (!pending) return;
        pending = false;
        if (cancelled) exitNative();
        else setMode('native');
      } else if (mode === 'native') setMode('');
    }

    function requestFailed() {
      if (!pending) return;
      pending = false;
      if (!cancelled) setMode('immersive');
    }

    function toggle() {
      if (mode === 'native') return exitNative();
      if (mode === 'immersive') { setMode(''); return; }
      if (pending) return;
      if (!request || !exit || (doc.fullscreenEnabled ?? doc.webkitFullscreenEnabled) === false || fullscreenElement()) {
        setMode('immersive');
        return;
      }
      cancelled = false;
      pending = true;
      try {
        const result = request.call(target);
        if (result?.then) return result.then(() => { syncNative(); requestFailed(); }, requestFailed);
        // The prefixed WebKit API reports completion through events.
        syncNative();
      } catch (_) { requestFailed(); }
    }

    button.setAttribute('aria-pressed', 'false');
    for (const event of ['fullscreenchange', 'webkitfullscreenchange']) doc.addEventListener(event, syncNative);
    for (const event of ['fullscreenerror', 'webkitfullscreenerror']) doc.addEventListener(event, requestFailed);
    return {
      toggle,
      active: () => Boolean(mode),
      reset() {
        cancelled = true;
        if (mode === 'native' && fullscreenElement() === target) exitNative();
        setMode('');
      }
    };
  }
  return { buildSpreads, spreadForPage, visualPages, effectiveMode, effectiveDirection, canScrollPage, createWheelPager, swipePageDelta, bindMousePager, animatePageOffset, pageDimensions, pdfPageWidth, pageSource, createFullscreen };
}));

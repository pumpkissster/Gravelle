/* Reuse catalogue nodes and avoid downloading unchanged catalogue snapshots. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleCatalog = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  function createKeyedList({ key = item => String(item.id), create, update = () => {} }) {
    const entries = new Map();
    function obtain(item) {
      const id = key(item); let element = entries.get(id);
      if (!element) { element = create(item); entries.set(id, element); }
      update(element, item); return element;
    }
    return {
      render(container, items) {
        const active = container.ownerDocument?.activeElement;
        const restoreFocus = active && container.contains(active);
        const rows = items.map(obtain); const retained = new Set(rows);
        Array.from(container.children).forEach(element => { if (!retained.has(element)) element.remove(); });
        rows.forEach((element, index) => { if (container.children[index] !== element) container.insertBefore(element, container.children[index] || null); });
        for (const [id, element] of entries) if (!retained.has(element)) entries.delete(id);
        if (restoreFocus && container.contains(active) && container.ownerDocument.activeElement !== active) active.focus({ preventScroll: true });
        return rows;
      },
      append(container, items) {
        const rows = items.map(obtain); rows.forEach(element => container.append(element)); return rows;
      },
      clear() { entries.clear(); },
      get size() { return entries.size; }
    };
  }
  function createSearchInput(commit, { delay = 150, schedule = setTimeout, unschedule = clearTimeout } = {}) {
    let timer = null; let composing = false; let value = ''; let pending = false;
    const stop = () => { if (timer !== null) unschedule(timer); timer = null; };
    function flush() { stop(); if (!pending || composing) return; pending = false; commit(value); }
    function input(next, isComposing = false) {
      value = next; pending = true; stop();
      if (!composing && !isComposing) timer = schedule(flush, delay);
    }
    return {
      input,
      compositionStart() { composing = true; stop(); },
      compositionEnd(next) { composing = false; input(next); },
      flush,
      cancel() { stop(); composing = false; pending = false; }
    };
  }
  function catalogStatus(value) {
    return value && typeof value.revision === 'string' && value.revision.length > 0
      && typeof value.scanning === 'boolean' && typeof value.cover_pending === 'boolean' ? value : null;
  }
  function createLoader(api) {
    let revision = null; let supported = true; let epoch = 0; let serial = 0;
    return {
      async load({ loaded = false, force = false } = {}) {
        const requestEpoch = epoch; const requestSerial = ++serial; let status = null;
        const current = () => requestEpoch === epoch && requestSerial === serial;
        if (supported) {
          try { status = catalogStatus(await api('/api/catalog/status')); }
          catch (error) { if (![404, 405].includes(error.status)) throw error; if (current()) supported = false; }
        }
        if (!current()) return { stale: true };
        if (loaded && !force && status && status.revision === revision) return { unchanged: true, status };
        const [libraries, data, models, bookmarks] = await Promise.all([
          api('/api/libraries'), api('/api/library'), api('/api/models'), api('/api/bookmarks')
        ]);
        if (!current()) return { stale: true };
        // Keep the revision from before these reads. A concurrent mutation will
        // then trigger another refresh instead of being silently acknowledged.
        revision = status?.revision || null;
        return { unchanged: false, status, libraries, data, models, bookmarks };
      },
      reset() { epoch += 1; serial += 1; revision = null; supported = true; }
    };
  }
  function createGridLayout({ count, columns, rowHeight, gap = 0 }) {
    const length = Math.max(0, Math.trunc(count) || 0); const width = Math.max(1, Math.trunc(columns) || 1);
    const spacing = Math.max(0, Number(gap) || 0); const rows = Math.ceil(length / width); const offsets = [0];
    for (let row = 0; row < rows; row += 1) offsets.push(offsets[row] + Math.max(1, Number(typeof rowHeight === 'function' ? rowHeight(row) : rowHeight) || 1) + spacing);
    function rowAt(position) {
      let low = 0, high = rows;
      while (low < high) { const middle = (low + high + 1) >> 1; if (offsets[middle] <= position) low = middle; else high = middle - 1; }
      return Math.max(0, Math.min(rows - 1, low));
    }
    return {
      count: length, columns: width, rows, height: Math.max(0, offsets[rows] - spacing),
      offset(index) { return offsets[Math.max(0, Math.min(rows, Math.floor(index / width)))]; },
      window({ top = 0, height = 0, overscan = 3, pinned = [] } = {}) {
        if (!rows) return [];
        const margin = Math.max(0, Math.trunc(overscan) || 0); const selected = new Set();
        const start = Math.max(0, rowAt(Math.max(0, top)) - margin), end = Math.min(rows, rowAt(Math.max(0, top + height)) + margin + 1);
        for (let row = start; row < end; row += 1) selected.add(row);
        pinned.forEach(index => { if (Number.isInteger(index) && index >= 0 && index < length) selected.add(Math.floor(index / width)); });
        const result = []; let previous = 0;
        Array.from(selected).sort((a, b) => a - b).forEach(row => {
          if (row > previous) result.push({ start: previous * width, end: row * width, spacer: true, height: offsets[row] - offsets[previous] - spacing });
          result.push({ start: row * width, end: Math.min(length, (row + 1) * width), spacer: false }); previous = row + 1;
        });
        if (previous < rows) result.push({ start: previous * width, end: length, spacer: true, height: offsets[rows] - offsets[previous] - spacing });
        return result;
      }
    };
  }
  function createRetainedPages() {
    const entries = new Map(); let total = 0;
    return {
      set(index, value) { const bytes = Math.max(0, Number(value) || 0); total += bytes - (entries.get(index) || 0); entries.set(index, bytes); },
      delete(index) { if (!entries.has(index)) return; total -= entries.get(index); entries.delete(index); },
      clear() { entries.clear(); total = 0; },
      get bytes() { return total; }, get size() { return entries.size; }, keys: () => Array.from(entries.keys())
    };
  }
  return { createKeyedList, createSearchInput, createLoader, createGridLayout, createRetainedPages };
});

/* Keep only navigation positions in the current browser session, per account. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleListState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const MAX_POSITION = 10000000;
  function number(value, fallback, min, max, integer = false) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return fallback;
    const bounded = Math.max(min, Math.min(max, value));
    return integer ? Math.floor(bounded) : bounded;
  }
  function route(value) {
    return typeof value === 'string' && value.startsWith('#/') && value.length <= 30000;
  }
  function normalizeSnapshot(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const snapshot = {
      x: number(value.x, 0, 0, MAX_POSITION),
      y: number(value.y, 0, 0, MAX_POSITION),
      page: number(value.page, 0, 0, 1000000, true),
      revealed: number(value.revealed, 0, 0, 1000000, true),
      capacity: number(value.capacity, 0, 0, 999, true)
    };
    const anchor = value.anchor;
    if (anchor && typeof anchor === 'object' && typeof anchor.id === 'string' && anchor.id.length > 0 && anchor.id.length <= 200) {
      snapshot.anchor = { id: anchor.id, offset: number(anchor.offset, 0, -MAX_POSITION, MAX_POSITION) };
    }
    const filters = value.filters;
    if (filters && typeof filters === 'object' && !Array.isArray(filters)) {
      snapshot.filters = {};
      for (const [name, max] of [['query', 500], ['status', 50], ['library', 200]]) {
        if (typeof filters[name] === 'string') snapshot.filters[name] = filters[name].slice(0, max);
      }
    }
    return snapshot;
  }
  function createStore({ storage, accountId, limit = 40 } = {}) {
    const capacity = number(limit, 40, 1, 1000, true);
    let key = null;
    if (accountId !== undefined && accountId !== null && ['string', 'number'].includes(typeof accountId)) {
      try { key = 'gravelle-list-state:v1:' + encodeURIComponent(String(accountId)); } catch (_) { /* Invalid account strings never share a storage key. */ }
    }
    const entries = new Map();
    function prune() {
      while (entries.size > capacity) entries.delete(entries.keys().next().value);
    }
    if (key && storage) {
      try {
        const saved = JSON.parse(storage.getItem(key));
        if (saved && Array.isArray(saved.entries)) {
          for (const entry of saved.entries) {
            if (!Array.isArray(entry) || !route(entry[0])) continue;
            const snapshot = normalizeSnapshot(entry[1]);
            if (!snapshot) continue;
            entries.delete(entry[0]);
            entries.set(entry[0], snapshot);
            prune();
          }
        }
      } catch (_) { /* A denied or malformed session store must not prevent navigation. */ }
    }
    return {
      read(routeHash) {
        if (!route(routeHash)) return null;
        return normalizeSnapshot(entries.get(routeHash));
      },
      write(routeHash, value) {
        if (!route(routeHash)) return false;
        const snapshot = normalizeSnapshot(value);
        if (!snapshot) return false;
        entries.delete(routeHash);
        entries.set(routeHash, snapshot);
        prune();
        if (key && storage) {
          try { storage.setItem(key, JSON.stringify({ entries: Array.from(entries) })); } catch (_) { /* Retain the in-memory copy when storage is unavailable or full. */ }
        }
        return true;
      }
    };
  }
  return { createStore, normalizeSnapshot };
});

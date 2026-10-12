/* Shared catalog selection rules, independent of DOM and pagination. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleBatchSelection = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function kind(view, admin) {
    if (view === 'libraries') return admin ? 'libraries' : null;
    if (view === 'library' || (admin && ['model', 'search'].includes(view))) return 'books';
    return null;
  }
  function actions(view, admin) {
    if (!kind(view, admin)) return [];
    if (view === 'libraries') return ['remove', 'scan', 'random'];
    if (['model', 'search'].includes(view)) return ['models', 'remove'];
    return admin ? ['remove', 'favorite', 'random'] : ['favorite', 'random'];
  }
  function retain(ids, items) {
    const available = new Set(items.map(item => String(item.id)));
    return new Set([...ids].map(String).filter(id => available.has(id)));
  }
  function initialModelIds(books) {
    if (!books.length) return new Set();
    const ids = new Set((books[0].models || []).map(model => String(model.id)));
    for (const book of books.slice(1)) {
      const other = new Set((book.models || []).map(model => String(model.id)));
      if (ids.size !== other.size || [...ids].some(id => !other.has(id))) return new Set();
    }
    return ids;
  }
  return { kind, actions, retain, initialModelIds };
});

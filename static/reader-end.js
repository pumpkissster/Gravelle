/* Shared catalogue order and intentional end-of-book boundaries. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.SumiReaderEnd = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
  const sorts = ['added', 'added_asc', 'title', 'updated', 'release', 'modified'];
  const normalizeSort = value => sorts.includes(value) ? value : 'added';
  function compareBooks(a, b, value) {
    const sort = normalizeSort(value);
    let delta = 0;
    if (sort === 'updated') delta = (b.progress?.updated_at || 0) - (a.progress?.updated_at || 0);
    else if (sort === 'added') delta = (b.added_at || 0) - (a.added_at || 0);
    else if (sort === 'added_asc') delta = (a.added_at || 0) - (b.added_at || 0);
    else if (sort === 'modified') delta = (b.modified || 0) - (a.modified || 0);
    else if (sort === 'release') delta = String(b.release_date || '').localeCompare(String(a.release_date || ''));
    return delta || collator.compare(a.title || a.path || '', b.title || b.path || '');
  }
  function libraryBooks(books, libraryId, sort = 'added') {
    return books.filter(book => String(book.library_id) === String(libraryId) && Number(book.page_count) > 0)
      .slice().sort((a, b) => compareBooks(a, b, sort));
  }
  function nextBook(books, current, sort = 'added') {
    if (!current) return null;
    const ordered = libraryBooks(books, current.library_id, sort);
    const index = ordered.findIndex(book => String(book.id) === String(current.id));
    return index < 0 ? null : ordered[index + 1] || null;
  }
  function readingSequence(books, current, sort = 'added') {
    return { libraryId: String(current?.library_id ?? ''), sort: normalizeSort(sort), ids: current ? libraryBooks(books, current.library_id, sort).map(book => String(book.id)) : [] };
  }
  function nextInSequence(books, current, sequence) {
    if (!current || sequence?.libraryId !== String(current.library_id)) return null;
    const index = sequence.ids.indexOf(String(current.id));
    if (index < 0) return null;
    const available = new Map(libraryBooks(books, current.library_id, sequence.sort).map(book => [String(book.id), book]));
    for (const id of sequence.ids.slice(index + 1)) if (available.has(id)) return available.get(id);
    return null;
  }
  function recommendations(books, current, sort = 'added', limit = 6) {
    if (!current) return [];
    const ordered = libraryBooks(books, current.library_id, sort);
    const index = ordered.findIndex(book => String(book.id) === String(current.id));
    const candidates = index < 0 ? ordered : [...ordered.slice(index + 1), ...ordered.slice(0, index)];
    return candidates.filter(book => String(book.id) !== String(current.id)).slice(0, Math.max(0, Math.min(6, Math.floor(Number(limit) || 0))));
  }
  function canFinish({ mode = 'single', pageCount = 0, loadedPage = null, pages = [], restoring = null, atBottom = false }) {
    const final = Number(pageCount) - 1;
    if (final < 0 || loadedPage === null || Number(loadedPage) !== final || restoring !== null) return false;
    return mode === 'webtoon' ? Boolean(atBottom) : pages.includes(final);
  }
  function atScrollBottom({ scrollTop = 0, clientHeight = 0, scrollHeight = 0 }) {
    return clientHeight > 0 && scrollHeight > 0 && scrollTop + clientHeight >= scrollHeight - 3;
  }
  return { normalizeSort, compareBooks, libraryBooks, nextBook, readingSequence, nextInSequence, recommendations, canFinish, atScrollBottom };
});

/* Copyable hash routes work with the same static server on every platform. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravellePageRouter = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const paths = { home: 'home', libraries: 'libraries', library: 'libraries', recent: 'recent', bookmarks: 'bookmarks', favorite: 'favorites', search: 'search', models: 'models', model: 'models', settings: 'settings', book: 'books', reader: 'read' };
  const views = { home: 'home', libraries: 'libraries', recent: 'recent', bookmarks: 'bookmarks', favorites: 'favorite', search: 'search', models: 'models', settings: 'settings' };
  const sorts = ['added', 'added_asc', 'title', 'updated', 'release', 'modified'];
  const listViews = ['library', 'recent', 'favorite', 'search', 'model'];
  const settingsSections = ['service', 'users', 'interface', 'performance', 'reading', 'transcoding', 'image-search', 'cache'];
  const home = () => ({ view: 'home' });
  function id(value) { return typeof value === 'string' && value.length > 0 && value.length <= 200 && !/[\x00-\x1f\x7f/\\]/.test(value) ? value : ''; }
  function normalize(input, depth = 0) {
    if (!input || !Object.hasOwn(paths, input.view)) return home();
    const route = { view: input.view };
    // Older service/user links now point to the two panels on the settings home.
    if (route.view === 'settings' && settingsSections.includes(input.section) && !['service', 'users'].includes(input.section)) {
      route.section = input.section;
      if (route.section === 'transcoding' && input.tab === 'rules') route.tab = 'rules';
    }
    if (['library', 'model', 'book', 'reader'].includes(route.view)) {
      route.id = id(String(input.id ?? '')); if (!route.id) return home();
    }
    if (listViews.includes(route.view)) {
      if (typeof input.query === 'string' && input.query) route.query = input.query.slice(0, 500);
      if (sorts.includes(input.sort)) route.sort = input.sort;
      if (typeof input.series === 'string' && input.series) route.series = input.series.slice(0, 500);
    }
    if (route.view === 'search' && input.image === true) route.image = true;
    if (route.view === 'reader' && Number.isSafeInteger(Number(input.page)) && Number(input.page) > 0 && Number(input.page) <= 1000000) route.page = Number(input.page);
    if (['book', 'reader'].includes(route.view) && input.from && depth < 2) {
      const from = normalize(input.from, depth + 1);
      if (!['book', 'reader'].includes(from.view) || (route.view === 'reader' && from.view === 'book')) route.from = from;
    }
    return route;
  }
  function parse(value, depth = 0) {
    if (typeof value !== 'string' || value.length > 30000 || depth > 2) return home();
    const source = value.replace(/^#/, '');
    if (source === '' || source === '/') return home();
    if (!source.startsWith('/') || source.startsWith('//')) return home();
    const [path, query = ''] = source.split('?');
    const pieces = path.replace(/\/$/, '').split('/').slice(1);
    let decoded;
    try { decoded = pieces.map(decodeURIComponent); } catch (_) { return home(); }
    let view = decoded.length === 1 ? views[decoded[0]] : undefined;
    if (decoded.length === 2) view = { libraries: 'library', models: 'model', books: 'book', read: 'reader', settings: 'settings' }[decoded[0]];
    if (!view) return home();
    const params = new URLSearchParams(query);
    if (decoded[0] === 'settings' && decoded.length === 2 && !settingsSections.includes(decoded[1])) return home();
    return normalize({ view, id: decoded[1], section: decoded[0] === 'settings' ? decoded[1] : undefined, tab: params.get('tab'), image: params.get('type') === 'image', query: params.get('q'), sort: params.get('sort'), series: params.get('series'), page: params.get('page'), from: params.has('from') ? parse(params.get('from'), depth + 1) : undefined }, depth);
  }
  function path(input, depth = 0) {
    const route = normalize(input, depth);
    const params = new URLSearchParams();
    if (route.query) params.set('q', route.query);
    if (route.sort) params.set('sort', route.sort);
    if (route.series) params.set('series', route.series);
    if (route.page) params.set('page', route.page);
    if (route.from) params.set('from', path(route.from, depth + 1));
    if (route.tab) params.set('tab', route.tab);
    if (route.image) params.set('type', 'image');
    return '/' + paths[route.view] + (route.section ? '/' + route.section : route.id ? '/' + encodeURIComponent(route.id) : '') + (params.size ? '?' + params : '');
  }
  const hash = route => '#' + path(route);
  function base(route) {
    const value = normalize(route);
    return ['book', 'reader'].includes(value.view) ? base(value.from || home()) : value;
  }
  function returnTo(route) {
    const value = normalize(route);
    return value.from || (value.view === 'reader' ? { view: 'book', id: value.id } : home());
  }
  function resolve(input, catalog) {
    const route = normalize(input);
    if (route.view === 'library' && !catalog.libraries.some(item => String(item.id) === route.id)) return { route: { view: 'libraries' }, unavailable: true };
    if (route.view === 'model' && !catalog.models.some(item => String(item.id) === route.id)) return { route: { view: 'models' }, unavailable: true };
    // A personally hidden library is absent from the catalog, but its authorized
    // works remain accessible by direct link. The API decides book access.
    if (route.from) route.from = resolve(route.from, catalog).route;
    return { route, unavailable: false };
  }
  return { normalize, parse, path, hash, base, returnTo, resolve };
});

'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Users = require('../static/user-management.js');

const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Application section is available: ${start}`);
  return source.slice(first, last);
}
const appearance = section('  function stored(', '  function node(');

function fixture(initial = []) {
  const records = new Map(initial), writes = [], elements = new Map();
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      value: '', open: false, textContent: '', children: [], handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
      replaceChildren(...children) { this.children = children; }, close() { this.open = false; }, reset() {},
    });
    return elements.get(selector);
  };
  const root = { dataset: { interfaceSkin: 'board', interfaceTheme: 'dark' } }, meta = { content: '', setAttribute(name, value) { this[name] = value; } };
  const scope = {
    activeUser: null, authGeneration: 0, listPositions: null, listPositionTimer: null,
    routing: { ready: true, restoring: false, generation: 0 },
    reader: { pending: null, open: false, saveChains: new Map(), settingsChains: new Map(), pageCache: { clear() {} } },
    state: { prefs: { fit: 'height', theme: 'dark' }, selectedModels: new Set() },
    userManagement: { generation: 0 }, visibleLibraries: { generation: 0 },
    selection: { busy: false }, librarySorts: new Map(), catalogPaging: {}, chapterPaging: {}, renderedLibraryData: '',
    messageState: {}, Messages: require('../static/message-center.js'),
    catalogSearch: { cancel() {} }, catalogLoader: { reset() {} }, catalogCards: { clear() {} }, catalogFlow: {},
    sharingUI: null, networkSettingsUI: null, transcodingUI: null, imageSearchUI: null, cacheManagementUI: null,
    libraryCoverRequest: 0, libraryCoverBusy: false, searchMode: 'text',
    readPrefs: () => ({ fit: 'height', theme: 'dark' }),
    cancelListRestoration() {}, stopCatalogFlow() {}, resetSelection() {}, resetMessages() {}, applySidebarPreferences() {},
    homeSectionIds: [], readDisplayPreferences() {}, displayPreferences() { return { order: [], hidden: [] }; },
    clearTimeout() {}, closeReader() { throw new Error('Skin changes must not close a reader'); },
    Users, End: { normalizeSort: value => value }, Performance: { setUser() {} },
    window: { sessionStorage: {}, GravelleListState: { createStore: ({ accountId }) => ({ accountId }) } },
    localStorage: {
      getItem(key) { if (scope.failRead) throw new Error('blocked'); return records.get(key) ?? null; },
      setItem(key, value) { if (scope.failWrite) throw new Error('quota'); records.set(key, value); writes.push({ key, value }); },
    },
    document: { documentElement: root, querySelector: selector => selector === 'meta[name="theme-color"]' ? meta : null },
    $: get,
  };
  vm.createContext(scope);
  vm.runInContext(appearance, scope, { filename: 'app.js:interface-theme' });
  return { scope, root, meta, records, writes };
}

test('the initial page offers only Board, with a separate light and dark selector', () => {
  const html = fs.readFileSync(require.resolve('../static/index.html'), 'utf8');
  assert.match(html, /<html[^>]+data-interface-skin="board"[^>]+data-interface-theme="dark"/);
  assert.match(html, /<select id="interface-skin"><option value="board" selected>Board<\/option><\/select>/);
  assert.match(html, /<select id="interface-theme"><option value="dark">暗色<\/option><option value="light">亮色<\/option><\/select>/);
  assert.doesNotMatch(html, /name="interface-skin"/);
});

test('an account with the old classic preference keeps its saved light mode under Board', () => {
  const f = fixture([
    ['gravelle-user-1-interface-skin', 'classic'], ['gravelle-user-1-interface-theme', 'light'],
    ['gravelle-user-2-interface-skin', 'board'], ['gravelle-user-2-interface-theme', 'dark'],
  ]);
  assert.equal(f.root.dataset.interfaceTheme, 'dark');
  assert.equal(f.writes.length, 0);
  f.scope.activateAccount({ id: 1, role: 'admin' });
  assert.equal(f.root.dataset.interfaceSkin, 'board');
  assert.equal(f.root.dataset.interfaceTheme, 'light');
  assert.equal(f.meta.content, '#f7f7f7');
  f.scope.activateAccount({ id: 2, role: 'user' });
  assert.equal(f.root.dataset.interfaceTheme, 'dark');
  assert.equal(f.meta.content, '#121212');
  f.scope.applyInterfaceTheme('light');
  assert.equal(f.records.get('gravelle-user-2-interface-theme'), 'light');
  f.scope.activateAccount(null);
  assert.equal(f.root.dataset.interfaceTheme, 'dark');
  assert.equal(f.records.has('gravelle-user-signed-out-interface-theme'), false);
});

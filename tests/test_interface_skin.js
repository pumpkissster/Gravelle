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
const changeHandler = section("  $('#interface-skin-picker').addEventListener('change'", "  $('#check-updates').addEventListener");

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
  const choices = ['classic', 'board'].map(value => {
    const classes = new Set();
    const label = { classList: { toggle(name, selected) { if (selected) classes.add(name); else classes.delete(name); } } };
    return { value, checked: value === 'classic', classes, closest: () => label, matches: selector => selector === 'input[name="interface-skin"]' };
  });
  const root = { dataset: {} }, meta = { content: '', setAttribute(name, value) { this[name] = value; } };
  const frame = { hash: '#/libraries/example?sort=added', scrollTop: 864, readerIndex: 7, listNode: {} };
  const scope = {
    activeUser: null, authGeneration: 0, listPositions: null, listPositionTimer: null,
    routing: { ready: true, restoring: false, generation: 0 },
    reader: { pending: null, open: false, saveChains: new Map(), settingsChains: new Map(), pageCache: { clear() {} } },
    state: { prefs: { fit: 'height', theme: 'dark' }, selectedModels: new Set() },
    userManagement: { generation: 0 }, visibleLibraries: { generation: 0 },
    selection: { busy: false }, librarySorts: new Map(), catalogPaging: {}, chapterPaging: {}, renderedLibraryData: '',
    sharingUI: null, networkSettingsUI: null, transcodingUI: null, imageSearchUI: null, cacheManagementUI: null,
    libraryCoverRequest: 0, libraryCoverBusy: false, searchMode: 'text',
    readPrefs: () => ({ fit: 'height', theme: 'dark' }),
    cancelListRestoration() {}, stopCatalogFlow() {}, resetSelection() {}, resetOptimizationReminders() {},
    clearTimeout() {}, closeReader() { throw new Error('Skin changes must not close a reader'); },
    Users, End: { normalizeSort: value => value }, Performance: { setUser() {} },
    window: { sessionStorage: {}, GravelleListState: { createStore: ({ accountId }) => ({ accountId }) } },
    localStorage: {
      getItem(key) { if (scope.failRead) throw new Error('blocked'); return records.get(key) ?? null; },
      setItem(key, value) { if (scope.failWrite) throw new Error('quota'); records.set(key, value); writes.push({ key, value }); },
    },
    document: { documentElement: root, querySelector: selector => selector === 'meta[name="theme-color"]' ? meta : null },
    $: get, $$: selector => selector === '#interface-skin-picker input[name="interface-skin"]' ? choices : [],
    frame,
  };
  vm.createContext(scope);
  vm.runInContext(appearance + changeHandler, scope, { filename: 'app.js:interface-skin' });
  return { scope, root, meta, records, writes, choices, frame, get,
    choose(value) {
      const input = choices.find(choice => choice.value === value); input.checked = true;
      get('#interface-skin-picker').handlers.change({ target: input });
    },
  };
}

test('new visits keep the classic skin and dark theme without creating a signed-out preference', () => {
  const f = fixture();
  assert.equal(f.root.dataset.interfaceSkin, 'classic');
  assert.equal(f.root.dataset.interfaceTheme, 'dark');
  assert.equal(f.meta.content, '#19161a');
  assert.equal(f.choices[0].checked, true);
  assert.equal(f.records.size, 0);
});

test('switching skin saves only the current account and preserves theme, reader and list position', () => {
  const f = fixture(); f.scope.activateAccount({ id: 4, role: 'user' });
  f.scope.applyInterfaceTheme('light');
  const frame = { ...f.frame }, prefs = JSON.stringify(f.scope.state.prefs);
  const imageNode = {}, pageCache = f.scope.reader.pageCache;
  Object.assign(f.scope.reader, { open: true, index: 7, imageNode });
  const catalogState = { loaded: true, books: [{ id: 1 }], selection: new Set(['1']) };
  Object.assign(f.scope.state, catalogState);
  f.choose('board');
  assert.equal(f.root.dataset.interfaceSkin, 'board');
  assert.equal(f.records.get('gravelle-user-4-interface-skin'), 'board');
  assert.equal(f.records.get('gravelle-user-4-interface-theme'), 'light');
  assert.equal(f.root.dataset.interfaceTheme, 'light');
  assert.equal(f.meta.content, '#f7f7f7');
  assert.equal(f.choices[1].checked, true); assert.equal(f.choices[0].checked, false);
  assert.equal(f.choices[1].classes.has('is-selected'), true);
  assert.deepEqual(f.frame, frame); assert.equal(JSON.stringify(f.scope.state.prefs), prefs);
  assert.equal(f.scope.reader.open, true); assert.equal(f.scope.reader.index, 7);
  assert.equal(f.scope.reader.imageNode, imageNode); assert.equal(f.scope.reader.pageCache, pageCache);
  assert.equal(f.scope.state.books, catalogState.books); assert.equal(f.scope.state.selection, catalogState.selection);
  f.choose('classic'); assert.equal(f.meta.content, '#fcf8fa'); assert.equal(f.root.dataset.interfaceTheme, 'light');
});

test('account changes and logout restore each account independently without inheriting another skin', () => {
  const f = fixture([
    ['gravelle-user-1-interface-skin', 'board'], ['gravelle-user-1-interface-theme', 'light'],
    ['gravelle-user-2-interface-skin', 'classic'], ['gravelle-user-2-interface-theme', 'dark'],
  ]);
  f.scope.activateAccount({ id: 1, role: 'admin' });
  assert.equal(f.root.dataset.interfaceSkin, 'board'); assert.equal(f.root.dataset.interfaceTheme, 'light');
  f.scope.activateAccount({ id: 2, role: 'user' });
  assert.equal(f.root.dataset.interfaceSkin, 'classic'); assert.equal(f.root.dataset.interfaceTheme, 'dark');
  f.choose('board'); assert.equal(f.meta.content, '#121212');
  f.scope.activateAccount(null);
  assert.equal(f.root.dataset.interfaceSkin, 'classic'); assert.equal(f.root.dataset.interfaceTheme, 'dark');
  assert.equal(f.records.has('gravelle-user-signed-out-interface-skin'), false);
  f.scope.activateAccount({ id: 1, role: 'admin' });
  assert.equal(f.root.dataset.interfaceSkin, 'board'); assert.equal(f.root.dataset.interfaceTheme, 'light');
  f.scope.activateAccount({ id: 3, role: 'user' });
  assert.equal(f.root.dataset.interfaceSkin, 'classic'); assert.equal(f.root.dataset.interfaceTheme, 'dark');
});

test('invalid saved skins and unscoped records cannot enable a different skin', () => {
  for (const value of ['blue', 'BOARD', '{}', '', null]) {
    const f = fixture([['interface-skin', 'board'], ['gravelle-user-4-interface-skin', value]]);
    f.scope.activateAccount({ id: 4, role: 'user' });
    assert.equal(f.root.dataset.interfaceSkin, 'classic');
    assert.equal(f.choices[0].checked, true);
  }
});

test('blocked browser storage still permits a skin change for the current visit', () => {
  const f = fixture(); f.scope.activateAccount({ id: 4, role: 'user' });
  f.scope.failWrite = true; f.choose('board');
  assert.equal(f.root.dataset.interfaceSkin, 'board'); assert.equal(f.choices[1].checked, true);
  f.scope.syncInterfaceSkinPicker(); assert.equal(f.choices[1].checked, true);
  f.scope.applyInterfaceTheme('light'); assert.equal(f.meta.content, '#f7f7f7');
  f.scope.failRead = true; f.scope.activateAccount({ id: 5, role: 'user' });
  assert.equal(f.root.dataset.interfaceSkin, 'classic');
});

test('skin controls use accessible native radio semantics and load their styles after the existing interface', () => {
  const html = fs.readFileSync(require.resolve('../static/index.html'), 'utf8');
  assert.ok(html.indexOf('href="/board-skin.css"') > html.indexOf('href="/network-settings-ui.css"'));
  assert.match(html, /<fieldset id="interface-skin-picker"[^>]*><legend>界面皮肤<\/legend>/);
  assert.equal((html.match(/type="radio" name="interface-skin"/g) || []).length, 2);
  assert.match(html, /name="interface-skin" value="classic" checked/);
  assert.match(html, /name="interface-skin" value="board"/);
  assert.doesNotMatch(html, /<select[^>]+interface-skin/);
});

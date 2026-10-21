'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Network = require('../static/network-settings-ui.js');
const Sharing = require('../static/sharing-ui.js');
const Routes = require('../static/page-router.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const state = { security_entry_enabled: false, security_entry_code: '', security_entry_path: '/', share_port_enabled: false, share_port: 8081, share_listening: false, share_error: '', public_share_url: '', runtime: 'docker' };
const share = { token: 'x'.repeat(43) };
class Element {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.handlers = {}; this.attributes = {}; this.dataset = {}; this.value = ''; this.textContent = ''; this.checked = false; this.hidden = false; this.className = ''; this.classList = { add: name => { this.className += ' ' + name; } }; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  emit(name) { return this.handlers[name]?.({ preventDefault() {} }); }
  reportValidity() { return true; }
}
function fixture(options = {}) {
  const calls = [], posts = [], history = [], copied = [], messages = [];
  const win = { location: { href: 'http://192.0.2.10:7878/#/settings/network' }, history: { state: { marker: true }, replaceState(...args) { history.push(args); } }, navigator: { clipboard: { async writeText(value) { copied.push(value); } } }, crypto: { getRandomValues(bytes) { bytes.fill(25); return bytes; } } };
  const doc = { defaultView: win, createElement(tag) { return new Element(tag, doc); } }; const host = new Element('section', doc);
  const walk = node => [node, ...node.children.flatMap(walk)]; const get = name => walk(host).find(node => node.name === name || node.textContent === name);
  let admin = true;
  const ui = Network.create({ host, isAdmin: () => admin, api: async path => { calls.push(path); return options.api ? options.api(path) : { ...state }; }, post: async (path, data) => { posts.push({ path, data }); return options.post ? options.post(path, data) : { ...state, ...data, security_entry_path: data.security_entry_enabled ? '/' + data.security_entry_code : '/' }; }, toast: value => messages.push(value) });
  return { ui, host, win, get, walk, calls, posts, history, copied, messages, setAdmin(value) { admin = value; }, form: () => walk(host).find(node => node.tagName === 'form') };
}

test('network routes are copyable secondary settings pages', () => {
  assert.deepEqual(Routes.parse('#/settings/network'), { view: 'settings', section: 'network' });
  assert.equal(Routes.hash({ view: 'settings', section: 'network' }), '#/settings/network');
});

test('network settings only accept exact origins and four mutable fields', () => {
  const value = Network.payload({ security_entry_enabled: true, security_entry_code: 'my-safe_code', share_port_enabled: true, share_port: 9999, public_share_url: 'https://photos.example.com:8443/' });
  assert.deepEqual(value, { security_entry_enabled: true, security_entry_code: 'my-safe_code', share_port_enabled: true, public_share_url: 'https://photos.example.com:8443' });
  for (const code of ['short', 'x'.repeat(65), 'bad/path', '<script>', '安全入口码', 'bad%2Fcode']) assert.throws(() => Network.payload({ security_entry_enabled: true, security_entry_code: code }));
  for (const url of ['javascript:alert(1)', 'data:text/html,foo', 'https://photos.example/path', 'https://u:p@photos.example', 'https://photos.example/?pass=x', 'https://photos.example/#hash', '//photos.example', 'https://photos.example\\evil', 'https://photos.example\n.evil']) assert.throws(() => Network.publicUrl(url));
  assert.equal(Network.publicUrl(''), ''); assert.equal(Network.publicUrl(' http://localhost:7879/ '), 'http://localhost:7879');
});

test('share URLs prioritize configured public origin, then independent port, then main origin', () => {
  const base = 'http://192.0.2.10:7878/my-entry#/settings/shares';
  assert.equal(Sharing.shareUrl(share, base, { public_share_url: 'https://gravelle.example.com:8443', share_port_enabled: true, share_port: 8081 }), 'https://gravelle.example.com:8443/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, base, { share_port_enabled: true, share_port: 8081 }), 'http://192.0.2.10:8081/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, base, { share_port_enabled: false, share_port: 8081 }), 'http://192.0.2.10:7878/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, 'https://[::1]:8080/entry#/home', { share_port_enabled: true, share_port: 8081 }), 'https://[::1]:8081/share/' + share.token);
  for (const url of ['javascript:alert(1)', 'https://u:p@evil.example', 'https://evil.example/path', 'https://evil.example/?x=1', '//evil.example']) assert.throws(() => Sharing.shareUrl(share, base, { public_share_url: url }));
  assert.throws(() => Sharing.shareUrl(share, 'javascript:alert(1)'));
  assert.throws(() => Sharing.shareUrl(share, base, { share_port_enabled: true, share_port: '8081' }));
});

test('saving rewrites entry pathname while preserving hash and history state', () => {
  const win = { location: { href: 'https://example.com/old-entry?ignored=1#/settings/network' }, history: { state: { retained: true }, replaceState(...args) { this.args = args; } } };
  Network.replaceEntry({ security_entry_enabled: true, security_entry_code: 'entry-123', security_entry_path: '/entry-123' }, win);
  assert.deepEqual(win.history.args, [{ retained: true }, '', '/entry-123#/settings/network']);
  Network.replaceEntry({ security_entry_enabled: false, security_entry_path: '/' }, win); assert.equal(win.history.args[2], '/#/settings/network');
  for (const path of ['//evil.example', '/api/login', '/wrong', '/entry-123?x=1']) assert.throws(() => Network.replaceEntry({ security_entry_enabled: true, security_entry_code: 'entry-123', security_entry_path: path }, win));
});

test('subaccounts do not display network settings or request its API', async () => {
  const f = fixture(); f.setAdmin(false); f.ui.show(); await tick(); assert.equal(f.host.hidden, true); assert.equal(f.calls.length, 0);
  await assert.rejects(f.ui.getSettings(), /管理员/); assert.equal(f.calls.length, 0);
});

test('administrator save sends no port and updates address after the signed cookie response', async () => {
  const f = fixture(); f.ui.show(); await tick();
  f.get('security_entry_enabled').checked = true; f.get('security_entry_enabled').emit('change');
  f.get('security_entry_code').value = 'private-entry'; f.get('security_entry_code').emit('input');
  f.get('share_port_enabled').checked = true; f.get('share_port_enabled').emit('change');
  f.get('public_share_url').value = 'https://public.example:8443'; f.get('public_share_url').emit('input');
  await f.form().emit('submit');
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].path, '/api/network-settings'); assert.deepEqual(Object.keys(f.posts[0].data).sort(), ['public_share_url', 'security_entry_code', 'security_entry_enabled', 'share_port_enabled']);
  assert.equal(f.history[0][2], '/private-entry#/settings/network'); assert.equal(f.get('保存网络设置').disabled, true);
  assert.ok(f.walk(f.host).some(node => node.textContent.includes('固定为 8081')));
  assert.ok(f.walk(f.host).some(node => node.textContent.includes('一次性恢复容器')));
});

test('refresh errors and late snapshots retain unsaved administrator edits', async () => {
  let finish, fail = false, count = 0;
  const f = fixture({ api() { count++; if (count === 1) return { ...state }; if (fail) throw new Error('network unavailable'); return new Promise(resolve => { finish = resolve; }); } });
  f.ui.show(); await tick(); const refreshing = f.get('刷新状态').emit('click');
  f.get('public_share_url').value = 'https://my-unsaved.example'; f.get('public_share_url').emit('input');
  finish({ ...state, public_share_url: 'https://stale.example' }); await refreshing;
  assert.equal(f.get('public_share_url').value, 'https://my-unsaved.example');
  fail = true; await f.get('刷新状态').emit('click'); assert.equal(f.get('public_share_url').value, 'https://my-unsaved.example');
  assert.equal(f.get('保存网络设置').disabled, false); assert.ok(f.walk(f.host).some(node => node.textContent === 'network unavailable'));
});

test('account reset invalidates late private responses and removes code from the DOM', async () => {
  let finish; const f = fixture({ api() { return new Promise(resolve => { finish = resolve; }); } });
  f.ui.show(); f.setAdmin(false); f.ui.reset(); finish({ ...state, security_entry_enabled: true, security_entry_code: 'secret-code', security_entry_path: '/secret-code' }); await tick();
  assert.equal(f.host.hidden, true); assert.equal(f.get('security_entry_code').value, ''); assert.equal(f.history.length, 0); assert.ok(!f.walk(f.host).some(node => node.textContent.includes('secret-code')));
});

test('share configuration cannot race a network settings mutation with an older GET', async () => {
  let finish; const f = fixture({ post(path, data) { return new Promise(resolve => { finish = () => resolve({ ...state, ...data, security_entry_path: '/' }); }); } });
  f.ui.show(); await tick(); f.get('public_share_url').value = 'https://new.example'; f.get('public_share_url').emit('input');
  const saving = f.form().emit('submit'); await assert.rejects(f.ui.getSettings(true), /正在保存/); assert.equal(f.calls.length, 1);
  finish(); await saving; assert.equal((await f.ui.getSettings()).public_share_url, 'https://new.example');
});

test('entry generator uses cryptographic randomness and entry URLs exclude page hashes', async () => {
  const f = fixture(); f.ui.show(); await tick(); await f.get('随机生成').emit('click');
  assert.match(f.get('security_entry_code').value, /^[A-Za-z0-9_-]{18}$/);
  assert.equal(Network.entryUrl('safe-code', true, 'https://example.com:8443/old#/settings/network'), 'https://example.com:8443/safe-code');
});

test('application integrates administrator gating and loads the network module before app', () => {
  const app = fs.readFileSync(require.resolve('../static/app.js'), 'utf8'); const html = fs.readFileSync(require.resolve('../static/index.html'), 'utf8');
  assert.match(app, /settingsLayout\.items\.network\.entry\.hidden = !isAdmin\(\)/);
  assert.match(app, /section === 'network' && isAdmin\(\)/);
  assert.match(app, /networkSettingsUI\?\.reset\(\)/);
  assert.ok(html.indexOf('src="/network-settings-ui.js"') < html.indexOf('src="/app.js"'));
  assert.match(html, /href="\/network-settings-ui\.css"/);
});

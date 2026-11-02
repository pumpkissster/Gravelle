'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Network = require('../static/network-settings-ui.js');
const Sharing = require('../static/sharing-ui.js');
const Routes = require('../static/page-router.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const state = { security_entry_enabled: false, security_entry_code: '', security_entry_path: '/', share_port_enabled: false, share_port: 8081, share_listening: false, share_error: '', public_share_url: '', runtime: 'docker' };
const tlsState = { ...state, connection_mode: 'http', certificate_path: '', certificate_password_set: false, https_port: 8443, https_listening: false, https_error: '', share_scheme: 'http' };
const share = { token: 'x'.repeat(43) };
class Element {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.handlers = {}; this.attributes = {}; this.dataset = {}; this.value = ''; this.textContent = ''; this.checked = false; this.hidden = false; this.className = ''; this.classList = { add: name => { this.className += ' ' + name; } }; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  emit(name) { return this.handlers[name]?.({ preventDefault() {} }); }
  click() { this.clickCount = (this.clickCount || 0) + 1; return this.emit('click'); }
  reportValidity() { return true; }
}
function fixture(options = {}) {
  const calls = [], posts = [], history = [], copied = [], messages = [];
  const initial = options.state || state;
  const win = { location: { href: 'http://192.0.2.10:7878/#/settings/network' }, history: { state: { marker: true }, replaceState(...args) { history.push(args); } }, navigator: { clipboard: { async writeText(value) { copied.push(value); } } }, crypto: { getRandomValues(bytes) { bytes.fill(25); return bytes; } } };
  const doc = { defaultView: win, createElement(tag) { return new Element(tag, doc); } }; const host = new Element('section', doc);
  const walk = node => [node, ...node.children.flatMap(walk)]; const get = name => walk(host).find(node => node.name === name || node.textContent === name);
  let admin = true;
  const ui = Network.create({ host, isAdmin: () => admin, api: async path => { calls.push(path); return options.api ? options.api(path) : { ...initial }; }, post: async (path, data) => { posts.push({ path, data }); if (options.post) return options.post(path, data); const response = { ...initial, ...data, security_entry_path: data.security_entry_enabled ? '/' + data.security_entry_code : '/' }; if ('connection_mode' in response) { response.certificate_password_set = Object.prototype.hasOwnProperty.call(data, 'certificate_password') ? Boolean(data.certificate_password) : initial.certificate_password_set; response.https_listening = data.connection_mode === 'https'; response.share_scheme = data.connection_mode === 'https' ? 'https' : 'http'; } if (data.certificate_upload) response.certificate_path = '/data/https-certificates/generated.pfx'; delete response.certificate_password; delete response.certificate_upload; return response; }, toast: value => messages.push(value) });
  return { ui, host, win, get, walk, calls, posts, history, copied, messages, setAdmin(value) { admin = value; }, form: () => walk(host).find(node => node.tagName === 'form') };
}

function sampleCertificate(name = 'example.pfx', bytes = Uint8Array.from([3, 8, 12, 77])) {
  return { name, size: bytes.byteLength, readCalls: 0, async arrayBuffer() { this.readCalls++; return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); } };
}
function chooseCertificate(f, file) { const input = f.get('certificate_file'); input.files = file ? [file] : []; input.value = file ? 'C:\\fakepath\\' + file.name : ''; input.emit('change'); }

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
  assert.equal(Sharing.shareUrl(share, base, { share_port_enabled: true, share_port: 8081, share_scheme: 'https' }), 'https://192.0.2.10:8081/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, 'https://photos.example:443/#/home', { share_port_enabled: true, share_port: 8081, share_scheme: 'http' }), 'http://photos.example:8081/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, base, { connection_mode: 'https', https_port: 8443 }), 'https://192.0.2.10:8443/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, 'https://photos.example:9443/#/home', { connection_mode: 'https', https_port: 8443 }), 'https://photos.example:9443/share/' + share.token);
  assert.equal(Sharing.shareUrl(share, base, { public_share_url: 'http://explicit.example:9876', share_port_enabled: true, share_port: 8081, share_scheme: 'https' }), 'http://explicit.example:9876/share/' + share.token);
  assert.throws(() => Sharing.shareUrl(share, base, { share_port_enabled: true, share_port: 8081, share_scheme: 'ftp' }));
  assert.throws(() => Sharing.shareUrl(share, base, { connection_mode: 'https', https_port: 0 }));
});

test('HTTPS payload preserves optional passwords and rejects invalid modes or certificate paths', () => {
  const value = { ...tlsState, connection_mode: 'https', certificate_path: ' /certificates/写真.PFX ' };
  const keep = Network.payload(value); assert.equal(keep.connection_mode, 'https'); assert.equal(keep.certificate_path, '/certificates/写真.PFX'); assert.ok(!('certificate_password' in keep)); assert.ok(!('https_port' in keep));
  assert.equal(Network.payload({ ...value, certificate_password: '' }).certificate_password, '');
  assert.equal(Network.payload({ ...value, certificate_password: '  exact password  ' }).certificate_password, '  exact password  ');
  for (const connection_mode of ['', 'tls', 'HTTPS', null]) assert.throws(() => Network.payload({ ...value, connection_mode }));
  for (const certificate_path of ['', '/cert/a.pem', '/cert/a.pfx\nother.pfx', '/cert/a.p12\0']) assert.throws(() => Network.payload({ ...value, certificate_path }));
  assert.throws(() => Network.payload({ ...value, certificate_password: 42 }));
  assert.throws(() => Network.payload({ ...value, certificate_password: 'x'.repeat(1025) }));
});

test('network snapshots accept legacy servers but never accept a returned certificate password', () => {
  assert.equal(Network.valid(state), true); assert.equal(Network.valid(tlsState), true);
  assert.equal(Network.valid({ ...tlsState, certificate_password: 'must-not-return' }), false);
  assert.equal(Network.valid({ ...tlsState, certificate_upload: { name: 'private.pfx', data: 'c2VjcmV0' } }), false);
  assert.equal(Network.valid({ ...tlsState, certificate_password_ciphertext: 'must-not-return' }), false);
  assert.equal(Network.valid({ ...state, connection_mode: 'https' }), false);
  assert.equal(Network.valid({ ...tlsState, https_port: '8443' }), false);
  assert.equal(Network.valid({ ...tlsState, share_scheme: 'ftp' }), false);
});

test('certificate upload encoding preserves every byte through chunk boundaries and the 16 MiB limit', async () => {
  for (const length of [1, 2, 3, 24575, 24576, 24577, Network.MAX_CERTIFICATE_BYTES]) {
    const bytes = new Uint8Array(length); for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251;
    const file = sampleCertificate('测试.P12', bytes); const upload = await Network.readCertificateUpload(file);
    assert.equal(upload.name, '测试.P12'); assert.equal(file.readCalls, 1);
    assert.deepEqual(Buffer.from(upload.data, 'base64'), Buffer.from(bytes));
    if (length === Network.MAX_CERTIFICATE_BYTES) assert.equal(Network.payload({ ...tlsState, connection_mode: 'https', certificate_upload: upload }).certificate_upload.data, upload.data);
  }
  for (const file of [{ name: 'empty.pfx', size: 0 }, { name: 'huge.pfx', size: Network.MAX_CERTIFICATE_BYTES + 1 }, { name: 'text.pem', size: 5 }, { name: '../escape.pfx', size: 5 }, { name: 'bad\0.p12', size: 5 }]) await assert.rejects(Network.readCertificateUpload(file));
  await assert.rejects(Network.readCertificateUpload({ name: 'broken.pfx', size: 3, async arrayBuffer() { throw Error('private bytes'); } }), /无法读取证书文件/);
  await assert.rejects(Network.readCertificateUpload({ name: 'partial.pfx', size: 3, async arrayBuffer() { return new ArrayBuffer(2); } }), /读取不完整/);
});

test('upload payload replaces the path draft and does not send uploads while HTTPS is off', () => {
  const certificate_upload = { name: 'example.pfx', data: 'AQID' };
  const result = Network.payload({ ...tlsState, connection_mode: 'https', certificate_path: '/stale/not-a-certificate', certificate_upload });
  assert.deepEqual(result.certificate_upload, certificate_upload); assert.ok(!('certificate_path' in result));
  for (const value of [{ ...tlsState, certificate_upload }, { ...state, certificate_upload }, { ...tlsState, connection_mode: 'https', certificate_upload: { name: '../secret.pfx', data: 'AQID' } }, { ...tlsState, connection_mode: 'https', certificate_upload: { name: 'a.pfx', data: '' } }, { ...tlsState, connection_mode: 'https', certificate_upload: { name: 'a.pfx', data: 'not-base64!' } }, { ...tlsState, connection_mode: 'https', certificate_upload: { name: 'a.pfx', data: 'A===' } }]) assert.throws(() => Network.payload(value));
});

test('choosing a certificate stages it locally until save, then shows the returned managed path', async () => {
  const configured = { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx', certificate_password_set: true };
  const f = fixture({ state: configured }); f.ui.show(); await tick();
  assert.equal(f.get('certificate_file').type, 'file'); assert.equal(f.get('certificate_file').accept, '.pfx,.p12'); assert.equal(f.get('certificate_file').hidden, true);
  f.get('上传证书').emit('click'); assert.equal(f.get('certificate_file').clickCount, 1);
  const file = sampleCertificate(); chooseCertificate(f, file);
  assert.equal(file.readCalls, 0); assert.equal(f.posts.length, 0); assert.equal(f.get('certificate_path').value, configured.certificate_path);
  assert.equal(f.get('certificate_path').disabled, true); assert.equal(f.get('certificate_path').required, false);
  assert.ok(f.walk(f.host).some(node => node.textContent.includes('example.pfx · 保存后上传')));
  for (const label of f.walk(f.host).filter(node => node.tagName === 'label')) assert.ok(!f.walk(label).includes(f.get('certificate_file')));
  await f.form().emit('submit'); assert.equal(file.readCalls, 1); assert.equal(f.posts.length, 1);
  assert.deepEqual(f.posts[0].data.certificate_upload, { name: file.name, data: 'AwgMTQ==' }); assert.ok(!('certificate_path' in f.posts[0].data)); assert.ok(!('certificate_password' in f.posts[0].data));
  assert.equal(f.get('certificate_path').value, '/data/https-certificates/generated.pfx'); assert.equal(f.get('certificate_path').disabled, false);
  assert.equal(f.get('certificate_file').value, ''); assert.equal(f.walk(f.host).find(node => node.className === 'network-upload-status').hidden, true);
});

test('invalid certificate selections are rejected without reading or posting', async () => {
  const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx' } }); f.ui.show(); await tick();
  for (const file of [sampleCertificate('wrong.png'), { ...sampleCertificate(), size: Network.MAX_CERTIFICATE_BYTES + 1 }]) {
    chooseCertificate(f, file); assert.equal(file.readCalls, 0); assert.equal(f.posts.length, 0); assert.equal(f.get('certificate_file').value, '');
    assert.equal(f.get('certificate_path').disabled, false); assert.equal(f.get('certificate_path').required, true);
    assert.equal(f.walk(f.host).find(node => node.className === 'network-error').hidden, false);
  }
});

test('canceling a staged certificate restores path editing and preserves the saved password', async () => {
  const configured = { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx', certificate_password_set: true };
  const f = fixture({ state: configured }); f.ui.show(); await tick(); chooseCertificate(f, sampleCertificate());
  f.get('certificate_password').value = 'password-for-staged-file'; f.get('certificate_password').emit('input'); f.get('取消上传').emit('click');
  assert.equal(f.get('certificate_file').value, ''); assert.equal(f.get('certificate_password').value, ''); assert.equal(f.get('certificate_path').disabled, false);
  await f.form().emit('submit'); assert.ok(!('certificate_path' in f.posts[0].data)); assert.ok(!('certificate_upload' in f.posts[0].data)); assert.ok(!('certificate_password' in f.posts[0].data));
});

test('a file chosen during refresh survives the late status response and remains the save source', async () => {
  let finish, calls = 0; const configured = { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx' };
  const f = fixture({ api() { return ++calls === 1 ? { ...configured } : new Promise(resolve => { finish = resolve; }); } });
  f.ui.show(); await tick(); const refreshing = f.get('刷新状态').emit('click'); const file = sampleCertificate(); chooseCertificate(f, file);
  finish({ ...configured, certificate_path: '/newer/server.pfx' }); await refreshing;
  assert.equal(f.get('certificate_path').value, configured.certificate_path); assert.equal(f.get('certificate_path').disabled, true);
  await f.form().emit('submit'); assert.equal(f.posts.length, 1); assert.equal(f.posts[0].data.certificate_upload.name, file.name);
});

test('a pending file read blocks duplicate saves and cannot post after account reset', async () => {
  let finish; const file = { name: 'slow.pfx', size: 3, arrayBuffer() { return new Promise(resolve => { finish = resolve; }); } };
  const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx' } }); f.ui.show(); await tick(); chooseCertificate(f, file);
  f.get('certificate_password').value = 'do-not-send'; f.get('certificate_password').emit('input'); const saving = f.form().emit('submit');
  assert.equal(f.get('保存网络设置').disabled, true); await f.form().emit('submit'); assert.equal(f.posts.length, 0);
  f.setAdmin(false); f.ui.reset(); finish(Uint8Array.from([1, 2, 3]).buffer); await saving;
  assert.equal(f.posts.length, 0); assert.equal(f.get('certificate_file').value, ''); assert.equal(f.get('certificate_password').value, ''); assert.equal(f.host.hidden, true);
  assert.ok(!f.walk(f.host).some(node => node.textContent.includes('slow.pfx')));
});

test('disabling HTTPS during a pending read cancels its upload and password draft', async () => {
  let finish; const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx', certificate_password_set: true } }); f.ui.show(); await tick();
  chooseCertificate(f, { name: 'slow.pfx', size: 3, arrayBuffer() { return new Promise(resolve => { finish = resolve; }); } });
  f.get('certificate_password').value = 'do-not-send'; f.get('certificate_password').emit('input'); const saving = f.form().emit('submit');
  f.get('https_enabled').checked = false; f.get('https_enabled').emit('change'); finish(Uint8Array.from([1, 2, 3]).buffer); await saving;
  assert.equal(f.posts.length, 0); assert.equal(f.get('certificate_file').value, ''); assert.equal(f.get('certificate_password').value, '');
  await f.form().emit('submit'); assert.equal(f.posts[0].data.connection_mode, 'http'); assert.ok(!('certificate_upload' in f.posts[0].data)); assert.ok(!('certificate_password' in f.posts[0].data));
});

test('file read and server failures keep the selection available for an explicit retry', async () => {
  let reads = 0, requests = 0;
  const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx' }, post(path, data) { if (++requests === 1) throw Error('证书密码不正确'); const response = { ...tlsState, connection_mode: 'https', certificate_path: '/data/https-certificates/retried.pfx', certificate_password_set: false, https_listening: true, share_scheme: 'https' }; return response; } });
  f.ui.show(); await tick(); chooseCertificate(f, { name: 'retry.pfx', size: 3, async arrayBuffer() { if (++reads === 1) throw Error('unreadable file'); return Uint8Array.from([1, 2, 3]).buffer; } });
  await f.form().emit('submit'); assert.equal(f.posts.length, 0); assert.equal(f.get('certificate_path').disabled, true); assert.equal(f.get('保存网络设置').disabled, false);
  await f.form().emit('submit'); assert.equal(f.posts.length, 1); assert.equal(f.get('certificate_path').disabled, true); assert.ok(f.get('certificate_file').value);
  await f.form().emit('submit'); assert.equal(f.posts.length, 2); assert.equal(reads, 3); assert.equal(f.get('certificate_file').value, ''); assert.equal(f.get('certificate_path').value, '/data/https-certificates/retried.pfx');
});

test('discard and account reset remove staged files from subsequent saves', async () => {
  for (const action of ['discard', 'reset']) {
    const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/old/original.pfx', certificate_password_set: true } }); f.ui.show(); await tick();
    chooseCertificate(f, sampleCertificate()); f.get('certificate_password').value = 'staged-password'; f.get('certificate_password').emit('input');
    if (action === 'discard') f.get('放弃修改').emit('click'); else { f.ui.reset(); f.ui.show(); await tick(); }
    assert.equal(f.get('certificate_file').value, ''); assert.equal(f.get('certificate_password').value, '');
    f.get('public_share_url').value = 'https://public.example.com'; f.get('public_share_url').emit('input'); await f.form().emit('submit');
    assert.ok(!('certificate_upload' in f.posts[0].data)); assert.ok(!('certificate_password' in f.posts[0].data)); assert.ok(!('certificate_path' in f.posts[0].data)); assert.equal(f.get('certificate_path').value, '/old/original.pfx');
  }
});

test('HTTPS switch reveals certificate controls and web settings cannot edit ports', async () => {
  const f = fixture({ state: tlsState }); f.ui.show(); await tick(); const details = f.walk(f.host).find(node => node.className.includes('network-certificate-details'));
  assert.equal(f.get('https_enabled').disabled, false); assert.equal(f.get('https_enabled').checked, false); assert.equal(details.hidden, true);
  assert.equal(f.get('https_enabled').attributes.role, 'switch'); assert.equal(f.walk(f.host).filter(node => node.tagName === 'select').length, 0);
  assert.ok(f.walk(f.host).some(node => node.textContent.includes('反向代理提供 HTTPS 时无需开启')));
  f.get('https_enabled').checked = true; f.get('https_enabled').emit('change'); assert.equal(details.hidden, false); assert.equal(f.get('certificate_path').required, true);
  assert.ok(f.walk(f.host).some(node => node.textContent.includes('固定为 8443')));
  assert.equal(f.walk(f.host).filter(node => node.tagName === 'input' && /port/.test(node.name || '')).length, 1); // Existing independent-share switch, never a numeric port input.
  assert.equal(f.walk(f.host).filter(node => node.tagName === 'input' && node.type === 'number').length, 0);
});

test('saving a new certificate replaces its password but clears the secret field after success', async () => {
  const f = fixture({ state: tlsState }); f.ui.show(); await tick();
  f.get('https_enabled').checked = true; f.get('https_enabled').emit('change');
  f.get('certificate_path').value = '/certs/reader.p12'; f.get('certificate_path').emit('input');
  f.get('certificate_password').value = 'private-password'; f.get('certificate_password').emit('input'); await f.form().emit('submit');
  assert.equal(f.posts[0].data.certificate_password, 'private-password'); assert.equal(f.get('certificate_password').value, '');
  assert.equal(f.posts[0].data.connection_mode, 'https'); assert.equal(f.get('https_enabled').checked, true);
  assert.match(f.get('certificate_password').placeholder, /已设置/); assert.equal(f.get('清除已保存密码').hidden, false);
  assert.ok(!f.walk(f.host).some(node => node.textContent.includes('private-password'))); assert.ok(!JSON.stringify(f.history).includes('private-password'));
});

test('an existing certificate password is retained on blank save and explicitly clearable', async () => {
  const configured = { ...tlsState, connection_mode: 'https', certificate_path: '/certs/reader.pfx', certificate_password_set: true, https_listening: true, share_scheme: 'https' };
  const f = fixture({ state: configured }); f.ui.show(); await tick(); assert.equal(f.get('certificate_password').value, '');
  f.get('public_share_url').value = 'https://photos.example.com'; f.get('public_share_url').emit('input'); await f.form().emit('submit');
  assert.ok(!('certificate_password' in f.posts[0].data));
  f.get('清除已保存密码').emit('click'); assert.equal(f.get('取消清除密码').textContent, '取消清除密码'); await f.form().emit('submit');
  assert.equal(f.posts[1].data.certificate_password, ''); assert.equal(f.get('清除已保存密码').hidden, true);
});

test('a dirty refresh preserves the displayed certificate baseline without resending a replaced path', async () => {
  const initial = { ...tlsState, connection_mode: 'https', certificate_path: '/data/https-certificates/old.pfx', certificate_password_set: true };
  const newer = { ...initial, certificate_path: '/data/https-certificates/new.pfx' };
  let reads = 0;
  const f = fixture({ state: initial, api() { return { ...(++reads === 1 ? initial : newer) }; }, post(path, data) { return { ...newer, ...data, https_listening: false, share_scheme: 'http' }; } });
  f.ui.show(); await tick();
  f.get('public_share_url').value = 'https://photos.example.com'; f.get('public_share_url').emit('input');
  await f.get('刷新状态').emit('click');
  assert.equal(f.get('certificate_path').value, initial.certificate_path);
  f.get('https_enabled').checked = false; f.get('https_enabled').emit('change');
  await f.form().emit('submit');
  assert.equal(f.posts[0].data.connection_mode, 'http');
  assert.ok(!('certificate_path' in f.posts[0].data));
  assert.ok(!('certificate_password' in f.posts[0].data));
  assert.equal(f.get('certificate_path').value, newer.certificate_path);
});

test('manual certificate path edits are sent but reverting to the filled path keeps the server value', async () => {
  const configured = { ...tlsState, connection_mode: 'https', certificate_path: '/certs/original.pfx' };
  for (const revert of [false, true]) {
    const f = fixture({ state: configured }); f.ui.show(); await tick();
    f.get('certificate_path').value = '/certs/manual.p12'; f.get('certificate_path').emit('input');
    if (revert) { f.get('certificate_path').value = configured.certificate_path; f.get('certificate_path').emit('input'); }
    await f.form().emit('submit');
    if (revert) assert.ok(!('certificate_path' in f.posts[0].data));
    else assert.equal(f.posts[0].data.certificate_path, '/certs/manual.p12');
  }
});

test('password drafts survive stale refresh but are cleared by disabling HTTPS, discard and account reset', async () => {
  let finish, calls = 0; const configured = { ...tlsState, connection_mode: 'https', certificate_path: '/certs/reader.pfx', certificate_password_set: true };
  const f = fixture({ api() { return ++calls === 1 ? { ...configured } : new Promise(resolve => { finish = resolve; }); } });
  f.ui.show(); await tick(); const refresh = f.get('刷新状态').emit('click'); f.get('certificate_password').value = 'draft-secret'; f.get('certificate_password').emit('input'); finish({ ...configured, certificate_password_set: false }); await refresh;
  assert.equal(f.get('certificate_password').value, 'draft-secret');
  f.get('https_enabled').checked = false; f.get('https_enabled').emit('change'); assert.equal(f.get('certificate_password').value, '');
  f.get('https_enabled').checked = true; f.get('https_enabled').emit('change'); f.get('certificate_password').value = 'another-secret'; f.get('certificate_password').emit('input'); f.get('放弃修改').emit('click'); assert.equal(f.get('certificate_password').value, '');
  f.get('certificate_password').value = 'reset-secret'; f.get('certificate_password').emit('input'); f.setAdmin(false); f.ui.reset();
  assert.equal(f.get('certificate_password').value, ''); assert.equal(f.get('certificate_path').value, ''); assert.equal(f.get('https_enabled').checked, false); assert.equal(f.host.hidden, true);
});

test('legacy HTTP and proxy settings load with HTTPS off and keep certificate secrets when saved', async () => {
  for (const connection_mode of ['http', 'proxy']) {
    const f = fixture({ state: { ...tlsState, connection_mode, certificate_path: '/certs/retained.pfx', certificate_password_set: true } });
    f.ui.show(); await tick(); assert.equal(f.get('https_enabled').checked, false);
    f.get('public_share_url').value = 'https://photos.example.com'; f.get('public_share_url').emit('input'); await f.form().emit('submit');
    assert.equal(f.posts[0].data.connection_mode, 'http'); assert.ok(!('certificate_path' in f.posts[0].data)); assert.equal(f.get('certificate_path').value, '/certs/retained.pfx');
    assert.ok(!('certificate_password' in f.posts[0].data)); assert.ok(!('https_enabled' in f.posts[0].data));
    assert.equal(f.get('https_enabled').checked, false);
  }
  const legacy = fixture(); legacy.ui.show(); await tick(); assert.equal(legacy.get('https_enabled').disabled, true);
});

test('turning HTTPS off sends HTTP without clearing the saved certificate password', async () => {
  const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/certs/retained.pfx', certificate_password_set: true } });
  f.ui.show(); await tick(); f.get('certificate_password').value = 'discard-this-draft'; f.get('certificate_password').emit('input');
  f.get('https_enabled').checked = false; f.get('https_enabled').emit('change'); await f.form().emit('submit');
  assert.equal(f.posts[0].data.connection_mode, 'http'); assert.ok(!('certificate_password' in f.posts[0].data));
  assert.equal(f.get('certificate_password').value, ''); assert.equal(f.get('certificate_password').disabled, true);
  assert.equal(f.get('certificate_path').required, false);
});

test('certificate errors never echo the supplied secret and no late save repopulates a reset account', async () => {
  let finish; const f = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/certs/reader.pfx' }, post() { return new Promise(resolve => { finish = resolve; }); } });
  f.ui.show(); await tick(); f.get('certificate_password').value = 'late-secret'; f.get('certificate_password').emit('input'); const saving = f.form().emit('submit');
  f.setAdmin(false); f.ui.reset(); finish({ ...tlsState, connection_mode: 'https', certificate_path: '/certs/reader.pfx', certificate_password_set: true }); await saving;
  assert.equal(f.get('certificate_password').value, ''); assert.equal(f.get('certificate_path').value, ''); assert.equal(f.host.hidden, true);
  const fail = fixture({ state: { ...tlsState, connection_mode: 'https', certificate_path: '/certs/reader.pfx' }, post() { throw new Error('密码不正确: private-secret'); } }); fail.ui.show(); await tick();
  fail.get('certificate_password').value = 'private-secret'; fail.get('certificate_password').emit('input'); await fail.form().emit('submit'); assert.ok(!fail.walk(fail.host).some(node => node.textContent.includes('private-secret'))); assert.ok(fail.walk(fail.host).some(node => node.textContent.includes('已隐藏')));
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

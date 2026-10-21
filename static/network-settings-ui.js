/* Administrator network controls. Entry codes are never stored in browser storage. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleNetworkSettingsUI = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  function publicUrl(value) {
    const text = String(value || '').trim(); if (!text) return '';
    if (/[\s\\]/.test(text)) throw new Error('公网分享地址只能填写 http 或 https 的域名与端口。');
    let url; try { url = new URL(text); } catch (_) { throw new Error('请填写完整的公网分享地址，例如 https://photos.example.com。'); }
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('公网分享地址不能包含路径、账号、密码或查询参数。');
    return url.origin;
  }
  function payload(value) {
    let code = String(value.security_entry_code || '').trim();
    if (value.security_entry_enabled && !/^[A-Za-z0-9_-]{8,64}$/.test(code)) throw new Error('安全入口码需要 8–64 位英文字母、数字、下划线或短横线。');
    if (!value.security_entry_enabled && !/^[A-Za-z0-9_-]{8,64}$/.test(code)) code = '';
    return { security_entry_enabled: Boolean(value.security_entry_enabled), security_entry_code: code, share_port_enabled: Boolean(value.share_port_enabled), public_share_url: publicUrl(value.public_share_url) };
  }
  function valid(value) {
    return Boolean(value && typeof value.security_entry_enabled === 'boolean' && typeof value.security_entry_code === 'string' && typeof value.security_entry_path === 'string' && typeof value.share_port_enabled === 'boolean' && Number.isInteger(value.share_port) && value.share_port > 0 && value.share_port <= 65535 && typeof value.share_listening === 'boolean' && typeof value.public_share_url === 'string' && ['docker', 'desktop', 'source'].includes(value.runtime));
  }
  function entryUrl(code, enabled, base) {
    const url = new URL(base); url.pathname = enabled ? '/' + encodeURIComponent(code) : '/'; url.search = ''; url.hash = ''; return url.href;
  }
  function replaceEntry(value, win) {
    const path = value.security_entry_enabled ? value.security_entry_path : '/';
    if (value.security_entry_enabled && (!/^[A-Za-z0-9_-]{8,64}$/.test(value.security_entry_code) || path !== '/' + value.security_entry_code) || !/^\/[A-Za-z0-9_-]*$/.test(path)) throw new Error('服务端返回的安全入口不正确，请刷新后重试。');
    const url = new URL(win.location.href); url.pathname = path; url.search = ''; win.history.replaceState(win.history.state, '', url.pathname + url.hash);
  }
  function create({ host, api, post, isAdmin, toast = () => {}, onSaved = () => {}, window: injectedWindow }) {
    const doc = host.ownerDocument, win = injectedWindow || doc.defaultView || window;
    let generation = 0, active = false, snapshot = null, loading = null, busy = false, dirty = false, edits = 0;
    const authorized = () => typeof isAdmin === 'function' ? Boolean(isAdmin()) : Boolean(isAdmin);
    const el = (tag, cls, text) => { const node = doc.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
    const button = (text, handler, primary = false) => { const node = el('button', 'button ' + (primary ? 'button-primary' : 'button-secondary'), text); node.type = 'button'; if (handler) node.addEventListener('click', handler); return node; };
    const input = (name, label) => { const node = el('input'); node.name = name; node.id = 'network-' + name; node.setAttribute('aria-label', label); return node; };
    const toggle = (name, title, hint) => { const row = el('label', 'network-toggle-row'); const text = el('span', 'network-toggle-copy'); text.append(el('strong', '', title), el('small', '', hint)); const control = input(name, title); control.type = 'checkbox'; control.setAttribute('role', 'switch'); const track = el('span', 'network-toggle'); track.append(control, el('span', 'network-toggle-track')); row.append(text, track); return { row, control }; };
    host.classList.add('network-settings-page'); host.hidden = true;
    const form = el('form', 'network-settings-form');
    const error = el('p', 'network-error'); error.setAttribute('role', 'alert'); error.hidden = true;
    const status = el('p', 'network-save-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const security = el('section', 'settings-panel network-card'); security.append(el('h2', '', '安全入口'));
    const enabled = toggle('security_entry_enabled', '启用安全入口', '通过专属路径进入阅读器，降低登录入口被随意发现的机会。'); security.append(enabled.row);
    const entry = el('div', 'network-details'); const codeLabel = el('label', 'network-field'); codeLabel.append(el('span', '', '安全入口码')); const codeControls = el('div', 'network-input-actions'); const code = input('security_entry_code', '安全入口码'); code.type = 'text'; code.autocomplete = 'off'; code.spellcheck = false; code.maxLength = 64; code.minLength = 8; code.pattern = '[A-Za-z0-9_-]{8,64}'; code.placeholder = '8–64 位英文字母、数字、_ 或 -';
    const generate = button('随机生成', () => { try { const bytes = new Uint8Array(18); win.crypto.getRandomValues(bytes); code.value = Array.from(bytes, n => 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'[n & 63]).join(''); changed(); } catch (_) { showError('暂时无法生成入口码，请手动填写。'); } }); codeControls.append(code, generate); codeLabel.append(codeControls);
    const address = el('output', 'network-entry-url'); address.setAttribute('aria-label', '安全入口完整地址'); const copyEntry = button('复制地址', async () => { try { await win.navigator.clipboard.writeText(address.textContent); toast('安全入口地址已复制'); } catch (_) { toast('请选中安全入口地址后复制'); } }); const addressRow = el('div', 'network-address-row'); addressRow.append(address, copyEntry);
    entry.append(codeLabel, addressRow, el('p', 'network-hint', '保存后请收藏完整地址，并同步告知其他账号。安全入口只增加入口控制，不能代替密码和 HTTPS。'), el('p', 'network-hint network-recovery', '忘记入口时：Windows 或 macOS 请先停止阅读器，再通过启动器关闭安全入口。Docker 请先停止原容器，再按部署说明使用同一数据映射运行一次性恢复容器。恢复只关闭安全入口，不修改账号和写真。')); security.append(entry);
    const sharing = el('section', 'settings-panel network-card'); sharing.append(el('h2', '', '独立分享端口')); const separate = toggle('share_port_enabled', '启用独立分享端口', '公网只转发分享端口，登录与管理继续通过默认端口访问。'); sharing.append(separate.row);
    const shareDetails = el('div', 'network-details'); const portRow = el('div', 'network-port-row'); const port = el('strong', 'network-port'); const listening = el('span', 'network-listening'); listening.setAttribute('role', 'status'); portRow.append(port, listening); const portHint = el('p', 'network-hint'); shareDetails.append(portRow, portHint, el('p', 'network-hint', '此端口只提供分享页面及其必需资源，根地址、账号登录和管理接口不开放。')); sharing.append(shareDetails);
    const publicPanel = el('section', 'settings-panel network-card'); publicPanel.append(el('h2', '', '公网分享地址')); const publicLabel = el('label', 'network-field'); publicLabel.append(el('span', '', '分享链接前缀')); const publicInput = input('public_share_url', '公网分享地址'); publicInput.type = 'url'; publicInput.autocomplete = 'off'; publicInput.spellcheck = false; publicInput.placeholder = 'https://photos.example.com'; publicInput.maxLength = 2048; publicLabel.append(publicInput); const publicHint = el('p', 'network-hint'); publicPanel.append(publicLabel, publicHint);
    const actions = el('div', 'network-actions'); const refresh = button('刷新状态', () => load(true).catch(() => {})); const discard = button('放弃修改', () => { if (snapshot) { dirty = false; fill(snapshot); showError(''); update(); } }); const save = el('button', 'button button-primary', '保存网络设置'); save.type = 'submit'; actions.append(refresh, discard, save); form.append(security, sharing, publicPanel, actions, status); host.append(error, form);
    function showError(message, reveal = false) { error.textContent = message || ''; error.hidden = !message; if (message && reveal && active) error.scrollIntoView?.({ block: 'nearest' }); }
    function fill(value) { enabled.control.checked = value.security_entry_enabled; code.value = value.security_entry_code; separate.control.checked = value.share_port_enabled; publicInput.value = value.public_share_url; }
    function update() {
      const unavailable = busy || !snapshot; for (const control of [enabled.control, separate.control, code, generate, publicInput]) control.disabled = unavailable;
      code.disabled = generate.disabled = unavailable || !enabled.control.checked;
      code.required = enabled.control.checked; entry.hidden = !enabled.control.checked; shareDetails.hidden = !separate.control.checked; save.disabled = unavailable || Boolean(loading) || !dirty; discard.disabled = unavailable || !dirty; refresh.disabled = busy || Boolean(loading); form.setAttribute('aria-busy', String(busy || Boolean(loading)));
      status.textContent = busy ? '正在保存…' : dirty ? '有尚未保存的修改' : snapshot ? '设置已保存' : '正在读取网络设置…';
      try { address.textContent = entryUrl(code.value, true, win.location.href); } catch (_) { address.textContent = ''; } copyEntry.disabled = unavailable || !/^[A-Za-z0-9_-]{8,64}$/.test(code.value);
      port.textContent = snapshot ? '端口 ' + snapshot.share_port : '端口 —';
      listening.textContent = !snapshot?.share_port_enabled ? '未启用' : snapshot.share_listening ? '正在监听' : snapshot.share_error || '尚未开始监听'; listening.dataset.state = snapshot?.share_port_enabled && snapshot.share_listening ? 'ready' : 'idle';
      portHint.textContent = snapshot?.runtime === 'docker' ? 'Docker 容器内分享端口固定为 8081，请在部署时映射 8081。设置页面不修改容器端口；宿主机端口由部署映射决定。' : snapshot?.runtime === 'desktop' ? '如需修改分享端口，请在 Windows 或 macOS 启动器中设置。' : '分享端口由服务启动参数配置，设置页面仅负责启用或关闭。';
      publicHint.textContent = '仅填写 http / https 的域名和端口，不带 /share 等路径。留空将使用当前主机地址与端口，可能无法从外网访问。';
    }
    function changed() { dirty = true; edits++; showError(''); update(); }
    for (const control of [enabled.control, separate.control]) control.addEventListener('change', changed);
    for (const control of [code, publicInput]) control.addEventListener('input', changed);
    async function load(force = false) {
      if (!authorized()) throw new Error('仅管理员可管理网络设置。');
      if (busy) throw new Error('正在保存网络设置，请稍后重试。');
      if (loading) return loading; if (snapshot && !force) return snapshot;
      const token = generation, edit = edits;
      const operation = (async () => { try { const value = await api('/api/network-settings'); if (token !== generation || !authorized()) throw new Error('账户已切换。'); if (!valid(value)) throw new Error('网络设置格式不正确，请重试。'); snapshot = value; if (!dirty && edit === edits) fill(value); showError(''); update(); return value; } catch (problem) { if (token === generation && authorized()) { showError(problem.message || '暂时无法读取网络设置。'); } throw problem; } finally { if (token === generation) { loading = null; update(); } } })(); loading = operation; update(); return operation;
    }
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (busy || loading || !snapshot || !authorized() || !form.reportValidity()) return;
      let data; try { data = payload({ security_entry_enabled: enabled.control.checked, security_entry_code: code.value, share_port_enabled: separate.control.checked, public_share_url: publicInput.value }); } catch (problem) { showError(problem.message, true); return; }
      const token = generation; busy = true; showError(''); update();
      try { const value = await post('/api/network-settings', data); if (token !== generation || !authorized()) return; if (!valid(value)) throw new Error('服务端未返回完整网络设置，请刷新状态核对。'); replaceEntry(value, win); snapshot = value; dirty = false; fill(value); onSaved(value); toast('网络设置已保存'); }
      catch (problem) { if (token === generation && authorized()) showError(problem.message || '保存失败，请重试。', true); }
      finally { if (token === generation) { busy = false; update(); } }
    });
    return { getSettings: load, show() { if (!authorized()) return; host.hidden = false; if (!active) { active = true; load(true).catch(() => {}); } }, hide() { active = false; host.hidden = true; }, reset() { generation++; active = false; snapshot = null; loading = null; busy = dirty = false; edits++; code.value = publicInput.value = ''; enabled.control.checked = separate.control.checked = false; host.hidden = true; showError(''); update(); } };
  }
  return { publicUrl, payload, valid, entryUrl, replaceEntry, create };
});

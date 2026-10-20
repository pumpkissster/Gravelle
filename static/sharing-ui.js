/* Administrator-only creation and management of private work shares. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleSharingUI = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const labels = Object.freeze({ active: '分享中', expired: '已到期', closed: '已关闭', exhausted: '已结束', unavailable: '作品不可用' });
  function payload(book, values = {}) {
    if (!book?.id) throw new Error('请选择要分享的作品。');
    const password = String(values.password || '');
    if (password && (password.length < 8 || password.length > 64)) throw new Error('访问密码需要 8–64 个字符。');
    if (values.kind === 'visits' || values.mode === 'visits' || values.max_visits !== undefined) throw new Error('请选择分享有效期。');
    const days = Number(values.duration_days ?? 1);
    if (![1, 3, 7].includes(days)) throw new Error('请选择 1 天、3 天或 7 天。');
    return { book_id: book.id, mode: 'time', days, ...(password ? { password } : {}) };
  }
  function timestamp(value) { return new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value).getTime(); }
  function date(value) { const result = new Date(timestamp(value)); return Number.isFinite(result.getTime()) ? result.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—'; }
  function status(share) {
    if (share.status && share.status !== 'active') return labels[share.status] || '不可访问';
    if (share.expires_at && timestamp(share.expires_at) <= Date.now()) return labels.expired;
    return labels.active;
  }
  function rule(share) { return share.expires_at ? `有效至 ${date(share.expires_at)}` : '—'; }
  function shareUrl(share, baseUrl) {
    const base = new URL(baseUrl || globalThis.location?.href || 'http://localhost/');
    const token = String(share.token || '');
    if (token && /^[A-Za-z0-9_-]{43}$/.test(token)) return new URL('/share/' + token, base).href;
    const value = new URL(share.url || '', base);
    if (value.origin !== base.origin || !/^\/share\/[A-Za-z0-9_-]{43}$/.test(value.pathname) || value.search || value.hash) throw new Error('分享地址格式不正确。');
    return value.href;
  }
  function create({ api, post, isAdmin, toast = () => {}, baseUrl, managementHost, document: injectedDocument, window: injectedWindow } = {}) {
    const doc = injectedDocument || managementHost?.ownerDocument || document; const win = injectedWindow || doc.defaultView || window;
    let generation = 0, createEpoch = 0, managementEpoch = 0, operationEpoch = 0;
    let createBusy = false, managementBusy = false, operationBusy = false, managementActive = false, currentBook = null, pendingOperation = null, timer = null, selectedDays = 1, createFromManagement = false;
    const passwordEntries = new Set();
    const element = (tag, cls, text) => { const node = doc.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
    const button = (text, fn, primary = false) => { const node = element('button', 'button ' + (primary ? 'button-primary' : 'button-secondary'), text); node.type = 'button'; if (fn) node.addEventListener('click', fn); return node; };
    const closeButton = (label, fn) => { const node = button('', fn); node.className = 'sharing-close'; node.setAttribute('aria-label', label); const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); const use = doc.createElementNS('http://www.w3.org/2000/svg', 'use'); use.setAttribute('href', '#i-close'); svg.append(use); node.append(svg); return node; };
    const authorized = () => typeof isAdmin === 'function' ? Boolean(isAdmin()) : Boolean(isAdmin);
    const showError = (node, value) => { node.textContent = value || ''; node.hidden = !value; };

    const dialog = element('dialog', 'sharing-dialog'); dialog.setAttribute('aria-labelledby', 'sharing-title');
    const header = element('header', 'sharing-header'); const title = element('h2', '', '分享写真'); title.id = 'sharing-title'; const close = closeButton('关闭', () => closeCreate()); header.append(title, close);
    const body = element('div', 'sharing-body'); const error = element('p', 'sharing-error'); error.setAttribute('role', 'alert'); error.hidden = true; const bookName = element('p', 'sharing-book-title');
    const form = element('form', 'sharing-form'); form.id = 'sharing-create-form'; const passwordLabel = element('label', 'sharing-field'); passwordLabel.append(element('span', '', '访问密码'));
    const password = element('input'); password.type = 'text'; password.autocomplete = 'off'; password.minLength = 8; password.maxLength = 64; password.placeholder = '留空自动生成'; password.setAttribute('aria-label', '访问密码'); passwordLabel.append(password);
    const daysLabel = element('div', 'sharing-field'); const durationLabel = element('span', '', '有效期'); durationLabel.id = 'sharing-duration-label'; const duration = element('div', 'sharing-duration'); duration.setAttribute('role', 'radiogroup'); duration.setAttribute('aria-labelledby', durationLabel.id);
    const choices = [1, 3, 7].map(value => {
      const control = button(value + ' 天', () => choose(value)); control.classList.add('sharing-duration-option'); control.setAttribute('role', 'radio'); control.dataset.days = String(value);
      control.addEventListener('keydown', event => {
        const index = [1, 3, 7].indexOf(value); let next;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % 3;
        else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index + 2) % 3;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = 2;
        else return;
        event.preventDefault(); choose([1, 3, 7][next]); choices[next].focus();
      }); duration.append(control); return control;
    });
    daysLabel.append(durationLabel, duration); form.append(passwordLabel, daysLabel);
    const result = element('section', 'sharing-result'); result.hidden = true; const resultTitle = element('h3', '', '分享已创建'); const resultLink = element('input'); resultLink.readOnly = true; resultLink.setAttribute('aria-label', '分享链接'); const resultPassword = element('input'); resultPassword.readOnly = true; resultPassword.setAttribute('aria-label', '分享访问密码'); const resultRule = element('p', 'sharing-hint'); const copyAll = button('复制链接和密码', () => copy(`分享链接：${resultLink.value}\n访问密码：${resultPassword.value}`, resultLink), true); result.append(resultTitle, element('label', 'sharing-label', '分享链接'), resultLink, element('label', 'sharing-label', '访问密码'), resultPassword, resultRule, copyAll);
    body.append(error, bookName, form, result);
    const footer = element('footer', 'sharing-footer'); const cancel = button('取消', () => closeCreate()); const submit = element('button', 'button button-primary', '创建分享'); submit.type = 'submit'; submit.setAttribute('form', form.id); footer.append(cancel, submit); dialog.append(header, body, footer); doc.body.append(dialog);

    const host = managementHost || element('section', 'sharing-management-page'); if (!managementHost) doc.body.append(host); host.classList.add('sharing-management-page'); host.hidden = true;
    const managementError = element('p', 'sharing-error'); managementError.setAttribute('role', 'alert'); managementError.hidden = true;
    const management = element('section', 'sharing-management'); const list = element('div', 'sharing-list'); const empty = element('p', 'sharing-empty', '还没有创建分享。'); const refresh = button('刷新', () => load()); const listTop = element('div', 'sharing-list-top'); const listCount = element('span'); listTop.append(listCount, refresh); management.append(listTop, empty, list); host.append(managementError, management);

    const confirm = element('dialog', 'sharing-dialog sharing-confirm-dialog'); confirm.setAttribute('aria-labelledby', 'sharing-confirm-title');
    const confirmHeader = element('header', 'sharing-header'); const confirmTitle = element('h2'); confirmTitle.id = 'sharing-confirm-title'; const confirmClose = closeButton('关闭确认窗口', () => closeConfirmation()); confirmHeader.append(confirmTitle, confirmClose);
    const confirmBody = element('div', 'sharing-body'); const confirmText = element('p', 'sharing-confirm-text'); const operationError = element('p', 'sharing-error'); operationError.setAttribute('role', 'alert'); operationError.hidden = true; confirmBody.append(confirmText, operationError);
    const confirmFooter = element('footer', 'sharing-footer'); const cancelOperation = button('取消', () => closeConfirmation()); const executeOperation = button('关闭分享', runOperation); executeOperation.classList.add('sharing-danger'); confirmFooter.append(cancelOperation, executeOperation); confirm.append(confirmHeader, confirmBody, confirmFooter); doc.body.append(confirm);

    function choose(value) { selectedDays = value; for (const control of choices) { const checked = Number(control.dataset.days) === value; control.setAttribute('aria-checked', String(checked)); control.tabIndex = checked ? 0 : -1; } }
    function setCreateBusy(value) { createBusy = value; close.disabled = cancel.disabled = submit.disabled = value; for (const control of [password, ...choices]) control.disabled = value; dialog.setAttribute('aria-busy', String(value)); }
    function setOperationBusy(value) { operationBusy = value; confirmClose.disabled = cancelOperation.disabled = executeOperation.disabled = value; confirm.setAttribute('aria-busy', String(value)); }
    function closeCreate(force = false) { if (createBusy && !force) return; createEpoch++; if (dialog.open) dialog.close(); currentBook = null; password.value = resultPassword.value = resultLink.value = ''; createFromManagement = false; if (force) setCreateBusy(false); }
    function closeConfirmation(force = false) { if (operationBusy && !force) return; operationEpoch++; pendingOperation = null; if (confirm.open) confirm.close(); if (force) setOperationBusy(false); }
    function showCreate(book) {
      if (!authorized() || createBusy || operationBusy || !book?.id) return;
      createEpoch++; currentBook = book; createFromManagement = managementActive; showError(error, ''); result.hidden = true; form.hidden = submit.hidden = false; cancel.textContent = '取消'; bookName.textContent = book.title || '作品'; password.value = resultPassword.value = resultLink.value = ''; choose(1); if (!dialog.open) dialog.showModal(); password.focus();
    }
    async function copy(value, input) { try { if (!win.navigator?.clipboard?.writeText) throw new Error('clipboard unavailable'); await win.navigator.clipboard.writeText(value); toast('已复制'); } catch (_) { input?.focus(); input?.select(); toast('请选中内容后复制'); } }
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (createBusy || !authorized() || !form.reportValidity()) return; let data;
      try { data = payload(currentBook, { password: password.value, duration_days: selectedDays }); } catch (problem) { showError(error, problem.message); return; }
      const account = generation, epoch = createEpoch; setCreateBusy(true); showError(error, '');
      try {
        const value = await post('/api/shares', data); if (account !== generation || epoch !== createEpoch || !authorized()) return;
        const share = value.share || value; resultLink.value = shareUrl(share, baseUrl || win.location.href); resultPassword.value = String(value.password || data.password || ''); if (!resultPassword.value) throw new Error('服务端未返回访问密码，请关闭该分享后重试。'); resultRule.textContent = rule(share); form.hidden = submit.hidden = true; result.hidden = false; cancel.textContent = '完成'; password.value = ''; resultLink.focus(); if (managementActive) await load(true);
      } catch (problem) { if (account === generation && epoch === createEpoch) showError(error, problem.message || '创建失败，请重试。'); }
      finally { if (account === generation && epoch === createEpoch) setCreateBusy(false); }
    });
    function promptOperation(kind, share) {
      if (!authorized() || operationBusy || !managementActive) return;
      pendingOperation = { kind, share }; operationEpoch++; showError(operationError, ''); confirmTitle.textContent = kind === 'delete' ? '删除分享记录' : '关闭分享'; executeOperation.textContent = kind === 'delete' ? '删除记录' : '关闭分享'; confirmText.textContent = kind === 'delete' ? `删除“${share.title || '作品'}”的分享记录？仅删除分享记录，作品不受影响。该链接保持关闭。` : `关闭“${share.title || '作品'}”的分享？关闭后链接立即失效。`; if (!confirm.open) confirm.showModal(); executeOperation.focus();
    }
    function clearPassword(entry, dispose = false) { entry.sequence++; entry.busy = false; entry.field.value = ''; entry.copy.disabled = true; entry.panel.hidden = true; entry.reveal.textContent = '查看密码'; entry.reveal.disabled = dispose; entry.reveal.setAttribute('aria-expanded', 'false'); entry.reveal.setAttribute('aria-busy', 'false'); showError(entry.error, ''); if (dispose) entry.alive = false; }
    function clearPasswords() { for (const entry of passwordEntries) clearPassword(entry, true); passwordEntries.clear(); }
    function passwordsInUse() { return [...passwordEntries].some(entry => entry.busy || !entry.panel.hidden); }
    function addPassword(share, card, actions) {
      if (share.password_available !== true) { const unavailable = button('原密码不可查看'); unavailable.disabled = true; actions.append(unavailable); return; }
      const panel = element('div', 'sharing-password-panel'); panel.hidden = true;
      const label = element('label', 'sharing-password-label', '访问密码'); const field = element('input', 'sharing-password-value'); field.type = 'text'; field.readOnly = true; field.autocomplete = 'off'; field.spellcheck = false; field.setAttribute('aria-label', '分享访问密码');
      const controls = element('span', 'sharing-password-controls'); label.append(controls); controls.append(field); panel.append(label);
      const passwordError = element('p', 'sharing-password-error'); passwordError.setAttribute('role', 'alert'); passwordError.hidden = true;
      const entry = { field, panel, error: passwordError, reveal: null, copy: null, busy: false, alive: true, sequence: 0 };
      const copyPassword = button('复制', async () => {
        if (!entry.alive || entry.busy || panel.hidden || !field.value || !managementActive || !authorized()) return;
        const account = generation, epoch = managementEpoch, sequence = entry.sequence;
        const current = () => account === generation && epoch === managementEpoch && sequence === entry.sequence && entry.alive && managementActive && authorized() && !panel.hidden && Boolean(field.value);
        try { if (!win.navigator?.clipboard?.writeText) throw new Error('clipboard unavailable'); await win.navigator.clipboard.writeText(field.value); if (current()) toast('密码已复制'); }
        catch (_) { if (current()) { field.focus(); field.select(); toast('请选中密码后复制'); } }
      }); copyPassword.classList.add('sharing-copy-password'); copyPassword.setAttribute('aria-label', '复制访问密码'); copyPassword.disabled = true; entry.copy = copyPassword; controls.append(copyPassword);
      const reveal = button('查看密码', async () => {
        if (!entry.alive || entry.busy || !managementActive || !authorized()) return;
        if (!entry.panel.hidden) { clearPassword(entry); return; }
        const account = generation, epoch = managementEpoch, sequence = ++entry.sequence;
        entry.busy = true; reveal.disabled = true; reveal.textContent = '正在获取…'; reveal.setAttribute('aria-busy', 'true'); showError(passwordError, '');
        const current = () => account === generation && epoch === managementEpoch && sequence === entry.sequence && entry.alive && passwordEntries.has(entry) && managementActive && authorized();
        try {
          const value = await post('/api/shares/' + encodeURIComponent(share.id) + '/password', {}); const secret = value?.password; if (value && typeof value === 'object') value.password = '';
          if (!current()) return;
          if (typeof secret !== 'string' || [...secret].length < 8 || [...secret].length > 64) throw new Error('暂时无法查看密码，请重新分享。');
          field.value = secret; panel.hidden = false; copyPassword.disabled = false; reveal.textContent = '隐藏密码'; reveal.setAttribute('aria-expanded', 'true');
        } catch (problem) { if (current()) showError(passwordError, problem.message || '密码获取失败，请重试。'); }
        finally { if (current()) { entry.busy = false; reveal.disabled = false; reveal.setAttribute('aria-busy', 'false'); if (panel.hidden) reveal.textContent = '查看密码'; } }
      });
      reveal.setAttribute('aria-expanded', 'false'); entry.reveal = reveal; passwordEntries.add(entry); actions.append(reveal); card.append(panel, passwordError);
    }
    async function load(force = false) {
      if (managementBusy && !force || !managementActive || !authorized()) return; clearPasswords(); if (force) managementEpoch++; const account = generation, epoch = managementEpoch; managementBusy = true; refresh.disabled = true; refresh.setAttribute('aria-busy', 'true'); showError(managementError, '');
      try {
        const value = await api('/api/shares'); if (account !== generation || epoch !== managementEpoch || !managementActive || !authorized()) return;
        const shares = Array.isArray(value.shares) ? value.shares : []; list.replaceChildren(); empty.hidden = shares.length > 0; listCount.textContent = `${shares.length} 个分享`;
        for (const share of shares) {
          const card = element('article', 'sharing-card'); const top = element('div', 'sharing-card-top'); const name = element('strong', '', share.title || '作品'); const badge = element('span', 'sharing-badge', status(share)); badge.dataset.status = share.status || 'active'; top.append(name, badge);
          const info = element('p', 'sharing-meta', `创建于 ${date(share.created_at)}`); const limit = element('p', 'sharing-meta', rule(share)); const visits = element('p', 'sharing-meta sharing-visits', `访问 ${Number.isSafeInteger(share.visits) && share.visits >= 0 ? share.visits : 0} 次`); visits.title = '成功建立阅读会话计数，刷新和翻页不重复计数。'; const actions = element('div', 'sharing-card-actions'); let copyField = null;
          actions.append(button('复制链接', () => { try { const url = shareUrl(share, baseUrl || win.location.href); if (!copyField) { copyField = element('input', 'sharing-copy-field'); copyField.readOnly = true; card.append(copyField); } copyField.value = url; copy(url, copyField); } catch (problem) { showError(managementError, problem.message); } }));
          if ([labels.active, labels.exhausted].includes(status(share))) actions.append(button('关闭分享', () => promptOperation('close', share)));
          if (share.status === 'closed') {
            const recreate = button('重新分享', () => showCreate({ id: share.book_id, title: share.title })); recreate.disabled = !share.book_id; const remove = button('删除记录', () => promptOperation('delete', share)); remove.classList.add('sharing-danger'); actions.append(recreate, remove);
          }
          card.append(top, info); if (share.expires_at) card.append(limit); card.append(visits, actions); addPassword(share, card, actions); list.append(card);
        }
      } catch (problem) { if (account === generation && epoch === managementEpoch) showError(managementError, problem.message || '暂时无法获取分享。'); }
      finally { if (account === generation && epoch === managementEpoch) { managementBusy = false; refresh.disabled = false; refresh.setAttribute('aria-busy', 'false'); } }
    }
    async function runOperation() {
      if (operationBusy || !pendingOperation || !authorized() || !managementActive) return;
      const selected = pendingOperation, account = generation, epoch = operationEpoch; setOperationBusy(true); showError(operationError, '');
      try {
        await post('/api/shares/' + encodeURIComponent(selected.share.id) + '/' + selected.kind, {}); if (account !== generation || epoch !== operationEpoch || !managementActive) return;
        setOperationBusy(false); closeConfirmation(); toast(selected.kind === 'delete' ? '分享记录已删除' : '分享已关闭'); await load(true);
      } catch (problem) { if (account === generation && epoch === operationEpoch) showError(operationError, problem.message || '操作失败，请重试。'); }
      finally { if (account === generation && epoch === operationEpoch) setOperationBusy(false); }
    }
    function hideManagement() { if (!managementActive) return; managementActive = false; managementEpoch++; managementBusy = false; clearPasswords(); win.clearInterval(timer); timer = null; host.hidden = true; closeConfirmation(true); if (createFromManagement) closeCreate(true); }
    function showManagement() { if (!authorized() || managementActive) return; managementActive = true; managementEpoch++; host.hidden = false; load(); timer = win.setInterval(() => { if (!doc.hidden && !confirm.open && !passwordsInUse()) load(); }, 30000); }
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeCreate(); }); confirm.addEventListener('cancel', event => { event.preventDefault(); closeConfirmation(); });
    choose(1);
    return { showCreate, showManagement, hideManagement, reset() { generation++; hideManagement(); clearPasswords(); closeCreate(true); closeConfirmation(true); managementBusy = false; list.replaceChildren(); showError(error, ''); showError(managementError, ''); host.hidden = true; } };
  }
  return { create, payload, status, rule, shareUrl, date };
});

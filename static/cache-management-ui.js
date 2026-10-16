/* Administrator server cache controls. Device reading memory is configured separately. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleCacheManagementUI = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const labels = Object.freeze({ covers: '作品封面', library_covers: '写真库封面', optimized: '优化图片' });
  function bytes(value) {
    const size = Math.max(0, Number(value) || 0); if (size < 1024) return size + ' B';
    const exponent = Math.min(4, Math.floor(Math.log(size) / Math.log(1024)));
    return (size / (1024 ** exponent)).toLocaleString('zh-CN', { maximumFractionDigits: 1 }) + ' ' + ['B', 'KiB', 'MiB', 'GiB', 'TiB'][exponent];
  }
  function clearDescription(kind) {
    if (kind === 'optimized') return '清除已完成作品的在用优化图片，并将这些作品切回原图。不会删除原始文件、作品资料或阅读记录；需要再次选择优化才会重新生成。正在处理、等待发布或等待替换的图片不会清除。';
    if (kind === 'library_covers') return '清除生成的写真库封面图片，保留已选择的封面组。再次访问时可重新生成。';
    if (kind === 'covers') return '清除从作品内页生成的封面缓存，再次访问时重新生成。手工上传的封面与原始图片保留。';
    return '';
  }
  function validSnapshot(value) {
    return Boolean(value && value.settings && Number.isInteger(value.settings.revision) &&
      Number.isInteger(value.settings.cover_limit_mb) && value.settings.cover_limit_mb >= 0 && Array.isArray(value.categories));
  }
  function create({ host, api, post, toast = () => {}, onCatalogChange = () => {} }) {
    const document = host.ownerDocument;
    let active = false; let snapshot = null; let generation = 0; let loading = false; let busy = false; let dirty = false; let draftRevision = null; let timer = null; let operation = null;
    const controls = new Map();
    function el(tag, className, text) { const item = document.createElement(tag); if (className) item.className = className; if (text !== undefined) item.textContent = String(text); return item; }
    function button(text, handler, primary = false) { const item = el('button', 'button ' + (primary ? 'button-primary' : 'button-secondary'), text); item.type = 'button'; if (handler) item.addEventListener('click', handler); return item; }
    host.classList.add('cache-management-page');
    const error = el('p', 'notice error'); error.setAttribute('role', 'alert'); error.hidden = true;
    const overview = el('section', 'settings-panel cache-overview'); const heading = el('div', 'settings-section-heading'); heading.append(el('h2', '', '服务端缓存')); const refresh = button('刷新', () => load()); heading.append(refresh); const total = el('p', 'cache-total'); overview.append(heading, total);
    const list = el('div', 'cache-categories');
    Object.entries(labels).forEach(([kind, name]) => {
      const card = el('section', 'settings-panel cache-category'); const top = el('div', 'settings-section-heading'); top.append(el('h2', '', name)); const size = el('strong', 'cache-size', '—'); top.append(size);
      const info = el('p', 'settings-description'); const policy = el('p', 'cache-policy'); const actions = el('div', 'cache-actions'); const clear = button(kind === 'optimized' ? '清除在用优化图' : '清除缓存', () => confirmClear(kind)); actions.append(clear);
      let old = null; if (kind === 'optimized') { old = button('清理旧副本', confirmOld); actions.append(old); }
      card.append(top, info, policy, actions); list.append(card); controls.set(kind, { size, info, policy, clear, old });
    });
    const settingsPanel = el('section', 'settings-panel'); settingsPanel.append(el('h2', '', '封面容量'));
    const form = el('form', 'cache-settings-form'); const label = el('label', 'settings-field'); label.append(el('span', '', '可再生封面容量上限')); const wrap = el('span', 'cache-limit-input'); const limit = el('input'); limit.type = 'number'; limit.min = 0; limit.max = 1048576; limit.step = 1; limit.required = true; limit.inputMode = 'numeric'; limit.setAttribute('aria-label', '可再生封面容量上限，MiB'); wrap.append(limit, el('span', '', 'MiB')); label.append(wrap);
    const hint = el('p', 'settings-description', '作品内页封面与写真库封面共用此上限，0 表示不限。手工上传封面与优化图片不自动回收。'); const save = el('button', 'button button-primary', '保存'); save.type = 'submit'; const discard = button('放弃修改', () => { dirty = false; draftRevision = null; if (snapshot) limit.value = snapshot.settings.cover_limit_mb; update(); }); const saveBar = el('div', 'cache-actions'); saveBar.append(discard, save); form.append(label, hint, saveBar); settingsPanel.append(form);
    limit.addEventListener('input', () => { if (!dirty) draftRevision = snapshot?.settings.revision ?? null; dirty = true; update(); });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (busy || !snapshot || !form.reportValidity()) return;
      const value = Number(limit.value); if (!Number.isInteger(value) || value < 0 || value > 1048576) return;
      const token = generation; busy = true; update();
      try { const result = await post('/api/cache/settings', { expected_revision: draftRevision ?? snapshot.settings.revision, cover_limit_mb: value }); if (token !== generation) return; dirty = false; draftRevision = null; accept(result); toast('缓存设置已保存'); }
      catch (problem) { if (token === generation && problem.status !== 401) showError(problem.message); }
      finally { if (token === generation) { busy = false; update(); } }
    });
    host.append(error, overview, list, settingsPanel);
    const dialog = el('dialog', 'settings-dialog library-dialog cache-confirm-dialog'); dialog.setAttribute('aria-labelledby', 'cache-confirm-title');
    const dialogHeader = el('header', 'dialog-header'); const dialogTitle = el('h2'); dialogTitle.id = 'cache-confirm-title'; const close = button('×', () => dialog.close()); close.className = 'icon-button'; close.setAttribute('aria-label', '关闭'); dialogHeader.append(dialogTitle, close);
    const dialogBody = el('div', 'dialog-body'); const description = el('p'); dialogBody.append(description); const dialogError = el('p', 'form-error'); dialogError.setAttribute('role', 'alert'); dialogError.hidden = true; dialogBody.append(dialogError);
    const footer = el('footer', 'dialog-footer'); const cancel = button('取消', () => dialog.close()); const confirm = button('确认清除', () => runOperation(), true); footer.append(cancel, confirm); dialog.append(dialogHeader, dialogBody, footer); document.body.append(dialog);
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    function showError(message) { error.textContent = message || ''; error.hidden = !message; }
    function update() {
      refresh.disabled = loading || busy; refresh.setAttribute('aria-busy', String(loading)); limit.disabled = busy || !snapshot; save.disabled = discard.disabled = busy || !snapshot || !dirty;
      close.disabled = cancel.disabled = confirm.disabled = busy;
      controls.forEach((control, kind) => { const data = snapshot?.categories.find(item => item.kind === kind); control.clear.disabled = busy || loading || !data?.can_clear; if (control.old) control.old.disabled = busy || loading || !(Number(data?.reclaimable_files) > 0); });
    }
    function accept(value) {
      if (!validSnapshot(value)) throw new Error('缓存数据格式不正确。'); snapshot = value;
      total.textContent = `已登记缓存 ${bytes(snapshot.summary?.bytes)}${Number(snapshot.summary?.protected_cover_bytes) > 0 ? ` · 上传封面 ${bytes(snapshot.summary.protected_cover_bytes)} 保留` : ''}${snapshot.summary?.truncated ? ' · 本次统计不完整，目录项目过多' : ''}`;
      controls.forEach((control, kind) => {
        const data = snapshot.categories.find(item => item.kind === kind) || {};
        control.size.textContent = bytes(data.bytes); control.info.textContent = `${Number(data.files) || 0} 个文件`;
        control.policy.textContent = kind === 'optimized' ? `在用 ${bytes(data.active_bytes)} · 保留副本 ${bytes(data.waiting_bytes)} · 旧副本 ${bytes(data.reclaimable_bytes)}。仅管理员手动清除。` : kind === 'covers' ? `可重新生成。${Number(data.protected_files) || 0} 个上传封面保留。` : '可重新生成，保留当前封面组。';
      });
      if (!dirty) limit.value = snapshot.settings.cover_limit_mb; showError(''); update();
    }
    async function load() {
      if (!active || loading || busy) return; const token = generation; loading = true; update();
      try { const value = await api('/api/cache'); if (token === generation) accept(value); }
      catch (problem) { if (token === generation && problem.status !== 401) showError(problem.message); }
      finally { if (token === generation) { loading = false; update(); } }
    }
    function confirmClear(kind) {
      if (busy || loading || !snapshot?.categories.find(item => item.kind === kind)?.can_clear) return;
      operation = { kind }; dialogTitle.textContent = '清除' + labels[kind]; description.textContent = clearDescription(kind); dialogError.hidden = true; confirm.textContent = '确认清除'; dialog.showModal();
    }
    function confirmOld() {
      if (busy || loading) return; operation = { kind: 'old' }; dialogTitle.textContent = '清理旧副本'; description.textContent = '只清理未被在用或等待发布版本引用的优化图，原始图片和有效优化图保留。'; dialogError.hidden = true; confirm.textContent = '确认清理'; dialog.showModal();
    }
    async function runOperation() {
      if (busy || !operation) return; const token = generation; const selected = operation; busy = true; dialogError.hidden = true; update();
      try {
        const value = selected.kind === 'old' ? await post('/api/transcoding/cleanup', {}) : await post('/api/cache/clear', { kind: selected.kind, confirm: true });
        if (token !== generation) return;
        const payload = selected.kind === 'old' ? await api('/api/cache') : value; if (token !== generation) return; accept(payload); dialog.close(); operation = null; toast(selected.kind === 'optimized' ? '优化图片已清除，作品已切回原图' : '缓存已清理');
        await Promise.resolve(onCatalogChange()).catch(() => {});
      } catch (problem) { if (token === generation && problem.status !== 401) { dialogError.textContent = problem.message; dialogError.hidden = false; } }
      finally { if (token === generation) { busy = false; update(); } }
    }
    update(); host.hidden = true;
    return {
      show() { active = true; host.hidden = false; load(); clearInterval(timer); timer = setInterval(() => { if (!document.hidden) load(); }, 30000); },
      hide() { active = false; host.hidden = true; clearInterval(timer); timer = null; if (dialog.open && !busy) dialog.close(); },
      reset() { generation += 1; active = loading = busy = dirty = false; snapshot = operation = draftRevision = null; clearInterval(timer); timer = null; if (dialog.open) dialog.close(); showError(''); host.hidden = true; limit.value = ''; update(); },
      refresh: load
    };
  }
  return { create, bytes, clearDescription, validSnapshot };
});

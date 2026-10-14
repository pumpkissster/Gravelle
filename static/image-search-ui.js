/* Local image lookup. Uploads and results stay in memory for the current account. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleImageSearchUI = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const maxBytes = 64 * 1024 * 1024;
  function fileError(file) {
    if (!file || !Number.isFinite(file.size) || file.size < 1) return '请选择有效的图片。';
    if (file.size > maxBytes) return '图片不能超过 64 MiB。';
    return '';
  }
  function imageType(bytes) {
    if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
    if ([137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => bytes[index] === value)) return 'image/png';
    if ([82, 73, 70, 70].every((value, index) => bytes[index] === value) && [87, 69, 66, 80].every((value, index) => bytes[index + 8] === value)) return 'image/webp';
    return '';
  }
  function statusMessage(status, admin = false) {
    if (!status) return '正在读取图片搜索状态…';
    if (!status.enabled) return admin ? '图片索引未启用。' : '图片搜索尚未启用，请联系管理员。';
    if (status.paused) return status.available ? '图片索引已暂停，仍可搜索已收录的图片。' : '图片索引已暂停。';
    if (status.indexing) return status.available ? '索引更新中，可先搜索已收录的图片。' : '正在建立图片索引，请稍后再试。';
    if (!status.available) return '暂无可搜索的图片。';
    return '搜索你可访问的写真库中的相同或近似图片。';
  }
  function normalizeResults(payload) {
    const seen = new Set();
    return (Array.isArray(payload?.results) ? payload.results : []).filter(result => {
      if (!result || !result.book || typeof result.book.id !== 'string' || !result.book.id || !Number.isSafeInteger(result.page) || result.page < 0 || !['exact', 'similar'].includes(result.match)) return false;
      const key = JSON.stringify([result.book.id, result.page]);
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
  }
  function createRequestGate() {
    let generation = 0; let controller = null;
    return {
      begin() { controller?.abort(); controller = new AbortController(); const request = controller; const token = ++generation; return { signal: request.signal, current: () => token === generation && !request.signal.aborted }; },
      cancel() { generation += 1; controller?.abort(); controller = null; }
    };
  }
  function create({ searchHost, settingsHost, api, post, openBook, isAdmin, toast = () => {}, onManage = () => {} }) {
    const document = searchHost.ownerDocument;
    const state = { active: false, managing: false, status: null, file: null, mime: '', url: '', results: [], searched: false, searching: false, busy: false, timer: null, statusRequest: null, generation: 0, fileGeneration: 0, catalog: '' };
    const queryGate = createRequestGate(); const search = {}; const settings = {};
    function el(tag, cls, text) { const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined) item.textContent = String(text); return item; }
    function button(text, cls, action) { const item = el('button', cls || 'button button-secondary', text); item.type = 'button'; item.addEventListener('click', action); return item; }
    function error(target, message) { target.textContent = message || ''; target.hidden = !message; }
    function releasePreview() { if (state.url) URL.revokeObjectURL(state.url); state.url = ''; search.preview.removeAttribute('src'); }
    function cancelQuery() { queryGate.cancel(); state.searching = false; renderSearch(); }
    function clearResults() { state.results = []; state.searched = false; renderResults(); }
    function stopStatus() { clearTimeout(state.timer); state.timer = null; }
    function scheduleStatus() { stopStatus(); if (state.active || state.managing) state.timer = setTimeout(() => loadStatus(), state.status?.indexing ? 2500 : 7000); }
    function renderSearch() {
      search.message.textContent = statusMessage(state.status, isAdmin());
      search.manage.hidden = !isAdmin() || Boolean(state.status?.enabled && state.status?.available);
      search.run.disabled = !state.file || !state.status?.enabled || !state.status?.available || state.searching;
      search.run.textContent = state.searching ? '正在搜索…' : '开始搜索';
      search.cancel.hidden = !state.searching;
      search.copy.hidden = Boolean(state.file);
      search.previewBox.hidden = !state.file;
      search.input.setAttribute('aria-label', state.file ? '更换图片' : '选择图片');
      search.drop.classList.toggle('has-image', Boolean(state.file));
      searchHost.setAttribute('aria-busy', String(state.searching));
    }
    function renderResults() {
      search.results.replaceChildren();
      search.resultHeading.hidden = !state.searched;
      search.resultHeading.textContent = state.results.length ? `找到 ${state.results.length} 个匹配页面${state.results.length >= 30 ? '（最多显示 30 个）' : ''}` : '没有找到匹配的图片';
      search.empty.hidden = !state.searched || Boolean(state.results.length);
      search.empty.textContent = state.status?.indexing ? '索引仍在更新，可以稍后再试。' : '可尝试保留更多画面，或使用更清晰的图片。';
      state.results.forEach(result => {
        const card = el('button', 'image-search-result'); card.type = 'button';
        card.setAttribute('aria-label', `阅读《${result.book.title || '作品'}》第 ${result.page + 1} 页`);
        const thumbnail = el('div', 'image-search-thumbnail'); const image = el('img'); image.alt = ''; image.loading = 'lazy'; image.decoding = 'async'; image.src = '/api/image-search/thumbnail/' + encodeURIComponent(result.book.id) + '/' + result.page;
        image.addEventListener('error', () => { image.remove(); thumbnail.classList.add('unavailable'); thumbnail.textContent = '预览不可用'; }, { once: true });
        thumbnail.append(image, el('span', 'image-search-match', result.match === 'exact' ? '相同图片' : '近似图片'));
        const info = el('div', 'image-search-result-info'); const title = el('strong', '', result.book.title || '未命名作品'); title.title = title.textContent;
        const models = (result.book.models || []).map(model => model.name).filter(Boolean).join(' / ');
        info.append(title, el('span', '', [models, result.book.library_name].filter(Boolean).join(' · ')), el('small', '', `第 ${result.page + 1} 页`));
        card.append(thumbnail, info); card.addEventListener('click', () => openBook(result.book, result.page)); search.results.append(card);
      });
    }
    async function chooseFile(file) {
      const generation = ++state.fileGeneration; cancelQuery();
      const invalid = fileError(file); if (invalid) { error(search.error, invalid); return; }
      let mime;
      try { mime = imageType(new Uint8Array(await file.slice(0, 16).arrayBuffer())); }
      catch (_) { if (generation === state.fileGeneration) error(search.error, '无法读取这张图片。'); return; }
      if (generation !== state.fileGeneration) return;
      if (!mime) { error(search.error, '支持 JPG、PNG 和 WebP 图片。'); return; }
      releasePreview(); clearResults(); error(search.error, ''); state.file = file; state.mime = mime; state.url = URL.createObjectURL(file); search.preview.src = state.url; search.filename.textContent = file.name || '粘贴的图片'; search.filename.title = search.filename.textContent; search.input.value = ''; renderSearch();
    }
    async function runQuery() {
      if (search.run.disabled) return;
      const request = queryGate.begin(); const file = state.file; state.searching = true; clearResults(); error(search.error, ''); renderSearch();
      try {
        const payload = await api('/api/image-search/query', { method: 'POST', body: file, headers: { 'Content-Type': state.mime }, signal: request.signal });
        if (!request.current()) return;
        state.results = normalizeResults(payload); state.searched = true;
        if (state.status) state.status.indexing = payload.indexing === true;
        renderResults();
      } catch (failure) { if (request.current() && failure.name !== 'AbortError' && failure.status !== 401) { clearResults(); error(search.error, failure.message || '图片搜索失败，请重试。'); } }
      finally { if (request.current()) { state.searching = false; renderSearch(); } }
    }
    searchHost.classList.add('image-search-page');
    search.message = el('p', 'image-search-status'); search.message.setAttribute('role', 'status');
    const statusRow = el('div', 'image-search-status-row'); search.manage = button('配置图片搜索', 'button button-secondary', onManage); statusRow.append(search.message, search.manage);
    search.drop = el('div', 'image-search-drop'); search.input = el('input'); search.input.type = 'file'; search.input.id = 'image-search-file'; search.input.accept = 'image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp'; search.input.className = 'sr-only image-search-file';
    const label = el('label', 'button button-secondary', '选择图片'); label.htmlFor = search.input.id;
    search.copy = el('div', 'image-search-upload-copy'); search.copy.append(el('strong', '', '用一张图片找到作品'), el('span', '', '拖入或粘贴图片 · JPG、PNG、WebP · 最大 64 MiB'), label);
    search.previewBox = el('div', 'image-search-preview'); search.previewBox.hidden = true; search.preview = el('img'); search.preview.alt = '待搜索的图片';
    search.filename = el('span', 'image-search-filename');
    const previewActions = el('div', 'image-search-preview-actions'); const replace = el('label', 'button button-secondary', '更换图片'); replace.htmlFor = search.input.id;
    const remove = button('移除图片', 'button button-secondary', () => { state.fileGeneration += 1; cancelQuery(); state.file = null; state.mime = ''; releasePreview(); clearResults(); error(search.error, ''); renderSearch(); search.input.focus(); });
    previewActions.append(replace, remove); search.previewBox.append(search.preview, search.filename, previewActions);
    search.drop.append(search.input, search.copy, search.previewBox);
    search.input.addEventListener('change', () => { if (search.input.files[0]) chooseFile(search.input.files[0]); });
    search.drop.addEventListener('dragover', event => { event.preventDefault(); search.drop.classList.add('is-dragging'); });
    search.drop.addEventListener('dragleave', event => { if (!search.drop.contains(event.relatedTarget)) search.drop.classList.remove('is-dragging'); });
    search.drop.addEventListener('drop', event => { event.preventDefault(); search.drop.classList.remove('is-dragging'); if (event.dataTransfer?.files?.length) chooseFile(event.dataTransfer.files[0]); });
    document.addEventListener('paste', event => {
      if (!state.active || searchHost.hidden || document.querySelector('dialog[open]') || event.target.closest?.('input:not([type="file"]),textarea,[contenteditable="true"]')) return;
      const item = Array.from(event.clipboardData?.items || []).find(item => item.kind === 'file' && item.type.startsWith('image/'));
      if (item) { const file = item.getAsFile(); if (file) { event.preventDefault(); chooseFile(file); } }
    });
    const actions = el('div', 'image-search-actions'); search.run = button('开始搜索', 'button button-primary', runQuery); search.cancel = button('取消', 'button button-secondary', cancelQuery); search.cancel.hidden = true; actions.append(search.run, search.cancel);
    search.error = el('p', 'notice error'); search.error.hidden = true; search.error.setAttribute('role', 'alert'); search.resultHeading = el('h2', 'image-search-results-title'); search.resultHeading.hidden = true;
    search.empty = el('p', 'image-search-status'); search.empty.hidden = true; search.results = el('div', 'image-search-results');
    searchHost.append(statusRow, search.drop, actions, search.error, search.resultHeading, search.empty, search.results);

    settingsHost.classList.add('image-search-settings');
    const panel = el('section', 'settings-panel'); panel.append(el('h2', '', '图片搜索'));
    const enableRow = el('label', 'setting-row'); const enableText = el('span'); enableText.append(el('strong', '', '建立图片索引'), el('small', '', '在服务端识别图片，用于库内搜索'));
    settings.enabled = el('input', 'image-search-toggle'); settings.enabled.type = 'checkbox'; settings.enabled.setAttribute('aria-label', '启用图片索引'); settings.enabled.addEventListener('change', () => mutate('/api/image-search/settings', { enabled: settings.enabled.checked })); enableRow.append(enableText, settings.enabled);
    settings.message = el('p', 'image-search-status'); settings.message.setAttribute('role', 'status');
    settings.progress = el('progress'); settings.progress.max = 100; settings.progress.setAttribute('aria-label', '图片索引进度'); settings.counts = el('div', 'image-search-index-counts');
    const tools = el('div', 'image-search-index-tools'); settings.pause = button('暂停', 'button button-secondary', () => mutate('/api/image-search/actions', { action: state.status?.paused ? 'resume' : 'pause' }));
    settings.retry = button('重试失败图片', 'button button-secondary', () => mutate('/api/image-search/actions', { action: 'retry' }));
    settings.rebuild = button('重建索引', 'button button-secondary', () => confirmAction('rebuild')); settings.clear = button('清理索引', 'button button-danger', () => confirmAction('clear')); settings.refresh = button('刷新', 'button button-secondary', () => loadStatus(true));
    tools.append(settings.pause, settings.retry, settings.rebuild, settings.clear, settings.refresh);
    settings.error = el('p', 'notice error'); settings.error.hidden = true; settings.error.setAttribute('role', 'alert');
    panel.append(enableRow, settings.message, settings.progress, settings.counts, tools, settings.error); settingsHost.append(panel);
    const confirm = el('dialog', 'settings-dialog image-search-confirm'); confirm.setAttribute('aria-labelledby', 'image-search-confirm-title'); const confirmHeader = el('header', 'dialog-header'); settings.confirmTitle = el('h2'); settings.confirmTitle.id = 'image-search-confirm-title'; const close = button('关闭', 'button button-secondary', () => confirm.close()); confirmHeader.append(settings.confirmTitle, close); settings.confirmText = el('p', 'settings-intro'); const footer = el('footer', 'dialog-footer'); const back = button('取消', 'button button-secondary', () => confirm.close()); settings.confirmRun = button('确认', 'button button-danger', async () => { const action = confirm.dataset.action; confirm.close(); await mutate('/api/image-search/actions', { action }); }); footer.append(back, settings.confirmRun); confirm.append(confirmHeader, settings.confirmText, footer); document.body.append(confirm);
    function confirmAction(action) { if (state.busy) return; confirm.dataset.action = action; settings.confirmTitle.textContent = action === 'clear' ? '清理图片索引？' : '重新建立图片索引？'; settings.confirmText.textContent = action === 'clear' ? '清除图片搜索索引并关闭搜索。原始图片、优化图片和阅读记录保留。' : '清除现有搜索索引并重新生成。原始图片和阅读资料保留。'; settings.confirmRun.textContent = action === 'clear' ? '清理并关闭' : '开始重建'; confirm.showModal(); }
    function renderSettings() {
      const snapshot = state.status;
      settings.enabled.checked = Boolean(snapshot?.enabled); settings.enabled.disabled = state.busy || !snapshot;
      settings.message.textContent = statusMessage(snapshot, true);
      const summary = snapshot?.summary || {}; const done = Math.max(0, Number(snapshot?.progress?.done) || 0); const total = Math.max(0, Number(snapshot?.progress?.total) || 0);
      settings.progress.value = total ? Math.min(100, done / total * 100) : 0; settings.progress.hidden = !snapshot?.indexing && !snapshot?.paused;
      settings.counts.textContent = snapshot ? `已索引 ${Number(summary.indexed) || 0} · 待处理 ${Number(summary.pending) || 0} · 失败 ${Number(summary.failed) || 0} · 共 ${Number(summary.total) || 0} 张` : '';
      settings.pause.textContent = snapshot?.paused ? '继续' : '暂停'; settings.pause.hidden = !snapshot?.indexing && !snapshot?.paused; settings.retry.hidden = !Number(summary.failed);
      settings.pause.disabled = settings.retry.disabled = state.busy || !snapshot?.enabled; settings.rebuild.disabled = state.busy || !snapshot?.enabled; settings.clear.disabled = state.busy || !snapshot || !(Number(summary.indexed) + Number(summary.pending) + Number(summary.failed)); settings.refresh.disabled = state.busy;
    }
    async function loadStatus(manual = false) {
      if (!(state.active || state.managing) || state.statusRequest || state.busy) { scheduleStatus(); return; }
      const generation = state.generation; const request = new AbortController(); state.statusRequest = request;
      try {
        const payload = await api('/api/image-search/status', { signal: request.signal });
        if (generation !== state.generation || request.signal.aborted) return;
        state.status = payload; error(settings.error, payload.error || ''); if (manual) error(search.error, ''); renderSearch(); renderSettings();
        if (!payload.enabled) { cancelQuery(); clearResults(); }
      } catch (failure) { if (generation === state.generation && !request.signal.aborted && failure.status !== 401) { error(state.managing ? settings.error : search.error, failure.message || '无法读取图片搜索状态。'); } }
      finally { if (state.statusRequest === request) { state.statusRequest = null; scheduleStatus(); } }
    }
    async function mutate(path, body) {
      if (!isAdmin() || state.busy) return;
      const generation = ++state.generation; state.statusRequest?.abort(); state.statusRequest = null; stopStatus(); state.busy = true; error(settings.error, ''); renderSettings();
      try { const payload = await post(path, body); if (generation !== state.generation) return; state.status = payload; if (!payload.enabled || body.action === 'clear') { cancelQuery(); clearResults(); } toast(body.action === 'clear' ? '图片索引已清理。' : body.action === 'rebuild' ? '已开始重建图片索引。' : '图片搜索设置已更新。'); }
      catch (failure) { if (generation === state.generation && failure.status !== 401) error(settings.error, failure.message || '操作失败，请重试。'); }
      finally { if (generation === state.generation) { state.busy = false; renderSearch(); renderSettings(); scheduleStatus(); } }
    }
    renderSearch(); renderSettings();
    return {
      showSearch() { if (state.active) return; state.active = true; loadStatus(true); },
      hideSearch() { if (!state.active) return; state.active = false; cancelQuery(); if (!state.managing) { stopStatus(); state.statusRequest?.abort(); state.statusRequest = null; } },
      showSettings() { if (!isAdmin() || state.managing) return; state.managing = true; loadStatus(true); },
      hideSettings() { if (!state.managing) return; state.managing = false; if (confirm.open) confirm.close(); if (!state.active) { stopStatus(); state.statusRequest?.abort(); state.statusRequest = null; } },
      syncCatalog(signature) { if (state.catalog && state.catalog !== signature) { cancelQuery(); clearResults(); } state.catalog = signature; },
      reset() { state.generation += 1; state.fileGeneration += 1; state.active = state.managing = state.busy = false; stopStatus(); state.statusRequest?.abort(); state.statusRequest = null; cancelQuery(); releasePreview(); Object.assign(state, { file: null, mime: '', status: null, catalog: '' }); clearResults(); error(search.error, ''); error(settings.error, ''); search.input.value = ''; if (confirm.open) confirm.close(); renderSearch(); renderSettings(); }
    };
  }
  return { maxBytes, fileError, imageType, statusMessage, normalizeResults, createRequestGate, create };
});

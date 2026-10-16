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
    if (admin && status.activity) {
      const activity = status.activity;
      if (activity.stage === 'stopped') return '图片索引任务已停止，请查看服务端日志。';
      if (activity.stage === 'stopping') return '服务正在停止图片索引任务…';
      if (activity.stage === 'error') return '图片索引暂时中断，正在重试，请查看服务端日志。';
      if (status.indexing) {
        if (activity.stage === 'reconciling') {
          const progress = indexProgress(status);
          return `正在核对图片：${progress.done} / ${progress.total} 张${status.available ? '，可先搜索已收录的图片。' : '，核对后开始建立索引。'}`;
        }
        if (activity.stage === 'waiting') return '正在等待后台任务名额…';
        if (activity.stage === 'reading') return '正在读取图片…';
        if (activity.stage === 'decoding') return '正在建立图片特征…';
        if (activity.stage === 'starting' || activity.stage === 'queued') return '图片索引任务等待处理…';
      }
    }
    if (status.indexing) return status.available ? '索引更新中，可先搜索已收录的图片。' : '正在建立图片索引，请稍后再试。';
    if (!status.available) return '暂无可搜索的图片。';
    return '搜索你可访问的写真库中的相同或近似图片。';
  }
  function indexProgress(status) {
    const checking = Boolean(status?.enabled && !status?.paused && status?.activity?.stage === 'reconciling');
    const source = checking ? status.activity : status?.progress;
    const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
    const total = count(source?.total); const done = Math.min(count(source?.done), total);
    return { done, total, percent: total ? done / total * 100 : 0, label: checking ? '图片核对进度' : '图片索引进度' };
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
  const failureKey = item => JSON.stringify([item.book_id, item.page_key]);
  function normalizeFailures(payload) {
    const seen = new Set();
    return (Array.isArray(payload?.items) ? payload.items : []).filter(item => {
      if (!item || typeof item.book_id !== 'string' || !item.book_id || typeof item.page_key !== 'string' || !item.page_key || item.page_key.length > 4096 || !Number.isSafeInteger(item.page) || item.page < 0 || typeof item.retryable !== 'boolean' || typeof item.reason !== 'string' || typeof item.filename !== 'string') return false;
      const key = failureKey(item); if (seen.has(key)) return false; seen.add(key); return true;
    });
  }
  function create({ searchHost, settingsHost, api, post, openBook, isAdmin, toast = () => {}, onManage = () => {}, onOpenFailure = () => {} }) {
    const document = searchHost.ownerDocument;
    const state = { active: false, managing: false, status: null, file: null, mime: '', url: '', results: [], searched: false, searching: false, busy: false, timer: null, statusRequest: null, generation: 0, fileGeneration: 0, catalog: '' };
    const queryGate = createRequestGate(); const search = {}; const settings = {};
    const failures = { expanded: false, page: 1, pageSize: 20, items: [], total: 0, selected: new Set(), request: null, generation: 0, loading: false, busy: false, loaded: false, observedCount: null };
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
    settings.countBefore = el('span'); settings.countAfter = el('span'); settings.failureToggle = button('失败 0 张', 'button button-secondary image-search-failure-toggle', toggleFailures); settings.failureToggle.setAttribute('aria-expanded', 'false'); settings.failureToggle.setAttribute('aria-controls', 'image-search-failure-panel'); settings.counts.append(settings.countBefore, settings.failureToggle, settings.countAfter);
    const tools = el('div', 'image-search-index-tools'); settings.pause = button('暂停', 'button button-secondary', () => mutate('/api/image-search/actions', { action: state.status?.paused ? 'resume' : 'pause' }));
    settings.retry = button('重试失败图片', 'button button-secondary', () => mutate('/api/image-search/actions', { action: 'retry' }));
    settings.rebuild = button('重建索引', 'button button-secondary', () => confirmAction('rebuild')); settings.clear = button('清理索引', 'button button-danger', () => confirmAction('clear')); settings.refresh = button('刷新', 'button button-secondary', () => loadStatus(true));
    tools.append(settings.pause, settings.retry, settings.rebuild, settings.clear, settings.refresh);
    settings.error = el('p', 'notice error'); settings.error.hidden = true; settings.error.setAttribute('role', 'alert');
    panel.append(enableRow, settings.message, settings.progress, settings.counts, tools, settings.error); settingsHost.append(panel);
    const failurePanel = el('section', 'settings-panel image-search-failures'); failurePanel.id = 'image-search-failure-panel'; failurePanel.hidden = true;
    const failureHeading = el('div', 'image-search-failure-heading'); failureHeading.append(el('h2', '', '索引失败明细'));
    settings.failureRefresh = button('刷新列表', 'button button-secondary', () => loadFailures(failures.page)); failureHeading.append(settings.failureRefresh);
    settings.failureStatus = el('p', 'image-search-status'); settings.failureStatus.setAttribute('role', 'status');
    const failureTools = el('div', 'image-search-failure-tools');
    const selectLabel = el('label', 'image-search-failure-select'); settings.failureAll = el('input'); settings.failureAll.type = 'checkbox'; settings.failureAll.setAttribute('aria-label', '选择本页可重试图片'); selectLabel.append(settings.failureAll, el('span', '', '选择本页'));
    settings.failureAll.addEventListener('change', () => { failures.selected.clear(); if (settings.failureAll.checked) failures.items.filter(item => item.retryable).forEach(item => failures.selected.add(failureKey(item))); renderFailureRows(); });
    settings.failureSelected = el('span', 'image-search-status'); settings.failureRetry = button('重试所选', 'button button-primary', () => retryFailures(failures.items.filter(item => failures.selected.has(failureKey(item)))));
    failureTools.append(selectLabel, settings.failureSelected, settings.failureRetry);
    settings.failureList = el('div', 'image-search-failure-list'); settings.failureList.setAttribute('aria-label', '索引失败图片');
    settings.failureError = el('p', 'notice error'); settings.failureError.hidden = true; settings.failureError.setAttribute('role', 'alert');
    const failurePages = el('div', 'image-search-failure-pages'); settings.failurePrevious = button('上一页', 'button button-secondary', () => loadFailures(failures.page - 1)); settings.failurePage = el('span', 'image-search-status'); settings.failureNext = button('下一页', 'button button-secondary', () => loadFailures(failures.page + 1)); failurePages.append(settings.failurePrevious, settings.failurePage, settings.failureNext);
    failurePanel.append(failureHeading, settings.failureStatus, failureTools, settings.failureList, settings.failureError, failurePages); settingsHost.append(failurePanel);
    function cancelFailures(clear = false) {
      failures.generation += 1; failures.request?.abort(); failures.request = null; failures.loading = failures.busy = false;
      if (clear) { failures.expanded = false; failures.page = 1; failures.items = []; failures.total = 0; failures.selected.clear(); failures.loaded = false; failures.observedCount = null; settings.failureList.replaceChildren(); error(settings.failureError, ''); }
      updateFailureControls();
    }
    async function toggleFailures() {
      if (!state.managing || !isAdmin() || state.busy || failures.busy || settings.failureToggle.disabled) return;
      failures.expanded = !failures.expanded;
      if (!failures.expanded) cancelFailures();
      updateFailureControls();
      if (failures.expanded && (!failures.loaded || (Number(state.status?.summary?.failed) !== failures.observedCount && !failures.selected.size))) await loadFailures(failures.page);
    }
    function updateFailureControls() {
      const busy = failures.loading || failures.busy || state.busy;
      const eligible = failures.items.filter(item => item.retryable);
      settings.failureToggle.textContent = `失败 ${Number(state.status?.summary?.failed) || 0} 张`;
      settings.failureToggle.disabled = failures.busy || state.busy || (!Number(state.status?.summary?.failed) && !failures.expanded);
      settings.failureToggle.setAttribute('aria-expanded', String(failures.expanded));
      settings.failureAll.checked = Boolean(eligible.length && eligible.every(item => failures.selected.has(failureKey(item))));
      settings.failureAll.indeterminate = Boolean(failures.selected.size && !settings.failureAll.checked);
      settings.failureAll.disabled = busy || !eligible.length || !state.status?.enabled;
      settings.failureSelected.textContent = `已选择 ${failures.selected.size} 张`;
      settings.failureRetry.disabled = busy || !failures.selected.size || !state.status?.enabled;
      if (failures.loaded && failures.total === Number(state.status?.summary?.failed) && failures.total <= failures.pageSize && !eligible.length) settings.retry.disabled = true;
      settings.failureRefresh.disabled = busy;
      settings.failurePrevious.disabled = busy || failures.page <= 1;
      settings.failureNext.disabled = busy || failures.page * failures.pageSize >= failures.total;
      settings.failurePage.textContent = `第 ${failures.page} / ${Math.max(1, Math.ceil(failures.total / failures.pageSize))} 页`;
      settings.failureStatus.textContent = failures.loading ? '正在读取失败图片…' : failures.total ? `共 ${failures.total} 张失败图片` : '暂无失败图片';
      failurePanel.hidden = !isAdmin() || !failures.expanded;
    }
    function renderFailureRows() {
      settings.failureList.replaceChildren();
      failures.items.forEach(item => {
        const row = el('article', 'image-search-failure-row');
        const check = el('input', 'image-search-failure-check'); check.type = 'checkbox'; check.checked = failures.selected.has(failureKey(item)); check.disabled = !item.retryable || failures.busy || state.busy || !state.status?.enabled; check.setAttribute('aria-label', `选择 ${item.filename}`);
        check.addEventListener('change', () => { if (check.checked) failures.selected.add(failureKey(item)); else failures.selected.delete(failureKey(item)); updateFailureControls(); });
        const info = el('div', 'image-search-failure-info'); const title = button(item.book_title || '未命名作品', 'image-search-failure-book', async () => { if (!state.managing || failures.busy) return; const generation = failures.generation; try { await onOpenFailure(item.book_id, item.page); } catch (failure) { if (generation === failures.generation && state.managing) error(settings.failureError, failure.message || '无法打开作品。'); } });
        info.append(title, el('span', '', [item.library_name, `第 ${item.page + 1} 页`, item.filename].filter(Boolean).join(' · ')), el('p', 'image-search-failure-reason', item.reason));
        const action = item.retryable ? button('重试', 'button button-secondary', () => retryFailures([item])) : el('span', 'image-search-failure-unsupported', '格式暂不支持');
        if (item.retryable) action.disabled = failures.busy || state.busy || !state.status?.enabled;
        row.append(check, info, action); settings.failureList.append(row);
      });
      updateFailureControls();
    }
    async function loadFailures(page = failures.page) {
      if (!state.managing || !failures.expanded || !isAdmin() || failures.busy || state.busy || !Number.isSafeInteger(page) || page < 1) return;
      const generation = ++failures.generation; failures.request?.abort(); const request = new AbortController(); failures.request = request; failures.loading = true; error(settings.failureError, ''); updateFailureControls();
      try {
        const payload = await api(`/api/image-search/failures?page=${page}&page_size=${failures.pageSize}`, { signal: request.signal });
        if (generation !== failures.generation || request.signal.aborted || !state.managing || !isAdmin()) return;
        const total = Number.isSafeInteger(payload?.total) && payload.total >= 0 ? payload.total : 0;
        const last = Math.max(1, Math.ceil(total / failures.pageSize));
        if (page > last) { failures.loading = false; failures.request = null; return loadFailures(last); }
        failures.page = page; failures.total = total; failures.items = normalizeFailures(payload); failures.selected.clear(); failures.loaded = true; failures.observedCount = Number(state.status?.summary?.failed) || 0; renderFailureRows();
      } catch (failure) { if (generation === failures.generation && !request.signal.aborted && failure.status !== 401) error(settings.failureError, failure.message || '无法读取失败图片。'); }
      finally { if (generation === failures.generation) { failures.loading = false; failures.request = null; updateFailureControls(); } }
    }
    async function retryFailures(items) {
      if (!state.managing || !failures.expanded || !isAdmin() || state.busy || failures.busy || !state.status?.enabled) return;
      const chosen = items.filter(item => item.retryable); if (!chosen.length) return;
      const generation = ++failures.generation; failures.request?.abort(); failures.request = null; failures.loading = false; failures.busy = true; stopStatus(); error(settings.failureError, ''); renderFailureRows(); renderSettings();
      try {
        const payload = await post('/api/image-search/retry', { items: chosen.map(({ book_id, page_key }) => ({ book_id, page_key })) });
        if (generation !== failures.generation || !state.managing || !isAdmin()) return;
        if (payload.snapshot) state.status = payload.snapshot;
        failures.selected.clear(); toast(`已重试 ${Number(payload.retried) || 0} 张图片${Number(payload.skipped) ? `，跳过 ${Number(payload.skipped)} 张` : ''}`);
        failures.busy = false; await loadFailures(failures.page);
      } catch (failure) { if (generation === failures.generation && failure.status !== 401) error(settings.failureError, failure.message || '图片重试失败。'); }
      finally { if (generation === failures.generation || !failures.busy) { failures.busy = false; renderSearch(); renderSettings(); scheduleStatus(); } }
    }
    const confirm = el('dialog', 'settings-dialog image-search-confirm'); confirm.setAttribute('aria-labelledby', 'image-search-confirm-title'); const confirmHeader = el('header', 'dialog-header'); settings.confirmTitle = el('h2'); settings.confirmTitle.id = 'image-search-confirm-title'; const close = button('关闭', 'button button-secondary', () => confirm.close()); confirmHeader.append(settings.confirmTitle, close); settings.confirmText = el('p', 'settings-intro'); const footer = el('footer', 'dialog-footer'); const back = button('取消', 'button button-secondary', () => confirm.close()); settings.confirmRun = button('确认', 'button button-danger', async () => { const action = confirm.dataset.action; confirm.close(); await mutate('/api/image-search/actions', { action }); }); footer.append(back, settings.confirmRun); confirm.append(confirmHeader, settings.confirmText, footer); document.body.append(confirm);
    function confirmAction(action) { if (state.busy) return; confirm.dataset.action = action; settings.confirmTitle.textContent = action === 'clear' ? '清理图片索引？' : '重新建立图片索引？'; settings.confirmText.textContent = action === 'clear' ? '清除图片搜索索引并关闭搜索。原始图片、优化图片和阅读记录保留。' : '清除现有搜索索引并重新生成。原始图片和阅读资料保留。'; settings.confirmRun.textContent = action === 'clear' ? '清理并关闭' : '开始重建'; confirm.showModal(); }
    function renderSettings() {
      const snapshot = state.status;
      const busy = state.busy || failures.busy;
      settings.enabled.checked = Boolean(snapshot?.enabled); settings.enabled.disabled = busy || !snapshot;
      settings.message.textContent = statusMessage(snapshot, true);
      const summary = snapshot?.summary || {}; const progress = indexProgress(snapshot);
      settings.progress.value = progress.percent; settings.progress.setAttribute('aria-label', progress.label); settings.progress.hidden = !snapshot?.indexing && !snapshot?.paused;
      settings.counts.hidden = !snapshot;
      settings.countBefore.textContent = snapshot ? `已索引 ${Number(summary.indexed) || 0} · 待处理 ${Number(summary.pending) || 0} · ` : '';
      settings.countAfter.textContent = snapshot ? ` · 共 ${Number(summary.total) || 0} 张` : '';
      settings.pause.textContent = snapshot?.paused ? '继续' : '暂停'; settings.pause.hidden = !snapshot?.indexing && !snapshot?.paused; settings.retry.hidden = !Number(summary.failed);
      settings.pause.disabled = settings.retry.disabled = busy || !snapshot?.enabled; settings.rebuild.disabled = busy || !snapshot?.enabled; settings.clear.disabled = busy || !snapshot || !(Number(summary.indexed) + Number(summary.pending) + Number(summary.failed)); settings.refresh.disabled = busy;
      updateFailureControls();
    }
    async function loadStatus(manual = false) {
      if (!(state.active || state.managing) || state.statusRequest || state.busy || failures.busy) { scheduleStatus(); return; }
      const generation = state.generation; const request = new AbortController(); state.statusRequest = request;
      try {
        const payload = await api('/api/image-search/status', { signal: request.signal });
        if (generation !== state.generation || request.signal.aborted) return;
        state.status = payload; error(settings.error, payload.error || ''); if (manual) error(search.error, ''); renderSearch(); renderSettings();
        const count = Number(payload.summary?.failed) || 0;
        if (state.managing && failures.expanded && !failures.loading && (count || failures.loaded) && (manual || !failures.loaded || (count !== failures.observedCount && !failures.selected.size && !failurePanel.contains(document.activeElement)))) loadFailures(failures.page);
        if (!payload.enabled) { cancelQuery(); clearResults(); }
      } catch (failure) { if (generation === state.generation && !request.signal.aborted && failure.status !== 401) { error(state.managing ? settings.error : search.error, failure.message || '无法读取图片搜索状态。'); } }
      finally { if (state.statusRequest === request) { state.statusRequest = null; scheduleStatus(); } }
    }
    async function mutate(path, body) {
      if (!isAdmin() || state.busy || failures.busy) return;
      cancelFailures(true);
      const generation = ++state.generation; state.statusRequest?.abort(); state.statusRequest = null; stopStatus(); state.busy = true; error(settings.error, ''); renderSettings();
      try { const payload = await post(path, body); if (generation !== state.generation) return; state.status = payload; if (!payload.enabled || body.action === 'clear') { cancelQuery(); clearResults(); } toast(body.action === 'clear' ? '图片索引已清理。' : body.action === 'rebuild' ? '已开始重建图片索引。' : '图片搜索设置已更新。'); }
      catch (failure) { if (generation === state.generation && failure.status !== 401) error(settings.error, failure.message || '操作失败，请重试。'); }
      finally { if (generation === state.generation) { state.busy = false; renderSearch(); renderSettings(); if (state.managing && Number(state.status?.summary?.failed)) loadFailures(1); scheduleStatus(); } }
    }
    renderSearch(); renderSettings();
    return {
      showSearch() { if (state.active) return; state.active = true; loadStatus(true); },
      hideSearch() { if (!state.active) return; state.active = false; cancelQuery(); if (!state.managing) { stopStatus(); state.statusRequest?.abort(); state.statusRequest = null; } },
      showSettings() { if (!isAdmin() || state.managing) return; state.managing = true; loadStatus(true); },
      hideSettings() { if (!state.managing) return; state.managing = false; cancelFailures(true); if (confirm.open) confirm.close(); if (!state.active) { stopStatus(); state.statusRequest?.abort(); state.statusRequest = null; } },
      syncCatalog(signature) { if (state.catalog && state.catalog !== signature) { cancelQuery(); clearResults(); } state.catalog = signature; },
      reset() { state.generation += 1; state.fileGeneration += 1; state.active = state.managing = state.busy = false; cancelFailures(true); stopStatus(); state.statusRequest?.abort(); state.statusRequest = null; cancelQuery(); releasePreview(); Object.assign(state, { file: null, mime: '', status: null, catalog: '' }); clearResults(); error(search.error, ''); error(settings.error, ''); search.input.value = ''; if (confirm.open) confirm.close(); renderSearch(); renderSettings(); }
    };
  }
  return { maxBytes, fileError, imageType, statusMessage, indexProgress, normalizeResults, normalizeFailures, failureKey, createRequestGate, create };
});

/* Administrator image optimization. Records survive completion; only confirmed jobs run. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleTranscodingUI = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const statusLabels = Object.freeze({ pending: '待选择', original: '使用原图', queued: '等待优化', running: '优化中', paused: '已暂停', failed: '失败', cancelled: '已取消', ready: '已完成' });
  const actionLabels = Object.freeze({ original: '使用原图', transcode: '开始优化', ignore: '忽略', pause: '暂停', resume: '继续', cancel: '取消任务', retry: '重试', restore: '取消忽略' });
  const defaultRules = Object.freeze({ enabled: false, file_size_mb: 10, max_source_pixels: 40000000, max_source_side: 10000, output_format: 'webp', quality: 88, max_output_side: 3840, workers: 1, memory_limit_mb: 1024, timeout_seconds: 120, storage_limit_mb: 10240, min_free_space_mb: 512 });
  function refreshDelay(snapshot) {
    return !snapshot || Number(snapshot.summary?.processing_books) > 0 || snapshot.books?.some(book => ['queued', 'running'].includes(book.status)) ? 5000 : 15000;
  }
  function normalizeFilters(value) {
    const input = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    return {
      query: typeof input.query === 'string' ? input.query.slice(0, 200) : '',
      status: typeof input.status === 'string' && Object.hasOwn(statusLabels, input.status) ? input.status : '',
      library: typeof input.library === 'string' && input.library.length <= 200 ? input.library : ''
    };
  }
  const numericFields = [
    ['file_size_mb', '文件大小超过', 'MiB', 0, 1024, 0.1, 1],
    ['max_source_pixels', '总像素超过', '万像素', 0, 100000, 1, 10000],
    ['max_source_side', '最长边超过', 'px', 0, 100000, 1, 1],
    ['quality', '压缩质量', '', 1, 95, 1, 1],
    ['max_output_side', '输出最长边', 'px', 512, 8192, 1, 1],
    ['memory_limit_mb', '单任务内存上限', 'MiB', 256, 8192, 1, 1],
    ['timeout_seconds', '单张处理超时', '秒', 10, 600, 1, 1],
    ['storage_limit_mb', '优化图片容量上限', 'MiB', 0, 1048576, 1, 1],
    ['min_free_space_mb', '保留磁盘空间', 'MiB', 0, 1048576, 1, 1]
  ];
  function bytes(value) {
    const number = Math.max(0, Number(value) || 0);
    if (number < 1024) return number + ' B';
    const exponent = Math.min(4, Math.floor(Math.log(number) / Math.log(1024)));
    return (number / (1024 ** exponent)).toLocaleString('zh-CN', { maximumFractionDigits: 1 }) + ' ' + ['B', 'KiB', 'MiB', 'GiB', 'TiB'][exponent];
  }
  function progress(book) {
    const total = Math.max(0, Number(book.matched_count) || 0);
    const success = Math.max(0, Number(book.success_count) || 0);
    return total ? Math.min(100, Math.round(success / total * 100)) : 0;
  }
  function formatParameters(value) {
    if (!value || typeof value !== 'object' || !value.output_format || !Number.isFinite(Number(value.quality)) || !Number.isFinite(Number(value.max_output_side))) return '未记录';
    return `${String(value.output_format).toUpperCase()} · 质量 ${value.quality} · 最长边 ${value.max_output_side}px`;
  }
  function formatTime(value) {
    const time = Number(value); if (!(time > 0)) return '未记录';
    const date = new Date(time * 1000); return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '未记录';
  }
  function eligible(book, action) {
    if (book.status === 'ignored') return action === 'restore';
    if (action === 'pause') return ['queued', 'running'].includes(book.status);
    if (action === 'resume') return book.status === 'paused';
    if (action === 'cancel') return ['queued', 'running', 'paused'].includes(book.status);
    if (action === 'retry') return ['failed', 'cancelled'].includes(book.status);
    if (action === 'transcode') return Number(book.matched_count) > 0 && !['queued', 'running'].includes(book.status);
    return action === 'original' || action === 'ignore';
  }
  function bookDisplaySignature(book) {
    return JSON.stringify([book.id, book.title, book.library_id, book.library_name, book.status, book.matched_count, book.success_count, book.failed_count, book.pending_count, book.running_count, book.output_count, book.actual_bytes, book.output_path, book.error, book.needs_retranscode, book.published, book.reasons, book.page_count]);
  }
  function create({ host, api, post, toast, mountFilter, onCatalogChange = () => {}, onTabChange = () => {}, onFiltersChange = () => {}, onRender = () => {} }) {
    const document = host.ownerDocument;
    const state = { active: false, tab: 'tasks', snapshot: null, loading: false, manualLoading: false, busy: false, selected: new Set(), filter: '', library: '', query: '', dirty: false, draft: null, generation: 0, timer: null, request: null, error: '', signature: '', published: '', rows: new Map(), librarySignature: '' };
    const controls = {};
    function el(tag, className, text) { const item = document.createElement(tag); if (className) item.className = className; if (text !== undefined) item.textContent = String(text); return item; }
    function button(text, className = 'button button-secondary', action) { const item = el('button', className, text); item.type = 'button'; if (action) item.addEventListener('click', action); return item; }
    function option(value, text) { const item = el('option', '', text); item.value = value; return item; }
    function label(text, control) { const item = el('label', 'transcode-filter'); item.append(el('span', 'sr-only', text), control); return item; }
    function setError(message) { state.error = message || ''; controls.error.textContent = state.error; controls.error.hidden = !state.error; }
    function addHint(parent, text) { parent.append(el('p', 'transcode-hint', text)); }
    host.classList.add('transcoding-page');
    const tabs = el('div', 'transcode-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', '图片优化');
    controls.tasksTab = button('作品与任务', '', () => switchTab('tasks'));
    controls.ignoredTab = button('忽略列表', '', () => switchTab('ignored'));
    controls.ignoredTab.hidden = true;
    controls.rulesTab = button('优化规则', '', () => switchTab('rules'));
    const tabButtons = { tasks: controls.tasksTab, ignored: controls.ignoredTab, rules: controls.rulesTab };
    Object.entries(tabButtons).forEach(([tab, item]) => {
      item.id = 'transcode-tab-' + tab;
      item.setAttribute('role', 'tab');
      item.setAttribute('aria-controls', 'transcode-panel-' + (tab === 'rules' ? 'rules' : 'tasks'));
    });
    tabs.append(...Object.values(tabButtons));
    tabs.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const tabNames = Object.keys(tabButtons).filter(tab => !tabButtons[tab].hidden);
      const index = tabNames.indexOf(state.tab);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabNames.length - 1 :
        (index + (event.key === 'ArrowRight' ? 1 : -1) + tabNames.length) % tabNames.length;
      switchTab(tabNames[next]);
      tabButtons[state.tab].focus();
    });
    controls.error = el('p', 'notice error'); controls.error.setAttribute('role', 'alert'); controls.error.hidden = true;
    const retryLoad = button('重新加载', 'button button-secondary', () => load(true));
    controls.disabledNotice = el('div', 'transcode-disabled'); controls.disabledNotice.append(el('span', '', '优化规则尚未启用。'), button('配置规则', 'button button-secondary', () => switchTab('rules')));
    const ignoreUnavailable = el('p', 'notice', '当前 Gravelle 服务尚未加载「忽略」功能。请重启服务并刷新页面。');
    ignoreUnavailable.hidden = true;
    controls.summary = el('div', 'transcode-summary'); controls.summary.setAttribute('aria-live', 'polite');
    const storageTools = el('div', 'transcode-storage-tools'); controls.cleanupSummary = el('span', 'transcode-hint');
    controls.cleanup = button('清理旧副本', 'button button-secondary', confirmCleanup); storageTools.append(controls.cleanupSummary, controls.cleanup);
    const tasks = el('section', 'transcode-tasks'); tasks.id = 'transcode-panel-tasks'; tasks.setAttribute('role', 'tabpanel'); tasks.setAttribute('aria-labelledby', 'transcode-tab-tasks');
    const filterBar = el('div', 'transcode-filter-bar');
    controls.query = el('input'); controls.query.type = 'search'; controls.query.placeholder = '搜索作品'; controls.query.maxLength = 200;
    const statusHost = el('div', 'transcode-filter transcode-select-host');
    const libraryHost = el('div', 'transcode-filter transcode-select-host');
    const statusOptions = [['', '全部状态'], ...Object.entries(statusLabels)];
    const allLibraries = [['', '全部媒体库']];
    let libraryOptions = allLibraries;
    const statusFilter = mountFilter(statusHost, '筛选状态', value => {
      state.filter = value;
      renderBooks();
      onFiltersChange(getFilters());
    });
    const libraryFilter = mountFilter(libraryHost, '筛选媒体库', value => {
      state.library = value;
      renderBooks();
      onFiltersChange(getFilters());
    });
    controls.refresh = button('刷新', 'button button-secondary', () => load(true));
    filterBar.append(label('搜索作品', controls.query), statusHost, libraryHost, controls.refresh);
    controls.query.addEventListener('input', () => { state.query = controls.query.value.slice(0, 200); renderBooks(); onFiltersChange(getFilters()); });
    const toolbar = el('div', 'transcode-selection-tools');
    const selectLabel = el('label', 'transcode-select-all'); controls.all = el('input'); controls.all.type = 'checkbox'; controls.all.setAttribute('aria-label', '选择当前筛选的全部作品'); selectLabel.append(controls.all, el('span', '', '全选')); controls.count = el('span', 'transcode-selected-count', '未选择作品');
    controls.all.addEventListener('change', () => { filtered().forEach(book => controls.all.checked ? state.selected.add(String(book.id)) : state.selected.delete(String(book.id))); renderBooks(); });
    const clear = button('清空选择', 'button button-secondary', () => { state.selected.clear(); renderBooks(); });
    const actions = el('div', 'transcode-actions'); controls.actions = {};
    Object.entries(actionLabels).forEach(([action, text]) => { const item = button(text, 'button ' + (action === 'transcode' ? 'button-primary' : 'button-secondary'), () => confirmAction(action)); item.dataset.action = action; controls.actions[action] = item; actions.append(item); });
    toolbar.append(selectLabel, controls.count, clear, actions);
    controls.list = el('div', 'transcode-book-list'); controls.list.setAttribute('aria-label', '优化作品');
    controls.empty = el('p', 'transcode-empty', '暂无命中规则的作品。'); controls.empty.setAttribute('role', 'status');
    tasks.append(controls.summary, storageTools, filterBar, toolbar, controls.list, controls.empty);
    const rules = el('section', 'settings-panel transcode-rules'); rules.id = 'transcode-panel-rules'; rules.setAttribute('role', 'tabpanel'); rules.setAttribute('aria-labelledby', 'transcode-tab-rules');
    const form = el('form'); controls.form = form;
    const enabled = el('label', 'transcode-enabled'); const enabledText = el('span'); enabledText.append(el('strong', '', '启用图片优化规则'), el('small', '', '命中规则的新作品等待管理员选择。')); controls.enabled = el('input'); controls.enabled.type = 'checkbox'; controls.enabled.id = 'transcode-enabled'; const toggle = el('span', 'visibility-switch'); toggle.setAttribute('aria-hidden', 'true'); enabled.append(enabledText, controls.enabled, toggle); form.append(enabled);
    function group(title, fieldKeys, hint) {
      const block = el('fieldset', 'transcode-rule-group'); block.append(el('legend', '', title));
      if (hint) addHint(block, hint);
      const fields = el('div', 'transcode-rule-fields');
      fieldKeys.forEach(key => {
        if (key === 'output_format') { const wrap = el('label', 'transcode-rule-field'); wrap.append(el('span', '', '输出格式')); const select = el('select'); select.append(option('webp', 'WebP'), option('jpeg', 'JPEG')); select.id = 'transcode-output-format'; controls[key] = select; wrap.append(select); fields.append(wrap); return; }
        const [, text, unit, min, max, step] = numericFields.find(item => item[0] === key); const wrap = el('label', 'transcode-rule-field'); wrap.append(el('span', '', text)); const inputWrap = el('span', 'transcode-number'); const input = el('input'); input.id = 'transcode-' + key.replaceAll('_', '-'); input.type = 'number'; input.min = min; input.max = max; input.step = step; input.required = true; input.inputMode = step < 1 ? 'decimal' : 'numeric'; controls[key] = input; inputWrap.append(input); if (unit) inputWrap.append(el('small', '', unit)); wrap.append(inputWrap); fields.append(wrap);
      });
      block.append(fields); form.append(block);
    }
    group('触发条件', ['file_size_mb', 'max_source_pixels', 'max_source_side'], '任一条件命中即可；输入 0 关闭该条件。');
    group('优化图片', ['output_format', 'quality', 'max_output_side'], '仅处理命中规则的图片，原始文件保持不变。');
    group('后台与存储', ['memory_limit_mb', 'timeout_seconds', 'storage_limit_mb', 'min_free_space_mb'], '每次处理 1 张图片。容量上限为 0 时不限；达到上限或空间不足时暂停任务。');
    const saveBar = el('div', 'transcode-rule-footer'); controls.draftStatus = el('span', 'transcode-hint'); controls.reset = button('放弃修改', 'button button-secondary', () => { state.dirty = false; state.draft = null; fillRules(state.snapshot?.rules || defaultRules); }); controls.preview = el('button', 'button button-primary', '预览并保存规则'); controls.preview.type = 'submit'; saveBar.append(controls.draftStatus, controls.reset, controls.preview); form.append(saveBar); rules.append(form);
    form.addEventListener('input', () => { state.dirty = true; state.draft = null; updateBusy(); });
    form.addEventListener('change', () => { state.dirty = true; state.draft = null; updateBusy(); });
    form.addEventListener('submit', event => { event.preventDefault(); previewRules(); });
    host.append(tabs, controls.error, controls.disabledNotice, ignoreUnavailable, tasks, rules);
    controls.error.after(retryLoad); controls.retryLoad = retryLoad; retryLoad.hidden = true;

    const dialog = el('dialog', 'settings-dialog library-dialog transcode-confirm-dialog'); dialog.setAttribute('aria-labelledby', 'transcode-confirm-title');
    const header = el('header', 'dialog-header'); const title = el('h2'); title.id = 'transcode-confirm-title'; const close = button('×', 'icon-button', () => { if (!state.busy) dialog.close(); }); close.setAttribute('aria-label', '关闭'); header.append(title, close);
    const body = el('div', 'dialog-body'); const footer = el('footer', 'dialog-footer'); const cancel = button('取消', 'button button-secondary', () => dialog.close()); const confirm = button('确认', 'button button-primary'); const dialogError = el('p', 'form-error'); dialogError.setAttribute('role', 'alert'); dialogError.hidden = true; footer.append(cancel, confirm); dialog.append(header, body, dialogError, footer); document.body.append(dialog);
    dialog.addEventListener('cancel', event => { if (state.busy) event.preventDefault(); });
    const previewDialog = el('dialog', 'settings-dialog library-dialog transcode-image-dialog'); previewDialog.setAttribute('aria-labelledby', 'transcode-image-title');
    const imageHeader = el('header', 'dialog-header'); const imageTitle = el('h2'); imageTitle.id = 'transcode-image-title'; const imageClose = button('×', 'icon-button', () => previewDialog.close()); imageClose.setAttribute('aria-label', '关闭作品预览'); imageHeader.append(imageTitle, imageClose);
    const imageBody = el('div', 'dialog-body transcode-image-body'); const previewImage = el('img'); previewImage.alt = '作品原图预览'; previewImage.decoding = 'async'; const imageStatus = el('p', 'transcode-hint'); imageStatus.setAttribute('role', 'status'); imageBody.append(previewImage, imageStatus);
    const imageFooter = el('footer', 'dialog-footer transcode-image-footer'); const previousImage = button('上一页', 'button button-secondary', () => showPreviewPage(previewIndex - 1)); const nextImage = button('下一页', 'button button-secondary', () => showPreviewPage(previewIndex + 1)); const imagePage = el('label', 'transcode-image-page'); const imageInput = el('input'); imageInput.type = 'number'; imageInput.min = 1; imageInput.setAttribute('aria-label', '预览页码'); const imageTotal = el('span'); imagePage.append(imageInput, imageTotal); imageInput.addEventListener('change', () => showPreviewPage(Number(imageInput.value) - 1)); imageFooter.append(previousImage, imagePage, nextImage); previewDialog.append(imageHeader, imageBody, imageFooter); document.body.append(previewDialog);
    let previewBook = null; let previewIndex = 0;
    previewImage.addEventListener('load', () => { imageStatus.textContent = ''; }); previewImage.addEventListener('error', () => { if (previewBook) imageStatus.textContent = '图片无法加载，请尝试其他页码或稍后重试。'; });
    previewDialog.addEventListener('close', () => { previewBook = null; previewImage.removeAttribute('src'); });
    function showPreviewPage(value) {
      if (!previewBook) return; const count = Math.max(0, Number(previewBook.page_count) || 0); previewIndex = Math.min(count ? count - 1 : 999999, Math.max(0, Math.trunc(Number(value)) || 0));
      imageInput.value = previewIndex + 1; if (count) imageInput.max = count; else imageInput.removeAttribute('max'); imageTotal.textContent = count ? `/ ${count}` : '';
      previousImage.disabled = previewIndex === 0; nextImage.disabled = Boolean(count && previewIndex >= count - 1); imageStatus.textContent = '正在加载原图…'; previewImage.src = '/api/transcoding/books/' + encodeURIComponent(previewBook.id) + '/pages/' + previewIndex;
    }
    function openPreview(book) { previewBook = book; imageTitle.textContent = book.title || '作品预览'; showPreviewPage(0); previewDialog.showModal(); }
    const detailDialog = el('dialog', 'settings-dialog library-dialog transcode-details-dialog'); detailDialog.setAttribute('aria-labelledby', 'transcode-details-title');
    const detailHeader = el('header', 'dialog-header'); const detailTitle = el('h2', '', '优化详情'); detailTitle.id = 'transcode-details-title'; const detailClose = button('×', 'icon-button', () => detailDialog.close()); detailClose.setAttribute('aria-label', '关闭优化详情'); detailHeader.append(detailTitle, detailClose);
    const detailBody = el('div', 'dialog-body'); const detailTabs = el('div', 'transcode-tabs'); detailTabs.setAttribute('role', 'tablist'); detailTabs.setAttribute('aria-label', '优化详情');
    const basicTab = button('基本信息', '', () => setDetailTab('basic')); const pagesTab = button('图片明细', '', () => setDetailTab('pages')); [basicTab, pagesTab].forEach((item, index) => { item.setAttribute('role', 'tab'); item.id = 'transcode-details-tab-' + (index ? 'pages' : 'basic'); item.setAttribute('aria-controls', 'transcode-details-content'); }); detailTabs.append(basicTab, pagesTab);
    const detailError = el('p', 'form-error'); detailError.setAttribute('role', 'alert'); detailError.hidden = true; const detailContent = el('div', 'transcode-detail-content'); detailContent.id = 'transcode-details-content'; detailContent.setAttribute('role', 'tabpanel'); detailBody.append(detailTabs, detailError, detailContent);
    detailTabs.addEventListener('keydown', event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); setDetailTab(event.key === 'Home' || event.key === 'ArrowLeft' && detailTab === 'pages' || event.key === 'ArrowRight' && detailTab === 'pages' ? 'basic' : 'pages'); (detailTab === 'pages' ? pagesTab : basicTab).focus(); });
    const detailFooter = el('footer', 'dialog-footer transcode-detail-footer'); const previousDetails = button('上一页', 'button button-secondary', () => loadDetails((detailData?.pagination?.page || 1) - 1)); const detailsPage = el('span', 'transcode-hint'); detailsPage.setAttribute('aria-live', 'polite'); const nextDetails = button('下一页', 'button button-secondary', () => loadDetails((detailData?.pagination?.page || 1) + 1)); const closeDetails = button('关闭', 'button button-secondary', () => detailDialog.close()); detailFooter.append(previousDetails, detailsPage, nextDetails, closeDetails); detailDialog.append(detailHeader, detailBody, detailFooter); document.body.append(detailDialog);
    let detailBook = null; let detailData = null; let detailTab = 'basic'; let detailRequest = 0; let detailLoading = false;
    detailDialog.addEventListener('close', () => { detailRequest += 1; detailBook = detailData = null; detailLoading = false; detailContent.replaceChildren(); });
    async function copyPath(value) {
      if (!value) return;
      try {
        const clipboard = document.defaultView?.navigator?.clipboard;
        if (clipboard?.writeText) { try { await clipboard.writeText(value); toast('路径已复制'); return; } catch (_) {} }
        const previousFocus = document.activeElement; const input = el('textarea', 'sr-only'); input.value = value; document.body.append(input); input.select(); let copied = false;
        try { copied = document.execCommand?.('copy') === true; } finally { input.remove(); previousFocus?.focus?.({ preventScroll: true }); }
        toast(copied ? '路径已复制' : '请选中路径后复制');
      } catch (_) { toast('请选中路径后复制'); }
    }
    function pathRow(label, value) {
      const row = el('div', 'transcode-detail-path'); row.append(el('span', '', label), el('code', '', value || '未生成'));
      if (value) row.append(button('复制', 'button button-secondary button-small', () => copyPath(value)));
      return row;
    }
    function detailFact(label, value) { const row = el('div', 'transcode-detail-fact'); row.append(el('dt', '', label), el('dd', '', value)); return row; }
    function renderDetails() {
      basicTab.setAttribute('aria-selected', String(detailTab === 'basic')); pagesTab.setAttribute('aria-selected', String(detailTab === 'pages'));
      basicTab.tabIndex = detailTab === 'basic' ? 0 : -1; pagesTab.tabIndex = detailTab === 'pages' ? 0 : -1; detailContent.setAttribute('aria-labelledby', detailTab === 'basic' ? basicTab.id : pagesTab.id);
      const paging = detailData?.pagination; previousDetails.hidden = nextDetails.hidden = detailsPage.hidden = detailTab !== 'pages'; previousDetails.disabled = detailLoading || !paging || paging.page <= 1; nextDetails.disabled = detailLoading || !paging || paging.page >= paging.pages;
      detailsPage.textContent = paging ? `${paging.page} / ${paging.pages} · 共 ${paging.total} 张` : '';
      if (!detailData) { detailContent.replaceChildren(el('p', 'transcode-hint', detailLoading ? '正在读取优化详情…' : '暂时无法读取详情。')); return; }
      const book = detailData.book || {};
      if (detailTab === 'basic') {
        const facts = el('dl', 'transcode-detail-facts'); facts.append(detailFact('作品', book.title || '未命名作品'), detailFact('写真库', book.library_name || '写真库'), detailFact('状态', statusLabels[book.status] || '未记录'), detailFact('命中图片', `${book.matched_count || 0} 张`), detailFact('未处理', `${book.pending_count || 0} 张`), detailFact('处理中', `${book.running_count || 0} 张`), detailFact('成功 / 失败', `${book.success_count || 0} / ${book.failed_count || 0} 张`), detailFact('在用优化图', `${book.output_count || 0} 张 · ${bytes(book.actual_bytes)}`), detailFact('最近整批完成', formatTime(book.completed_at)), detailFact('当前规则版本', `#${detailData.current_rule_revision}`), detailFact('本次目标规则', `#${detailData.target_rule_revision}`), detailFact('目标输出参数', formatParameters(detailData.target_parameters)));
        const profiles = el('section', 'transcode-detail-profiles'); profiles.append(el('h3', '', '当前使用的版本'));
        const active = Array.isArray(detailData.active_profiles) ? detailData.active_profiles : [];
        if (!active.length) profiles.append(el('p', 'transcode-hint', '当前使用原图，尚无在用优化版本。'));
        active.forEach(item => { const entry = el('article', 'transcode-detail-profile'); entry.append(el('p', '', formatParameters(item.parameters)), el('p', 'transcode-hint', `规则版本 ${item.parameters?.rule_revision ? '#' + item.parameters.rule_revision : '未记录'} · ${item.count || 0} 张 · ${bytes(item.bytes)}`), el('code', '', item.profile || '未记录')); profiles.append(entry); });
        detailContent.replaceChildren(facts, profiles);
      } else {
        const rows = (detailData.pages || []).map(item => {
          const row = el('article', 'transcode-detail-page'); const heading = el('div', 'transcode-book-heading'); heading.append(el('h3', '', `第 ${Number(item.page_index) + 1} 页`), el('span', 'transcode-status', item.status === 'unmatched' ? '未命中规则' : ({ pending: '未处理', running: '处理中', success: '成功', failed: '失败' }[item.status] || '未处理')));
          row.append(heading, el('p', 'transcode-hint', `原图 ${bytes(item.source_bytes)} · ${item.width || 0} × ${item.height || 0}`));
          if (item.reasons?.length) row.append(el('p', 'transcode-hint', item.reasons.join(' · ')));
          if (item.active_path) row.append(el('p', '', `在用 ${bytes(item.active_bytes)} · ${formatParameters(item.active_parameters)}`));
          if (item.desired_path && item.desired_path !== item.active_path) row.append(el('p', '', `本次输出 ${bytes(item.desired_bytes)} · ${formatParameters(item.desired_parameters)}`));
          row.append(pathRow('原图', item.original_path)); if (item.archive_entry) row.append(pathRow('压缩包内页', item.archive_entry));
          if (item.active_path) row.append(pathRow('在用优化图', item.active_path));
          if (item.desired_path && item.desired_path !== item.active_path) row.append(pathRow('本次优化图', item.desired_path));
          if (item.error) row.append(el('p', 'form-error', item.error)); return row;
        });
        detailContent.replaceChildren(...rows); if (!rows.length) detailContent.append(el('p', 'transcode-hint', '没有图片记录。'));
      }
    }
    function setDetailTab(tab) { detailTab = tab === 'pages' ? 'pages' : 'basic'; renderDetails(); detailBody.scrollTop = 0; }
    async function loadDetails(page = 1) {
      if (!detailBook || detailLoading) return; const request = ++detailRequest; const generation = state.generation; detailLoading = true; detailError.hidden = true; renderDetails();
      try { const data = await api('/api/transcoding/books/' + encodeURIComponent(detailBook.id) + '/details?page=' + Math.max(1, page) + '&page_size=40'); if (request !== detailRequest || generation !== state.generation || !detailDialog.open) return; if (!data?.book || !Array.isArray(data.pages) || !data.pagination) throw new Error('优化详情格式不正确。'); detailData = data; detailBody.scrollTop = 0; }
      catch (error) { if (request === detailRequest && generation === state.generation && error.status !== 401) { detailError.textContent = error.message; detailError.hidden = false; } }
      finally { if (request === detailRequest && generation === state.generation) { detailLoading = false; renderDetails(); } }
    }
    function openDetails(book) { detailRequest += 1; detailBook = book; detailData = null; detailTab = 'basic'; detailLoading = false; detailTitle.textContent = (book.title || '作品') + ' · 优化详情'; detailDialog.showModal(); loadDetails(); }
    let confirmOperation = null;
    confirm.addEventListener('click', () => { if (confirmOperation && !state.busy) confirmOperation(); });
    function openConfirm(heading, content, confirmText, operation) { title.textContent = heading; body.replaceChildren(...content); confirm.textContent = confirmText; dialogError.hidden = true; dialogError.textContent = ''; confirmOperation = operation; dialog.showModal(); }
    function updateBusy() {
      // Automatic polling never changes filter widths or temporarily disables filters.
      controls.refresh.disabled = state.manualLoading || state.busy; controls.refresh.setAttribute('aria-busy', String(state.manualLoading));
      controls.preview.disabled = !state.snapshot || state.busy || !state.dirty; controls.reset.disabled = state.busy || !state.dirty;
      controls.draftStatus.textContent = state.dirty ? '有未保存的修改' : '';
      Array.from(form.elements).forEach(item => { if (item !== controls.preview && item !== controls.reset) item.disabled = state.busy || !state.snapshot; });
      close.disabled = cancel.disabled = confirm.disabled = state.busy;
      controls.all.disabled = state.busy || !filtered().length;
      controls.cleanup.disabled = state.busy || !state.snapshot || !(Number(state.snapshot.summary?.reclaimable_files) > 0);
      renderSelection();
    }
    function fillRules(values) {
      const rules = { ...defaultRules, ...values }; controls.enabled.checked = rules.enabled === true; controls.output_format.value = rules.output_format;
      numericFields.forEach(([key, , , , , , factor]) => { controls[key].value = Number(rules[key]) / factor; }); updateBusy();
    }
    function readRules() {
      const rules = { enabled: controls.enabled.checked, output_format: controls.output_format.value, workers: 1 };
      numericFields.forEach(([key, , , , , , factor]) => { rules[key] = Number(controls[key].value) * factor; });
      if (rules.enabled && ![rules.file_size_mb, rules.max_source_pixels, rules.max_source_side].some(value => value > 0)) { controls.file_size_mb.setCustomValidity('启用时至少设置一个触发条件。'); controls.file_size_mb.reportValidity(); controls.file_size_mb.setCustomValidity(''); return null; }
      return rules;
    }
    function currentBooks() {
      return (state.tab === 'ignored' ? state.snapshot?.ignored_books : state.snapshot?.books) || [];
    }
    function filtered() {
      const query = state.query.trim().toLocaleLowerCase();
      return currentBooks().filter(book => (state.tab === 'ignored' || !state.filter || book.status === state.filter) && (!state.library || String(book.library_id) === state.library) && (!query || [book.title, book.library_name].some(value => String(value || '').toLocaleLowerCase().includes(query))));
    }
    function renderSelection() {
      const selected = currentBooks().filter(book => state.selected.has(String(book.id))); const visible = filtered();
      controls.count.textContent = selected.length ? `已选择 ${selected.length} 本作品` : '未选择作品';
      const selectedVisible = visible.filter(book => state.selected.has(String(book.id))).length;
      controls.all.checked = visible.length > 0 && visible.length === selectedVisible; controls.all.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
      const canIgnore = Array.isArray(state.snapshot?.ignored_books);
      Object.entries(controls.actions).forEach(([action, item]) => {
        const count = selected.filter(book => eligible(book, action)).length;
        item.hidden = (action === 'restore') !== (state.tab === 'ignored') || action === 'ignore' && !canIgnore;
        item.disabled = state.busy || !count || action !== 'restore' && state.snapshot?.rules?.enabled === false;
        item.title = count && count < selected.length ? `适用于所选的 ${count} 本作品` : '';
      });
    }
    function renderSummary() {
      const summary = state.snapshot?.summary || {};
      controls.summary.replaceChildren(...[['total_books', '命中作品'], ['pending_books', '待选择'], ['processing_books', '优化中'], ['completed_books', '已完成'], ['actual_bytes', '优化图占用']].map(([key, text]) => { const item = el('div', 'transcode-stat'); item.append(el('strong', '', key === 'actual_bytes' ? bytes(summary[key]) : String(summary[key] || 0)), el('span', '', text)); return item; }));
      controls.disabledNotice.hidden = state.snapshot?.rules?.enabled === true;
      const count = Math.max(0, Number(summary.reclaimable_files) || 0);
      controls.cleanupSummary.textContent = `已存储优化图 ${bytes(summary.stored_bytes)} · 可清理旧副本 ${count} 个，${bytes(summary.reclaimable_bytes)}`;
    }
    function renderLibraries() {
      const libraries = new Map(currentBooks().map(book => [String(book.library_id), book.library_name || '媒体库']));
      if (state.snapshot && state.library && !libraries.has(state.library)) state.library = '';
      const signature = JSON.stringify(Array.from(libraries).sort((a, b) => a[0].localeCompare(b[0])));
      if (signature === state.librarySignature) return;
      state.librarySignature = signature;
      libraryOptions = [...allLibraries, ...libraries];
    }
    function syncFilters() {
      if (state.active && state.tab !== 'rules') {
        if (state.tab === 'ignored') statusFilter.hide();
        else statusFilter.update(statusOptions, state.filter);
        libraryFilter.update(libraryOptions, state.library);
      } else {
        statusFilter.hide();
        libraryFilter.hide();
      }
    }
    function renderBooks() {
      syncFilters();
      const focus = document.activeElement?.dataset.transcodeId; const open = new Set(Array.from(controls.list.querySelectorAll('details[open]')).map(item => item.dataset.bookId));
      const books = filtered();
      const rows = books.map(book => {
        const id = String(book.id); const signature = bookDisplaySignature(book); const cached = state.rows.get(id);
        if (cached?.signature === signature) { cached.element.classList.toggle('is-selected', state.selected.has(id)); const input = cached.element.querySelector('input'); input.checked = state.selected.has(id); input.disabled = state.busy; return cached.element; }
        const row = el('article', 'transcode-book'); row.classList.toggle('is-selected', state.selected.has(id));
        const select = el('label', 'transcode-book-select'); const input = el('input'); input.type = 'checkbox'; input.checked = state.selected.has(id); input.disabled = state.busy; input.dataset.transcodeId = id; input.setAttribute('aria-label', `选择《${book.title || '作品'}》`); input.addEventListener('change', () => { input.checked ? state.selected.add(id) : state.selected.delete(id); row.classList.toggle('is-selected', input.checked); renderSelection(); }); select.append(input);
        const main = el('div', 'transcode-book-main'); const heading = el('div', 'transcode-book-heading'); heading.append(el('h3', '', book.title || '未命名作品'), el('span', 'transcode-status transcode-status-' + String(book.status || 'pending').replace(/[^a-z]/g, ''), book.status === 'ignored' ? '已忽略' : statusLabels[book.status] || '待选择'));
        const libraryName = book.library_name || '媒体库';
        const counts = el('p', 'transcode-book-counts', book.status === 'ignored' ?
          `${libraryName} · ${Number(book.page_count) || 0} 页` :
          `${libraryName} · 命中 ${Number(book.matched_count) || 0} 张 · 未处理 ${Number(book.pending_count) || 0} 张 · 成功 ${Number(book.success_count) || 0} 张 · 失败 ${Number(book.failed_count) || 0} 张`);
        main.append(heading, counts);
        if (['queued', 'running', 'paused'].includes(book.status)) { const track = el('div', 'transcode-progress'); track.setAttribute('role', 'progressbar'); track.setAttribute('aria-label', '优化进度'); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', progress(book)); const fill = el('span'); fill.style.width = progress(book) + '%'; track.append(fill); main.append(track); }
        const meta = el('div', 'transcode-book-meta');
        if (book.status !== 'ignored') meta.append(el('span', '', book.published ? '已在书库显示' : '暂未在书库显示'));
        if (book.output_count || book.actual_bytes) meta.append(el('span', '', `优化图 ${Number(book.output_count) || 0} 张 · ${bytes(book.actual_bytes)}`));
        if (book.needs_retranscode) meta.append(el('span', 'transcode-rebuild', '有图片需要重新优化'));
        meta.append(button('预览作品', 'transcode-preview-button', () => openPreview(book)));
        if (book.status !== 'ignored') meta.append(button('优化详情', 'transcode-preview-button', () => openDetails(book)));
        main.append(meta);
        if (book.reasons?.length || book.output_path || book.error) { const details = el('details', 'transcode-book-details'); details.dataset.bookId = id; details.open = open.has(id); details.append(el('summary', '', '查看详情'));
          if (book.reasons?.length) { const reasons = el('p', '', (Array.isArray(book.reasons) ? book.reasons : [book.reasons]).map(value => typeof value === 'string' ? value : value?.message || value?.reason || '').filter(Boolean).join(' · ')); details.append(reasons); }
          if (book.output_path) { const path = el('p', 'transcode-path'); path.append(el('span', '', '优化图路径'), el('code', '', book.output_path)); details.append(path); }
          if (book.error) details.append(el('p', 'form-error', book.error)); main.append(details);
        }
        row.append(select, main); state.rows.set(id, { signature, element: row }); return row;
      });
      const retained = new Set(rows);
      Array.from(controls.list.children).forEach(row => { if (!retained.has(row)) row.remove(); });
      rows.forEach((row, index) => { if (controls.list.children[index] !== row) controls.list.insertBefore(row, controls.list.children[index] || null); });
      controls.empty.hidden = Boolean(books.length); controls.empty.textContent = state.loading && !state.snapshot ? '正在读取优化记录…' : currentBooks().length ? '没有符合筛选条件的作品。' : state.tab === 'ignored' ? '暂无忽略的作品。' : '暂无命中规则的作品。';
      controls.all.disabled = state.busy || !books.length;
      renderSelection();
      if (focus) Array.from(controls.list.querySelectorAll('input[data-transcode-id]')).find(item => item.dataset.transcodeId === focus)?.focus({ preventScroll: true });
      if (state.snapshot) onRender();
    }
    function getFilters() { return normalizeFilters({ query: state.query, status: state.filter, library: state.library }); }
    function restoreFilters(value) {
      const filters = normalizeFilters(value);
      state.query = filters.query; state.filter = filters.status; state.library = filters.library;
      controls.query.value = state.query;
      if (state.snapshot) renderLibraries();
      renderBooks();
      return getFilters();
    }
    function accept(snapshot) {
      if (!snapshot || !Array.isArray(snapshot.books) || !snapshot.rules) throw new Error('优化数据格式不正确。');
      const ids = new Set([...snapshot.books, ...(snapshot.ignored_books || [])].map(book => String(book.id)));
      state.rows.forEach((value, id) => { if (!ids.has(id)) state.rows.delete(id); });
      const signature = JSON.stringify(snapshot); const published = JSON.stringify([
        snapshot.books.map(book => String(book.id) + ':' + Boolean(book.published)).sort(),
        (snapshot.ignored_books || []).map(book => String(book.id)).sort()
      ]);
      const changed = state.published && state.published !== published; state.published = published; state.snapshot = snapshot;
      const canIgnore = Array.isArray(snapshot.ignored_books);
      ignoreUnavailable.hidden = canIgnore;
      controls.ignoredTab.hidden = !canIgnore;
      if (!canIgnore && state.tab === 'ignored') switchTab('tasks');
      const currentIds = new Set(currentBooks().map(book => String(book.id)));
      state.selected.forEach(id => { if (!currentIds.has(id)) state.selected.delete(id); });
      if (!state.dirty) fillRules(snapshot.rules);
      if (signature !== state.signature) { state.signature = signature; renderSummary(); renderLibraries(); renderBooks(); }
      if (changed) Promise.resolve(onCatalogChange()).catch(() => {});
    }
    async function load(force = false) {
      if (!state.active || state.loading || state.busy || document.hidden && !force) { scheduleLoad(); return; }
      stopLoad();
      const request = {};
      state.request = request;
      const generation = state.generation; state.loading = true; state.manualLoading = force; updateBusy(); if (!state.snapshot) renderBooks();
      try { const snapshot = await api('/api/transcoding'); if (generation !== state.generation || state.request !== request) return; accept(snapshot); setError(''); controls.retryLoad.hidden = true; }
      catch (error) { if (generation === state.generation && state.request === request && error.status !== 401) { setError(error.message); controls.retryLoad.hidden = false; } }
      finally { if (generation === state.generation) { state.request = null; state.loading = state.manualLoading = false; updateBusy(); if (!state.snapshot) renderBooks(); scheduleLoad(); } }
    }
    function stopLoad() { clearTimeout(state.timer); state.timer = null; }
    function scheduleLoad() { stopLoad(); if (state.active && !document.hidden && state.tab !== 'rules') state.timer = setTimeout(() => load(), refreshDelay(state.snapshot)); }
    function switchTab(tab, updateRoute = true) {
      const previous = state.tab;
      state.tab = Object.hasOwn(tabButtons, tab) ? tab : 'tasks';
      if (state.tab === 'ignored' && state.snapshot && !Array.isArray(state.snapshot.ignored_books)) {
        state.tab = 'tasks';
        updateRoute = true;
      }
      const ignored = state.tab === 'ignored';
      tasks.hidden = state.tab === 'rules'; rules.hidden = !tasks.hidden;
      tasks.setAttribute('aria-labelledby', ignored ? 'transcode-tab-ignored' : 'transcode-tab-tasks');
      controls.summary.hidden = storageTools.hidden = statusHost.hidden = ignored;
      controls.list.setAttribute('aria-label', ignored ? '忽略的作品' : '优化作品');
      Object.entries(tabButtons).forEach(([key, item]) => { const selected = key === state.tab; item.setAttribute('aria-selected', String(selected)); item.tabIndex = selected ? 0 : -1; });
      if (updateRoute) onTabChange(state.tab);
      if (previous !== state.tab && (previous === 'ignored' || ignored)) state.selected.clear();
      if (previous !== state.tab) {
        if (state.tab === 'rules') syncFilters();
        else {
          if (state.snapshot) renderLibraries();
          renderBooks();
          if (state.active) load();
        }
      }
      else scheduleLoad();
    }
    async function mutate(url, payload, success, { saveRules = false } = {}) {
      stopLoad(); const generation = state.generation; state.busy = true; dialogError.hidden = true; updateBusy();
      try { const snapshot = await post(url, payload); if (generation !== state.generation) return; state.request = null; if (saveRules) { state.dirty = false; state.draft = null; } accept(snapshot); setError(''); dialog.close(); toast(typeof success === 'function' ? success(snapshot) : success); Promise.resolve(onCatalogChange()).catch(() => {}); }
      catch (error) {
        if (generation === state.generation && error.status !== 401) {
          const unsupported = (payload.action === 'ignore' || payload.action === 'restore') && error.message === '优化批量操作无效。';
          dialogError.textContent = unsupported ? ignoreUnavailable.textContent : error.message;
          dialogError.hidden = false;
        }
      }
      finally { if (generation === state.generation) { state.busy = false; updateBusy(); renderBooks(); scheduleLoad(); } }
    }
    function confirmAction(action) {
      if ((action === 'ignore' || action === 'restore') && !Array.isArray(state.snapshot?.ignored_books)) return;
      const selected = currentBooks().filter(book => state.selected.has(String(book.id)) && eligible(book, action)); if (!selected.length || state.busy) return;
      const count = selected.length; const descriptions = { original: `将 ${count} 本作品设为使用原图并在写真库显示。命中规则的记录仍会保留。`, transcode: `为 ${count} 本作品处理命中规则的图片。后台完成后自动显示新作品，原始文件保持不变。`, pause: `暂停 ${count} 本作品的优化任务。`, resume: `继续 ${count} 本作品的优化任务。`, cancel: `取消 ${count} 本作品的后台任务。新作品继续等待管理员选择。`, retry: `重新处理 ${count} 本作品中尚未成功的图片。` };
      descriptions.ignore = `将 ${count} 本作品移入忽略列表，从书库隐藏，并停止图片优化。`;
      descriptions.restore = `将 ${count} 本作品移出忽略列表。命中当前优化规则的作品会回到「待选择」，其余作品会在书库显示。`;
      const list = el('ul', 'transcode-confirm-list'); selected.forEach(book => list.append(el('li', '', book.title || '未命名作品')));
      openConfirm(actionLabels[action], [el('p', 'settings-intro', descriptions[action]), list], '确认' + actionLabels[action], () => mutate('/api/transcoding/actions', { ids: selected.map(book => book.id), action }, '操作已提交'));
    }
    function confirmCleanup() {
      const summary = state.snapshot?.summary; if (state.busy || state.loading || !summary || !(Number(summary.reclaimable_files) > 0)) return;
      const description = el('p', 'settings-intro', `所有写真库中共有 ${Number(summary.reclaimable_files)} 个可清理的旧副本，占用 ${bytes(summary.reclaimable_bytes)}。仅清理已不再使用的优化图，不影响原始图片、正在使用的版本或等待发布的图片。`);
      openConfirm('清理旧副本', [description], '确认清理', () => mutate('/api/transcoding/cleanup', {}, snapshot => {
        const result = snapshot.cleanup || {};
        return `已清理 ${Number(result.removed_files) || 0} 个旧副本，释放 ${bytes(result.removed_bytes)}${Number(result.skipped_files) > 0 ? `，跳过 ${Number(result.skipped_files)} 个文件` : ''}。`;
      }));
    }
    async function previewRules() {
      if (!state.dirty || state.busy || !form.reportValidity()) return; const values = readRules(); if (!values) return;
      const generation = state.generation; state.busy = true; updateBusy(); setError('');
      try {
        const preview = await post('/api/transcoding/rules/preview', { rules: values }); if (generation !== state.generation) return;
        state.draft = preview;
        const impacts = Array.isArray(preview.impacts) ? preview.impacts : []; const total = impacts.reduce((sum, book) => sum + (Number(book.needs_retranscode) || 0), 0);
        const intro = el('p', 'settings-intro', `涉及 ${impacts.length} 本作品，${total} 张图片需要重新优化。保存规则后不会自动开始优化。`);
        const list = el('div', 'transcode-impact-list');
        impacts.forEach(book => { const item = el('article', 'transcode-impact'); item.append(el('strong', '', book.title || '未命名作品'), el('small', '', `${book.library_name || '写真库'} · 新命中 ${Number(book.newly_matched) || 0} 张 · 不再命中 ${Number(book.no_longer_matched) || 0} 张 · 需重新优化 ${Number(book.needs_retranscode) || 0} 张`)); list.append(item); });
        if (!impacts.length) list.append(el('p', 'transcode-hint', '当前没有受影响的作品。'));
        openConfirm('确认优化规则', [intro, list], '确认保存规则', () => mutate('/api/transcoding/rules', { rules: preview.rules, expected_revision: preview.revision }, '优化规则已保存', { saveRules: true }));
      } catch (error) { if (generation === state.generation && error.status !== 401) setError(error.message); }
      finally { if (generation === state.generation) { state.busy = false; updateBusy(); } }
    }
    document.addEventListener?.('visibilitychange', () => { if (document.hidden) stopLoad(); else if (state.active && state.tab !== 'rules') load(); });
    fillRules(defaultRules); switchTab('tasks', false); renderBooks();
    return {
      show(tab = 'tasks') { state.active = true; host.hidden = false; switchTab(tab, false); syncFilters(); load(); },
      hide() { state.active = false; host.hidden = true; statusFilter.hide(); libraryFilter.hide(); stopLoad(); if (dialog.open && !state.busy) dialog.close(); if (previewDialog.open) previewDialog.close(); if (detailDialog.open) detailDialog.close(); },
      reset() { state.generation += 1; state.active = false; stopLoad(); state.snapshot = null; state.loading = state.manualLoading = state.busy = state.dirty = false; state.draft = null; state.selected.clear(); state.rows.clear(); state.filter = state.library = state.query = state.signature = state.published = state.librarySignature = ''; libraryOptions = allLibraries; controls.query.value = ''; if (dialog.open) dialog.close(); if (previewDialog.open) previewDialog.close(); if (detailDialog.open) detailDialog.close(); detailRequest += 1; setError(''); controls.retryLoad.hidden = controls.ignoredTab.hidden = ignoreUnavailable.hidden = true; fillRules(defaultRules); renderBooks(); host.hidden = true; },
      refresh: () => load(true), getFilters, restoreFilters, isLoaded: () => Boolean(state.snapshot)
    };
  }
  return { create, bytes, progress, eligible, statusLabels, defaultRules, bookDisplaySignature, normalizeFilters, formatParameters, formatTime, refreshDelay };
});

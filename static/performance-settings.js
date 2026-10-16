/* Device preferences and administrator-controlled background task limits. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SumiPerformance = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const defaults = Object.freeze({ webtoonStep: 3, coverConcurrency: 3, imageCacheLimit: 0, imageCacheMemoryMB: 256, bookPageSize: 0, chapterPageSize: 0 });
  const limits = { webtoonStep: [1, 20], coverConcurrency: [1, 12], imageCacheLimit: [0, 999], imageCacheMemoryMB: [0, 16384], bookPageSize: [0, 999], chapterPageSize: [0, 999] };
  function decodedImageBytes(size, fallback = 16 * 1024 * 1024) {
    const width = Number(size?.width); const height = Number(size?.height);
    return Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0
      ? Math.min(Number.MAX_SAFE_INTEGER, Math.ceil(width) * Math.ceil(height) * 4)
      : Math.max(0, Number.isFinite(fallback) ? fallback : 16 * 1024 * 1024);
  }
  function normalize(values = {}) {
    if (!values || typeof values !== 'object' || Array.isArray(values)) values = {};
    return Object.fromEntries(Object.entries(defaults).map(([key, fallback]) => {
      const number = values[key]; const [min, max] = limits[key];
      return [key, Number.isInteger(number) && number >= min && number <= max ? number : fallback];
    }));
  }
  function paginate(total, requestedPage, capacity) {
    const size = Math.max(1, Math.trunc(Number(capacity)) || 1);
    const count = Math.max(1, Math.ceil(Math.max(0, total) / size));
    const page = Math.min(count - 1, Math.max(0, Math.trunc(Number(requestedPage)) || 0));
    return { page, count, size, start: page * size, end: Math.min(Math.max(0, total), (page + 1) * size) };
  }
  function catalogWindow(total, requestedPage, capacity, revealed, batchSize = 24) {
    const integer = (value, fallback = 0) => {
      const number = Number(value);
      return Number.isFinite(number) ? Math.min(Number.MAX_SAFE_INTEGER, Math.max(0, Math.trunc(number))) : fallback;
    };
    const length = integer(total); const pageSize = integer(capacity);
    if (pageSize) return { ...paginate(length, integer(requestedPage), pageSize), flow: false, hasMore: false };
    const size = Math.max(1, integer(batchSize, 24));
    const end = Math.min(length, Math.max(size, integer(revealed)));
    return { page: 0, count: 1, size, start: 0, end, flow: true, hasMore: end < length };
  }
  function createQueue(initialLimit = 3) {
    let limit = Math.max(1, Math.trunc(initialLimit) || 1); const pending = []; const active = new Set();
    function pump() {
      while (active.size < limit && pending.length) {
        const item = pending.shift(); active.add(item);
        Promise.resolve().then(() => item.task(item.controller.signal)).then(item.resolve, item.reject).finally(() => { active.delete(item); pump(); });
      }
    }
    return {
      add(task, key) {
        return new Promise((resolve, reject) => { pending.push({ task, key, resolve, reject, controller: new AbortController() }); pump(); });
      },
      setLimit(value) { limit = Math.max(1, Math.trunc(value) || 1); pump(); },
      cancel(predicate = () => true) {
        for (let index = pending.length - 1; index >= 0; index -= 1) if (predicate(pending[index].key)) { const [item] = pending.splice(index, 1); item.controller.abort(); item.resolve(false); }
        active.forEach(item => { if (predicate(item.key)) item.controller.abort(); });
      },
      stats() { return { active: active.size, pending: pending.length, limit }; }
    };
  }
  function createCache(initialLimit = 0, dispose = () => {}, options = {}) {
    const entries = new Map(); let limit = Math.max(0, Math.trunc(initialLimit) || 0);
    let byteLimit = Math.max(0, Math.trunc(options.byteLimit) || 0);
    const weight = typeof options.weight === 'function' ? options.weight : value => value?.bytes || 0;
    const bytes = () => [...entries.values()].reduce((sum, value) => {
      const size = Number(weight(value));
      return Math.min(Number.MAX_SAFE_INTEGER, sum + (Number.isFinite(size) && size > 0 ? size : 0));
    }, 0);
    const api = {
      get(key) { const value = entries.get(key); if (value !== undefined) { entries.delete(key); entries.set(key, value); } return value; },
      peek: key => entries.get(key),
      set(key, value) { if (entries.has(key) && entries.get(key) !== value) dispose(entries.get(key), key); entries.delete(key); entries.set(key, value); return value; },
      delete(key) { if (!entries.has(key)) return; const value = entries.get(key); entries.delete(key); dispose(value, key); },
      trim(protectedKeys = []) {
        if (!limit && !byteLimit) return;
        const protectedSet = new Set(protectedKeys);
        for (const key of entries.keys()) {
          if ((!limit || entries.size <= limit) && (!byteLimit || bytes() <= byteLimit)) break;
          if (!protectedSet.has(key)) api.delete(key);
        }
      },
      setLimit(value, protectedKeys = []) { limit = Math.max(0, Math.trunc(value) || 0); api.trim(protectedKeys); },
      setByteLimit(value, protectedKeys = []) { byteLimit = Math.max(0, Math.trunc(value) || 0); api.trim(protectedKeys); },
      setLimits(count, memory, protectedKeys = []) { limit = Math.max(0, Math.trunc(count) || 0); byteLimit = Math.max(0, Math.trunc(memory) || 0); api.trim(protectedKeys); },
      clear() { [...entries.keys()].forEach(key => api.delete(key)); },
      keys: () => [...entries.keys()],
      get bytes() { return bytes(); },
      get availableBytes() { return byteLimit ? Math.max(0, byteLimit - bytes()) : Infinity; },
      get size() { return entries.size; }
    };
    return api;
  }
  let settings = { ...defaults };
  let accountStorageKey = null;
  let currentUser = null;
  let serverAPI = null;
  let serverEpoch = 0;
  let serverState = { value: 1, loaded: false, loading: false, saving: false, error: '', message: '', request: null };
  let renderedPanel = null;
  let serverControls = null;
  function resetServer() {
    serverEpoch += 1;
    serverState = { value: 1, loaded: false, loading: false, saving: false, error: '', message: '', request: null };
    renderedPanel?.replaceChildren();
    renderedPanel = null; serverControls = null;
  }
  function configureServer(options) {
    resetServer();
    serverAPI = options || null;
  }
  function isServerAdmin() {
    return currentUser?.role === 'admin' && typeof serverAPI?.api === 'function' && typeof serverAPI?.post === 'function' && (!serverAPI.isAdmin || serverAPI.isAdmin());
  }
  function serverValue(payload) {
    if (!Number.isInteger(payload?.taskConcurrency) || payload.taskConcurrency < 1 || payload.taskConcurrency > 8 || payload?.maxTaskConcurrency !== 8) throw new Error('服务端返回的任务设置无效。');
    return payload.taskConcurrency;
  }
  function renderServerState() {
    if (!serverControls) return;
    const allowed = isServerAdmin();
    serverControls.group.hidden = !allowed;
    serverControls.input.disabled = !allowed || !serverState.loaded || serverState.loading || serverState.saving;
    serverControls.save.disabled = serverControls.input.disabled;
    serverControls.save.textContent = serverState.saving ? '保存中…' : '保存';
    serverControls.status.textContent = serverState.loading ? '正在读取…' : serverState.error || serverState.message;
    serverControls.status.className = serverState.error ? 'performance-server-status form-error' : 'performance-server-status';
    serverControls.status.hidden = !serverControls.status.textContent;
    serverControls.retry.hidden = !allowed || serverState.loaded || !serverState.error;
    serverControls.retry.disabled = serverState.loading || serverState.saving;
  }
  function loadServer() {
    if (!isServerAdmin() || serverState.loaded) return Promise.resolve();
    if (serverState.request) return serverState.request;
    const epoch = serverEpoch;
    serverState.loading = true; serverState.error = ''; serverState.message = ''; renderServerState();
    const read = serverAPI.api;
    const request = Promise.resolve().then(() => {
      if (epoch !== serverEpoch || !isServerAdmin()) return;
      return read('/api/performance');
    }).then(payload => {
      if (epoch !== serverEpoch || !isServerAdmin()) return;
      serverState.value = serverValue(payload); serverState.loaded = true;
      if (serverControls) serverControls.input.value = serverState.value;
    }).catch(error => {
      if (epoch !== serverEpoch || !isServerAdmin()) return;
      serverState.error = '读取后台任务设置失败：' + (error.message || '请稍后重试。');
    }).finally(() => {
      if (epoch !== serverEpoch || !isServerAdmin()) return;
      serverState.loading = false; serverState.request = null; renderServerState();
    });
    serverState.request = request;
    return request;
  }
  async function saveServer(event) {
    event.preventDefault();
    if (!isServerAdmin() || !serverState.loaded || serverState.loading || serverState.saving) return;
    const input = serverControls.input; const value = Number(input.value);
    serverState.error = ''; serverState.message = '';
    if (String(input.value).trim() === '' || !input.checkValidity() || !Number.isInteger(value) || value < 1 || value > 8) {
      input.reportValidity(); input.value = serverState.value;
      serverState.error = '后台任务并发数需要填写 1～8 的整数。'; renderServerState(); return;
    }
    const epoch = serverEpoch;
    serverState.saving = true; renderServerState();
    try {
      const payload = await serverAPI.post('/api/performance', { taskConcurrency: value });
      if (epoch !== serverEpoch || !isServerAdmin()) return;
      serverState.value = serverValue(payload); input.value = serverState.value; serverState.message = '已保存';
    } catch (error) {
      if (epoch !== serverEpoch || !isServerAdmin()) return;
      input.value = serverState.value;
      serverState.error = '保存后台任务设置失败：' + (error.message || '请稍后重试。');
    } finally {
      if (epoch === serverEpoch && isServerAdmin()) { serverState.saving = false; renderServerState(); }
    }
  }
  const covers = createQueue(settings.coverConcurrency); const coverRecords = new Map(); let observer; let detachObserver;
  function loadCover(image, url, lazy = true) {
    const record = { started: false, start() {
      if (record.started || !image.isConnected) return;
      record.started = true; observer?.unobserve(image);
      covers.add(signal => new Promise(resolve => {
        let finished = false;
        const finish = () => { if (finished) return; finished = true; image.removeEventListener('load', finish); image.removeEventListener('error', finish); signal.removeEventListener('abort', abort); coverRecords.delete(image); resolve(true); };
        const abort = () => { image.removeAttribute('src'); finish(); };
        if (signal.aborted || !image.isConnected) { abort(); return; }
        image.addEventListener('load', finish); image.addEventListener('error', finish); signal.addEventListener('abort', abort, { once: true });
        image.loading = 'eager'; image.src = url;
      }), image).catch(() => { coverRecords.delete(image); });
    } };
    coverRecords.set(image, record);
    if (!detachObserver && typeof MutationObserver !== 'undefined') {
      detachObserver = new MutationObserver(() => { coverRecords.forEach((item, target) => { if (!target.isConnected) { observer?.unobserve(target); covers.cancel(key => key === target); coverRecords.delete(target); } }); });
      detachObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
    if (lazy && typeof IntersectionObserver !== 'undefined') {
      observer ||= new IntersectionObserver(entries => entries.forEach(entry => { if (entry.isIntersecting) coverRecords.get(entry.target)?.start(); }), { rootMargin: '200px' });
      observer.observe(image);
    } else queueMicrotask(record.start);
  }
  function update(values) {
    settings = normalize({ ...settings, ...values }); covers.setLimit(settings.coverConcurrency);
    try { if (accountStorageKey) localStorage.setItem(accountStorageKey, JSON.stringify(settings)); } catch (_) { /* Still applies to this visit. */ }
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('sumi-performance-change', { detail: { ...settings } }));
    return { ...settings };
  }
  function setUser(user) {
    currentUser = user || null; resetServer();
    accountStorageKey = user?.id !== undefined ? `gravelle-user-${encodeURIComponent(String(user.id))}-sumi-performance` : null;
    let saved = null;
    try {
      if (accountStorageKey) saved = localStorage.getItem(accountStorageKey);
      if (saved === null && user?.role === 'admin' && Number(user.id) === 1) {
        saved = localStorage.getItem('sumi-performance');
        if (saved !== null) localStorage.setItem(accountStorageKey, saved);
      }
    } catch (_) { /* Keep safe defaults. */ }
    covers.cancel(); coverRecords.forEach((_, image) => observer?.unobserve(image)); coverRecords.clear();
    settings = normalize((() => { try { return JSON.parse(saved || '{}'); } catch (_) { return {}; } })());
    covers.setLimit(settings.coverConcurrency);
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('sumi-performance-change', { detail: { ...settings } }));
    return { ...settings };
  }
  function render() {
    const panel = document.getElementById('performance-settings'); if (!panel) return;
    // Refresh status without rebuilding inputs or replacing an unfinished edit.
    if (renderedPanel === panel) { renderServerState(); if (isServerAdmin() && !serverState.error) loadServer(); return; }
    panel.replaceChildren();
    panel.className = 'performance-settings-page';
    renderedPanel = panel;
    const make = (tag, text, className) => { const element = document.createElement(tag); if (text) element.textContent = text; if (className) element.className = className; return element; };
    if (isServerAdmin()) {
      const group = make('section', '', 'settings-panel performance-server-settings');
      group.append(make('h2', '后台任务'));
      const form = make('form', '', 'setting-row performance-row performance-task-row');
      const label = make('label'); label.htmlFor = 'performance-taskConcurrency'; label.append(make('strong', '后台任务并发数'), make('small', '图片优化与图片索引共用；默认 1，其他任务排队'));
      const actions = make('div', '', 'performance-task-actions');
      const taskInput = make('input'); taskInput.type = 'number'; taskInput.id = 'performance-taskConcurrency'; taskInput.min = 1; taskInput.max = 8; taskInput.step = 1; taskInput.required = true; taskInput.value = serverState.value; taskInput.setAttribute('aria-label', '后台任务并发数');
      const save = make('button', '保存', 'button button-primary'); save.type = 'submit';
      const formEpoch = serverEpoch;
      actions.append(taskInput, save); form.append(label, actions); form.addEventListener('submit', event => {
        if (formEpoch !== serverEpoch) { event.preventDefault(); return; }
        return saveServer(event);
      });
      const feedback = make('div', '', 'performance-server-feedback');
      const status = make('p', '', 'performance-server-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); status.hidden = true;
      const retry = make('button', '重试', 'button button-secondary'); retry.type = 'button'; retry.hidden = true; retry.addEventListener('click', loadServer);
      feedback.append(status, retry); group.append(form, feedback); panel.append(group);
      serverControls = { group, input: taskInput, save, status, retry };
    }
    const device = make('section', '', 'settings-panel performance-device-settings');
    device.append(make('h2', '加载与列表'), make('p', '按当前账号保存在此设备。', 'settings-intro'));
    const rows = [
      ['webtoonStep', '条漫加载步进', '预加载当前页前后各几页', 1, 20],
      ['coverConcurrency', '封面加载并发数', '同时加载的列表封面数量', 1, 12],
      ['imageCacheLimit', '图片缓存数量限制', '0 表示不限制；正在显示的页始终保留', 0, 999],
      ['imageCacheMemoryMB', '图片解码缓存预算（MiB）', '默认 256；0 不限制，当前显示页始终保留', 0, 16384],
      ['bookPageSize', '作品页面容量', '0 使用瀑布流滚动加载；大于 0 按指定数量分页', 0, 999]
    ];
    rows.forEach(([key, title, hint, min, max]) => {
      const row = make('label', '', 'setting-row performance-row'); const label = make('span'); label.append(make('strong', title), make('small', hint));
      const input = make('input'); input.type = 'number'; input.id = 'performance-' + key; input.min = min; input.max = max; input.step = 1; input.required = true; input.value = settings[key]; input.setAttribute('aria-label', title);
      const saveValidValue = () => {
        const value = Number(input.value);
        if (String(input.value).trim() === '' || !input.checkValidity() || !Number.isInteger(value)) return false;
        if (settings[key] !== value) update({ [key]: value });
        return true;
      };
      input.addEventListener('input', saveValidValue);
      input.addEventListener('change', () => { if (!saveValidValue()) { input.reportValidity(); input.value = settings[key]; } });
      input.addEventListener('blur', () => { if (!saveValidValue()) input.value = settings[key]; });
      row.append(label, input); device.append(row);
    });
    device.append(make('p', '按图片像素估算浏览器解码占用，优先释放较远页面；当前页可能超过预算。不清理浏览器网络缓存。', 'performance-note'));
    panel.append(device);
    renderServerState(); if (isServerAdmin()) loadServer();
  }
  return { defaults, normalize, paginate, catalogWindow, createQueue, createCache, decodedImageBytes, get: () => ({ ...settings }), update, setUser, configureServer, loadCover, render };
});

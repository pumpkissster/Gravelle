/* Device-specific loading and pagination preferences. No build step required. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SumiPerformance = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const defaults = Object.freeze({ webtoonStep: 3, coverConcurrency: 3, imageCacheLimit: 0, bookPageSize: 0, chapterPageSize: 0 });
  const limits = { webtoonStep: [1, 20], coverConcurrency: [1, 12], imageCacheLimit: [0, 999], bookPageSize: [0, 999], chapterPageSize: [0, 999] };
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
  function createCache(initialLimit = 0, dispose = () => {}) {
    const entries = new Map(); let limit = Math.max(0, Math.trunc(initialLimit) || 0);
    const api = {
      get(key) { const value = entries.get(key); if (value !== undefined) { entries.delete(key); entries.set(key, value); } return value; },
      peek: key => entries.get(key),
      set(key, value) { if (entries.has(key) && entries.get(key) !== value) dispose(entries.get(key), key); entries.delete(key); entries.set(key, value); return value; },
      delete(key) { if (!entries.has(key)) return; const value = entries.get(key); entries.delete(key); dispose(value, key); },
      trim(protectedKeys = []) {
        if (!limit) return;
        const protectedSet = new Set(protectedKeys);
        for (const key of entries.keys()) { if (entries.size <= limit) break; if (!protectedSet.has(key)) api.delete(key); }
      },
      setLimit(value, protectedKeys = []) { limit = Math.max(0, Math.trunc(value) || 0); api.trim(protectedKeys); },
      clear() { [...entries.keys()].forEach(key => api.delete(key)); },
      keys: () => [...entries.keys()],
      get size() { return entries.size; }
    };
    return api;
  }
  let settings = { ...defaults };
  if (typeof localStorage !== 'undefined') { try { settings = normalize(JSON.parse(localStorage.getItem('sumi-performance') || '{}')); } catch (_) { /* Keep safe defaults. */ } }
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
    try { localStorage.setItem('sumi-performance', JSON.stringify(settings)); } catch (_) { /* Still applies to this visit. */ }
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('sumi-performance-change', { detail: { ...settings } }));
    return { ...settings };
  }
  function render() {
    const panel = document.getElementById('performance-settings'); if (!panel) return;
    // A library refresh must not interrupt a number the user is still typing.
    if (panel.contains?.(document.activeElement)) return;
    panel.replaceChildren();
    const make = (tag, text, className) => { const element = document.createElement(tag); if (text) element.textContent = text; if (className) element.className = className; return element; };
    panel.append(make('h2', '性能设置'), make('p', '仅保存在当前设备，可按设备性能和网络状况调整。', 'settings-intro'));
    const rows = [
      ['webtoonStep', '条漫加载步进', '预加载当前页前后各几页', 1, 20],
      ['coverConcurrency', '封面加载并发数', '同时加载的列表封面数量', 1, 12],
      ['imageCacheLimit', '图片缓存数量限制', '0 表示不限制；正在显示的页始终保留', 0, 999],
      ['bookPageSize', '作品页面容量', '0 使用瀑布流滚动加载；大于 0 按指定数量分页', 0, 999],
      ['chapterPageSize', '章节页面容量', '0 自动适应章节目录高度', 0, 999]
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
      row.append(label, input); panel.append(row);
    });
    panel.append(make('p', '图片缓存限制控制阅读器保留的原图数量，不清理浏览器自身的网络缓存。', 'performance-note'));
  }
  return { defaults, normalize, paginate, catalogWindow, createQueue, createCache, get: () => ({ ...settings }), update, loadCover, render };
});

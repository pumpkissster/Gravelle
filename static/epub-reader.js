/* EPUB chapter reader. Book HTML stays in a script-disabled, isolated document. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.GravelleEpubReader = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const defaults = Object.freeze({ font: 20, line: 1.8, width: 720, theme: 'dark' });
  const clamp = (value, low, high, fallback = low) => Number.isFinite(Number(value)) ? Math.min(high, Math.max(low, Number(value))) : fallback;
  const position = value => Math.round(clamp(value, 0, 1) * 100000) / 100000;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function preferences(value = {}) {
    return { font: Math.round(clamp(value.font, 14, 32, defaults.font) / 2) * 2, line: Math.round(clamp(value.line, 1.4, 2.4, defaults.line) * 5) / 5, width: Math.round(clamp(value.width, 520, 1040, defaults.width) / 40) * 40, theme: value.theme === 'light' ? 'light' : 'dark' };
  }
  function chapterIndex(value, count) {
    return Math.floor(clamp(value, 0, Math.max(0, Math.floor(Number(count) || 0) - 1)));
  }
  function scrollPosition(metrics = {}) {
    const range = Math.max(0, Number(metrics.scrollHeight || 0) - Number(metrics.clientHeight || 0));
    return range > 0 ? position(Number(metrics.scrollTop || 0) / range) : 0;
  }
  function linkTarget(element, count) {
    const index = element?.getAttribute?.('data-epub-index');
    if (!/^(0|[1-9][0-9]{0,4})$/.test(String(index ?? '')) || Number(index) >= count) return null;
    return { page: Number(index), fragment: String(element.getAttribute('data-epub-fragment') || '').replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 512) };
  }
  function chapterDocument(html, title, prefs, token = 0) {
    const p = preferences(prefs);
    // CSP precedes the supplied fragment. The sandbox also forbids scripts, forms,
    // popups and top navigation; book styles, fonts and network access are blocked.
    return `<!doctype html><html lang="zh-CN" data-epub-body data-theme="${p.theme}" data-font="${p.font}" data-line="${p.line.toFixed(1)}" data-width="${p.width}"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'self'; font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none'; form-action 'none'; base-uri 'none'"><meta name="gravelle-epub-document" content="${Math.max(0, Math.floor(Number(token) || 0))}"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escape(title)}</title><link rel="stylesheet" href="/epub-reader.css"></head><body>${String(html ?? '')}</body></html>`;
  }

  function create(options = {}) {
    const doc = options.document || (typeof document !== 'undefined' ? document : null);
    const win = options.window || doc?.defaultView || (typeof window !== 'undefined' ? window : null);
    if (!doc || !win || typeof options.api !== 'function') throw new TypeError('EPUB 阅读器需要页面和 API。');
    const raf = callback => (win.requestAnimationFrame || (fn => win.setTimeout(fn, 0)))(callback);
    const delay = (fn, time) => win.setTimeout(fn, time);
    const clear = handle => win.clearTimeout(handle);
    const dialog = doc.createElement('dialog'); dialog.id = 'epub-reader'; dialog.className = 'epub-reader';
    dialog.setAttribute('aria-label', 'EPUB 阅读器');
    dialog.innerHTML = `<div class="epub-shell"><header class="epub-toolbar"><button type="button" class="epub-icon" data-action="close" aria-label="关闭阅读器，返回书架">${icon('back')}</button><button type="button" class="epub-icon" data-action="toc" aria-label="打开章节目录" aria-expanded="false" aria-controls="epub-toc">${icon('contents')}</button><div class="epub-heading"><strong data-role="title"></strong><span data-role="chapter-title"></span></div><div class="epub-tools"><button type="button" class="epub-chip" data-action="settings" aria-expanded="false" aria-controls="epub-settings">Aa<span>排版</span></button><button type="button" class="epub-icon" data-action="theme" aria-label="切换为浅色阅读背景">${icon('theme')}</button><button type="button" class="epub-icon" data-action="bookmark" aria-label="添加书签">${icon('bookmark')}</button></div></header><div class="epub-content"><iframe class="epub-frame" title="EPUB 章节正文" sandbox="allow-same-origin" referrerpolicy="no-referrer"></iframe><div class="epub-message" role="status" aria-live="polite"><span data-role="message"></span><button type="button" class="epub-chip" data-action="retry" hidden>重试</button></div><button type="button" class="epub-shade" data-action="dismiss" aria-label="关闭阅读选项" hidden></button><aside id="epub-toc" class="epub-panel epub-toc" aria-label="章节目录" hidden><div class="epub-panel-heading"><h2>章节目录</h2><button type="button" class="epub-icon" data-action="dismiss" aria-label="关闭章节目录">${icon('close')}</button></div><nav data-role="toc"></nav></aside><section id="epub-settings" class="epub-panel epub-settings" aria-label="阅读排版" hidden><div class="epub-panel-heading"><h2>阅读排版</h2><button type="button" class="epub-icon" data-action="dismiss" aria-label="关闭阅读排版">${icon('close')}</button></div><div class="epub-setting"><span>字号</span><div><button type="button" data-setting="font" data-step="-2" aria-label="缩小字号">A−</button><output data-output="font"></output><button type="button" data-setting="font" data-step="2" aria-label="放大字号">A＋</button></div></div><div class="epub-setting"><span>行距</span><div><button type="button" data-setting="line" data-step="-.2" aria-label="减小行距">−</button><output data-output="line"></output><button type="button" data-setting="line" data-step=".2" aria-label="增加行距">＋</button></div></div><div class="epub-setting epub-width-setting"><span>行宽</span><div><button type="button" data-setting="width" data-step="-80" aria-label="减小行宽">−</button><output data-output="width"></output><button type="button" data-setting="width" data-step="80" aria-label="增加行宽">＋</button></div></div></section></div><footer class="epub-footer"><button type="button" class="epub-chip" data-action="previous">${icon('back')}<span>上一章</span></button><span class="epub-location" data-role="location"></span><button type="button" class="epub-save" data-action="save" title="重试保存进度" disabled data-role="save-status">进度已保存</button><button type="button" class="epub-chip" data-action="next"><span>下一章</span>${icon('next')}</button></footer></div>`;
    doc.body.append(dialog);
    const $ = selector => dialog.querySelector(selector);
    const all = selector => Array.from(dialog.querySelectorAll(selector));
    const frame = $('.epub-frame');
    const state = { open: false, book: null, page: 0, position: 0, completed: false, loaded: false, token: 0, controller: null, fragment: '', restoring: false, restoreSequence: 0, recovery: null, prefs: { ...defaults }, pending: null, saveSeq: 0, saveTimer: null, routeTimer: null, detachFrame: null, savedOverflow: '', panel: null, source: null };
    const saveChains = new Map();
    const failedSaves = new Map();
    let saveGeneration = 0; let accountKey;

    function settingKey() { return typeof options.settingsKey === 'function' ? options.settingsKey() : options.settingsKey; }
    function discardAccountSaves() { ++saveGeneration; ++state.saveSeq; state.pending = null; failedSaves.clear(); saveChains.clear(); clear(state.saveTimer); }
    function readPreferences(theme) {
      let value = {};
      const key = settingKey();
      if (key) { try { value = JSON.parse(win.localStorage.getItem(key) || '{}') || {}; } catch (_) {} }
      state.prefs = preferences({ ...defaults, theme, ...value });
    }
    function rememberPreferences() { const key = settingKey(); if (key) { try { win.localStorage.setItem(key, JSON.stringify(state.prefs)); } catch (_) {} } }
    function location() { return { bookId: String(state.book?.id || ''), page: state.page, position: position(state.position), completed: state.completed, fragment: '' }; }
    function scroller() { try { return frame.contentDocument?.scrollingElement || frame.contentDocument?.documentElement; } catch (_) { return null; } }
    function capture() { const element = scroller(); if (state.loaded && element && !state.restoring && !state.completed) state.position = scrollPosition(element); return location(); }
    function routeNow() { clear(state.routeTimer); if (state.open) options.onRoute?.(state.book, location()); }
    function updateLocation() {
      $('[data-role="location"]').textContent = `第 ${state.page + 1} / ${state.book?.page_count || 1} 章 · ${Math.round(state.position * 100)}%`;
      $('[data-action="previous"]').disabled = !state.loaded || state.page === 0;
      const next = $('[data-action="next"]'); next.disabled = !state.loaded || state.completed;
      next.querySelector('span').textContent = state.page === Number(state.book?.page_count) - 1 ? (state.completed ? '已读完' : '完成阅读') : '下一章';
    }
    function status(text, failed = false) { const control = $('[data-role="save-status"]'); control.textContent = text; control.disabled = !failed; control.dataset.failed = String(failed); }
    function queueSave({ completed = state.completed, immediate = false, keepalive = false } = {}) {
      if (!state.book || typeof options.saveProgress !== 'function' || !state.loaded) return Promise.resolve();
      const at = capture(); state.completed = Boolean(completed);
      state.pending = { book: state.book, payload: { page: at.page, position: state.completed ? 1 : at.position, completed: state.completed }, seq: ++state.saveSeq, generation: saveGeneration };
      failedSaves.delete(String(state.book.id));
      clear(state.saveTimer); status('等待保存…');
      if (immediate) return flush(keepalive);
      state.saveTimer = delay(() => flush(), 650); return Promise.resolve();
    }
    function flush(keepalive = false) {
      clear(state.saveTimer); const item = state.pending || failedSaves.get(String(state.book?.id)); if (!item) return Promise.resolve(); state.pending = null;
      const prior = saveChains.get(String(item.book.id)) || Promise.resolve();
      const operation = prior.catch(() => {}).then(async () => {
        // A queued write may have waited for an earlier request while the account
        // was being changed. Never invoke the host API with that stale payload.
        if (item.generation !== saveGeneration) return;
        if (state.open && state.book === item.book && item.seq === state.saveSeq) status('正在保存…');
        try {
          await options.saveProgress(item.book, item.payload, { keepalive });
          if (item.generation !== saveGeneration) return;
          if (failedSaves.get(String(item.book.id))?.seq <= item.seq) failedSaves.delete(String(item.book.id));
          if (state.open && state.book === item.book && item.seq === state.saveSeq) status('进度已保存');
        } catch (error) {
          if (item.generation !== saveGeneration) return;
          if (error?.status !== 401 && (!failedSaves.get(String(item.book.id)) || failedSaves.get(String(item.book.id)).seq < item.seq)) failedSaves.set(String(item.book.id), item);
          if (state.open && state.book === item.book && item.seq === state.saveSeq) status('进度未保存，点击重试', true);
          if (error?.status !== 401) options.onSaveError?.(error, item.book);
        }
      });
      saveChains.set(String(item.book.id), operation);
      operation.finally(() => { if (saveChains.get(String(item.book.id)) === operation) saveChains.delete(String(item.book.id)); }); return operation;
    }
    function showMessage(text, retry = false) {
      $('.epub-message').hidden = !text; $('[data-role="message"]').textContent = text; $('[data-action="retry"]').hidden = !retry;
    }
    function closePanel(focus = false) {
      const previous = state.panel; state.panel = null;
      $('#epub-toc').hidden = true; $('#epub-settings').hidden = true; $('.epub-shade').hidden = true;
      $('[data-action="toc"]').setAttribute('aria-expanded', 'false'); $('[data-action="settings"]').setAttribute('aria-expanded', 'false');
      if (focus && previous) $(`[data-action="${previous}"]`).focus();
    }
    function togglePanel(name) {
      const current = state.panel; closePanel(); if (current === name) return;
      state.panel = name; $(`#epub-${name === 'toc' ? 'toc' : 'settings'}`).hidden = false; $('.epub-shade').hidden = false;
      $(`[data-action="${name}"]`).setAttribute('aria-expanded', 'true');
      (name === 'toc' ? $('[data-role="toc"] [aria-current="location"]') || $('[data-role="toc"] button') : $('#epub-settings button[data-setting]'))?.focus({ preventScroll: true });
      if (name === 'toc') $('[data-role="toc"] [aria-current="location"]')?.scrollIntoView({ block: 'nearest' });
    }
    function renderToc() {
      const list = $('[data-role="toc"]'); list.replaceChildren();
      const chapters = Array.isArray(state.book.epub_chapters) ? state.book.epub_chapters : [];
      const toc = Array.isArray(state.book.epub_toc) && state.book.epub_toc.length ? state.book.epub_toc : chapters;
      for (const entry of toc) {
        if (!Number.isInteger(entry.index) || entry.index < 0 || entry.index >= state.book.page_count) continue;
        const button = doc.createElement('button'); button.type = 'button'; button.textContent = entry.title || `第 ${entry.index + 1} 章`;
        button.dataset.page = String(entry.index); button.dataset.fragment = String(entry.fragment || '');
        button.dataset.depth = String(Math.floor(clamp(entry.depth, 0, 5))); if (entry.index === state.page) button.setAttribute('aria-current', 'location');
        list.append(button);
      }
      if (!list.childElementCount) { const item = doc.createElement('p'); item.textContent = '暂无章节目录'; list.append(item); }
    }
    function applyPreferences() {
      dialog.dataset.theme = state.prefs.theme;
      $('[data-action="theme"]').setAttribute('aria-label', state.prefs.theme === 'dark' ? '切换为浅色阅读背景' : '切换为深色阅读背景');
      $('[data-output="font"]').textContent = state.prefs.font + ' px'; $('[data-output="line"]').textContent = state.prefs.line.toFixed(1); $('[data-output="width"]').textContent = state.prefs.width + ' px';
      all('[data-setting]').forEach(button => {
        const key = button.dataset.setting, delta = Number(button.dataset.step);
        const next = preferences({ ...state.prefs, [key]: state.prefs[key] + delta }); button.disabled = Math.abs(next[key] - state.prefs[key]) < .001;
      });
      try {
        const element = frame.contentDocument?.documentElement; if (!element) return;
        element.dataset.theme = state.prefs.theme; element.dataset.font = String(state.prefs.font); element.dataset.line = state.prefs.line.toFixed(1); element.dataset.width = String(state.prefs.width);
      } catch (_) {}
    }
    function restore(at, fragment = '', syncRoute = true) {
      const element = scroller(); if (!element) return;
      state.restoring = true; const token = state.token; const sequence = ++state.restoreSequence;
      const apply = () => {
        if (!state.open || token !== state.token || sequence !== state.restoreSequence) return;
        let target = null; try { if (fragment) target = frame.contentDocument.getElementById(fragment); } catch (_) {}
        if (target) target.scrollIntoView({ block: 'start' });
        else element.scrollTop = position(at) * Math.max(0, element.scrollHeight - element.clientHeight);
        state.position = state.completed ? 1 : scrollPosition(element); updateLocation();
      };
      apply(); raf(() => { apply(); raf(() => { if (!state.open || token !== state.token || sequence !== state.restoreSequence) return; state.restoring = false; state.position = state.completed ? 1 : scrollPosition(element); updateLocation(); if (syncRoute) routeNow(); }); });
    }
    function bindFrame(token, targetPosition, targetFragment) {
      state.detachFrame?.(); state.detachFrame = null;
      let child; let content; try { child = frame.contentWindow; content = frame.contentDocument; } catch (_) { return; }
      if (!child || !content) return;
      const recovery = { token, position: targetPosition, fragment: targetFragment, active: true };
      state.recovery = recovery;
      const stopRecovery = () => { recovery.active = false; ++state.restoreSequence; state.restoring = false; };
      const onImageSettled = () => {
        if (!state.open || !state.loaded || token !== state.token || state.recovery !== recovery || !recovery.active) return;
        // Lazy images need not finish before the chapter becomes readable. While
        // the reader has not interacted, restore the requested location as their
        // dimensions become known. Never pull the reader away from manual input.
        restore(recovery.position, recovery.fragment);
      };
      const images = Array.from(content.querySelectorAll('img'));
      images.forEach(image => { image.addEventListener('load', onImageSettled); image.addEventListener('error', onImageSettled); });
      const onScroll = () => {
        if (!state.open || token !== state.token || state.restoring || !state.loaded) return;
        stopRecovery();
        const nextPosition = scrollPosition(scroller()); if (nextPosition < .999) state.completed = false;
        state.position = state.completed ? 1 : nextPosition; updateLocation(); queueSave(); clear(state.routeTimer); state.routeTimer = delay(routeNow, 450);
      };
      const onClick = event => {
        const anchor = event.target?.closest?.('a'); if (!anchor) return;
        event.preventDefault(); event.stopPropagation(); const target = linkTarget(anchor, state.book.page_count);
        if (target) navigate(target.page, { position: 0, fragment: target.fragment });
      };
      const onKey = event => { stopRecovery(); handleKey(event); };
      child.addEventListener('scroll', onScroll, { passive: true }); content.addEventListener('click', onClick, true); content.addEventListener('keydown', onKey);
      ['pointerdown', 'wheel', 'touchstart'].forEach(type => content.addEventListener(type, stopRecovery, { passive: true, capture: true }));
      state.detachFrame = () => {
        recovery.active = false; if (state.recovery === recovery) state.recovery = null;
        child.removeEventListener('scroll', onScroll); content.removeEventListener('click', onClick, true); content.removeEventListener('keydown', onKey);
        ['pointerdown', 'wheel', 'touchstart'].forEach(type => content.removeEventListener(type, stopRecovery, true));
        images.forEach(image => { image.removeEventListener('load', onImageSettled); image.removeEventListener('error', onImageSettled); });
      };
    }
    async function navigate(page, navigation = {}) {
      if (!state.open) return;
      const next = chapterIndex(page, state.book.page_count);
      const at = position(navigation.position || 0); const fragment = String(navigation.fragment || '').slice(0, 512);
      if (state.loaded && next === state.page && state.source) {
        state.completed = Boolean(navigation.completed ?? (state.completed && next === state.book.page_count - 1 && at >= .999));
        if (state.recovery) Object.assign(state.recovery, { position: at, fragment, active: true });
        closePanel(); restore(at, fragment, !navigation.fromRoute); return;
      }
      if (state.loaded) queueSave({ immediate: true });
      state.controller?.abort(); state.detachFrame?.(); state.detachFrame = null;
      const token = ++state.token; const book = state.book; const controller = new AbortController(); state.controller = controller;
      state.page = next; state.position = at; state.fragment = fragment; state.completed = Boolean(navigation.completed); state.loaded = false; state.source = null; state.restoring = false;
      closePanel(); renderToc(); updateLocation(); showMessage('正在打开章节…'); frame.hidden = true; frame.onload = null; frame.srcdoc = '';
      const entry = book.epub_chapters?.find(item => item.index === next); $('[data-role="chapter-title"]').textContent = entry?.title || `第 ${next + 1} 章`;
      try {
        const revision = /^[a-f0-9]{24}$/.test(book.page_revision || '') ? '?v=' + book.page_revision : '';
        const result = await options.api('/api/books/' + encodeURIComponent(book.id) + '/epub/chapters/' + next + revision, { signal: controller.signal });
        if (!state.open || token !== state.token || state.book !== book) return;
        if (typeof result?.html !== 'string') throw new Error('章节内容无法读取。');
        if (book.page_revision && result.source_version && String(result.source_version).slice(0, 24) !== book.page_revision) throw new Error('作品已更新，请重新打开。');
        state.source = result; $('[data-role="chapter-title"]').textContent = result.title || entry?.title || `第 ${next + 1} 章`;
        frame.onload = () => {
          if (!state.open || token !== state.token || state.book !== book) return;
          // Ignore a delayed load from the blank/previous document.
          try { if (frame.contentDocument.querySelector('meta[name="gravelle-epub-document"]')?.content !== String(token)) return; } catch (_) { return; }
          state.loaded = true; frame.hidden = false; showMessage(''); bindFrame(token, at, fragment); applyPreferences(); restore(at, fragment, !navigation.fromRoute); updateLocation();
        };
        frame.srcdoc = chapterDocument(result.html, result.title || book.title, state.prefs, token);
      } catch (error) {
        if (!state.open || token !== state.token || error?.name === 'AbortError') return;
        showMessage(error?.message || '章节打开失败。', true); updateLocation();
      }
    }
    function finish() {
      if (!state.loaded) return; capture(); state.completed = true; state.position = 1;
      const element = scroller(); if (element) element.scrollTop = element.scrollHeight;
      queueSave({ completed: true, immediate: true }); state.position = 1; updateLocation(); routeNow();
    }
    function changeSetting(key, delta) {
      if (state.recovery) state.recovery.active = false;
      const at = capture().position; const next = { ...state.prefs, [key]: Math.round((state.prefs[key] + delta) * 10) / 10 }; state.prefs = preferences(next);
      applyPreferences(); rememberPreferences(); if (state.loaded) restore(at);
    }
    function handleKey(event) {
      if (!state.open || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (state.panel) closePanel(true); else close(); return; }
      if (event.target?.closest?.('input,textarea,select,[contenteditable="true"]')) return;
      if (event.key === 'ArrowLeft' && state.loaded && state.page > 0) { event.preventDefault(); navigate(state.page - 1); }
      else if (event.key === 'ArrowRight' && state.loaded) { event.preventDefault(); state.page < state.book.page_count - 1 ? navigate(state.page + 1) : finish(); }
    }
    async function open(book, navigation = {}) {
      if (state.open) await close({ notify: false });
      if (!book || book.format !== 'epub' || !Number.isInteger(book.page_count) || book.page_count < 1) throw new TypeError('无法打开这本 EPUB 作品。');
      const nextAccountKey = settingKey(); if (accountKey !== undefined && accountKey !== nextAccountKey) discardAccountSaves(); accountKey = nextAccountKey;
      state.open = true; state.book = book; state.page = chapterIndex(navigation.page ?? book.progress?.page ?? 0, book.page_count); state.position = position(navigation.position ?? book.progress?.position ?? 0);
      state.completed = false; state.loaded = false; readPreferences(navigation.theme || 'dark');
      state.savedOverflow = doc.body.style.overflow; doc.body.style.overflow = 'hidden'; $('[data-role="title"]').textContent = book.title || 'EPUB';
      $('[data-action="bookmark"]').hidden = typeof options.onBookmark !== 'function'; applyPreferences(); renderToc(); status('进度已保存');
      if (failedSaves.has(String(book.id))) status('进度未保存，点击重试', true);
      if (!dialog.open) dialog.showModal(); $('[data-action="close"]').focus({ preventScroll: true });
      const completed = Boolean(book.progress?.completed && book.progress.page === state.page && state.page === book.page_count - 1 && state.position >= .999);
      return navigate(state.page, { position: state.position, fragment: navigation.fragment || '', fromRoute: navigation.fromRoute, completed });
    }
    function close({ notify = true, save = true } = {}) {
      if (!state.open) {
        if (!notify || !save) ++state.token;
        if (!save) { discardAccountSaves(); return Promise.resolve(); }
        return Promise.allSettled([...saveChains.values()]).then(() => {});
      }
      const book = state.book; const at = capture(); const saved = save ? queueSave({ immediate: true, keepalive: true }) : Promise.resolve();
      state.open = false; ++state.token; state.controller?.abort(); state.controller = null; state.detachFrame?.(); state.detachFrame = null; state.loaded = false;
      const closingToken = state.token;
      clear(state.saveTimer); clear(state.routeTimer); closePanel(); frame.onload = null; frame.srcdoc = ''; state.source = null;
      if (!save) discardAccountSaves();
      if (dialog.open) dialog.close(); doc.body.style.overflow = state.savedOverflow;
      return saved.finally(() => { if (notify && !state.open && state.token === closingToken) options.onClose?.(book, at); });
    }
    function onClick(event) {
      const chapter = event.target.closest?.('[data-page]'); if (chapter && $('[data-role="toc"]').contains(chapter)) { navigate(Number(chapter.dataset.page), { fragment: chapter.dataset.fragment }); return; }
      const step = event.target.closest?.('[data-setting]'); if (step) { changeSetting(step.dataset.setting, Number(step.dataset.step)); return; }
      const control = event.target.closest?.('[data-action]'); if (!control || control.disabled) return;
      switch (control.dataset.action) {
        case 'close': close(); break;
        case 'toc': togglePanel('toc'); break;
        case 'settings': togglePanel('settings'); break;
        case 'dismiss': closePanel(true); break;
        case 'theme': { if (state.recovery) state.recovery.active = false; const at = capture().position; state.prefs.theme = state.prefs.theme === 'dark' ? 'light' : 'dark'; applyPreferences(); rememberPreferences(); if (state.loaded) restore(at); break; }
        case 'previous': if (state.page > 0) navigate(state.page - 1); break;
        case 'next': state.page < state.book.page_count - 1 ? navigate(state.page + 1) : finish(); break;
        case 'retry': navigate(state.page, { position: state.position, fragment: state.fragment }); break;
        case 'save': flush(true); break;
        case 'bookmark': if (state.loaded) options.onBookmark?.(state.book, capture()); break;
      }
    }
    function onResize() { if (state.open && state.loaded) restore(state.position); }
    function onHide() { if (state.open && (doc.visibilityState === 'hidden' || doc.hidden)) queueSave({ immediate: true, keepalive: true }); }
    function onPageHide() { if (state.open) queueSave({ immediate: true, keepalive: true }); }
    dialog.addEventListener('click', onClick); dialog.addEventListener('keydown', handleKey);
    const onCancel = event => { event.preventDefault(); if (state.panel) closePanel(true); else close(); };
    dialog.addEventListener('cancel', onCancel); win.addEventListener('resize', onResize); win.addEventListener('pagehide', onPageHide); doc.addEventListener('visibilitychange', onHide);
    return { open, navigate, close, flush, location: () => capture(), isOpen: () => state.open, element: dialog,
      destroy() { close({ notify: false }); dialog.removeEventListener('click', onClick); dialog.removeEventListener('keydown', handleKey); dialog.removeEventListener('cancel', onCancel); win.removeEventListener('resize', onResize); win.removeEventListener('pagehide', onPageHide); doc.removeEventListener('visibilitychange', onHide); dialog.remove(); },
    };
  }
  function icon(name) {
    const paths = { back: '<path d="m14 5-7 7 7 7"/>', next: '<path d="m10 5 7 7-7 7"/>', close: '<path d="m6 6 12 12M6 18 18 6"/>', contents: '<path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/>', theme: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>', bookmark: '<path d="M6 3h12v18l-6-4-6 4Z"/>' };
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || ''}</svg>`;
  }
  return { create, preferences, position, chapterIndex, scrollPosition, linkTarget, chapterDocument };
});

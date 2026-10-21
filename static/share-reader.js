/* Anonymous transport, shared reader model and appearance. No account state is loaded. */
((root, factory) => {
  const api = factory(typeof module === 'object' && module.exports ? require('./reader-model.js') : root.SumiReaderModel);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else { root.GravelleShareReader = api; if (root.document) api.create({ window: root, document: root.document }).start(); }
})(typeof window === 'undefined' ? globalThis : window, Model => {
  'use strict';
  const messages = Object.freeze({ expired: '分享已到期，请联系分享者。', closed: '分享已关闭。', exhausted: '分享的访问次数已用完。', unavailable: '分享或作品已不可用。', invalid: '分享链接不正确。' });
  const choices = { mode: [['single', '单页'], ['double', '双页'], ['webtoon', '连续滚动']], direction: [['ltr', '左 → 右'], ['rtl', '右 → 左'], ['ttb', '上下翻页']], fit: [['height', '适应屏幕'], ['width', '适应宽度']] };
  const labels = { mode: '阅读方式', direction: '阅读方向', fit: '图片适配' };
  const MEDIA_LIMIT = 3, MEMORY_LIMIT = 96 * 1024 * 1024;
  function tokenFromPath(path) { return /^\/share\/([A-Za-z0-9_-]{43})\/?$/.exec(String(path || ''))?.[1] || null; }
  function validManifest(value) { return Boolean(value?.manifest && typeof value.manifest.title === 'string' && Number.isInteger(value.manifest.page_count) && value.manifest.page_count > 0 && value.manifest.page_count <= 100000 && typeof value.key === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value.key) && ['folder', 'zip', 'cbz', 'archive', 'pdf', 'epub'].includes(value.manifest.format)); }
  function pageNumber(value, total) { const number = Number(value); return Number.isInteger(number) ? Math.min(Math.max(number, 0), Math.max(0, total - 1)) : 0; }
  function readingPrefs(saved, book = {}) {
    const value = saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
    const defaults = { mode: book.reading_mode || 'single', direction: book.reading_direction || 'ltr', fit: 'height', theme: 'dark' };
    const result = {};
    for (const name of ['mode', 'direction', 'fit', 'theme']) {
      const allowed = name === 'theme' ? ['dark', 'light'] : choices[name].map(row => row[0]);
      result[name] = allowed.includes(value[name]) ? value[name] : allowed.includes(defaults[name]) ? defaults[name] : allowed[0];
    }
    result.coverAlone = typeof value.coverAlone === 'boolean' ? value.coverAlone : true;
    result.offset = typeof value.offset === 'boolean' ? value.offset : false;
    return result;
  }
  function create({ window: win = window, document: doc = document, fetch: request, crypto: decryptor } = {}) {
    const fetcher = request || win.fetch.bind(win), decrypt = decryptor || win.GravelleShareCrypto;
    const token = tokenFromPath(win.location.pathname), api = '/api/share/' + token, $ = id => doc.getElementById(id);
    const gate = $('share-gate'), reader = $('share-reader'), form = $('share-unlock-form'), input = $('share-password'), unlock = $('share-unlock'), gateRetry = $('share-gate-retry');
    const gateTitle = $('share-access-title'), gateMessage = $('share-gate-message'), passwordError = $('share-password-error');
    const host = $('share-image-host'), message = $('share-page-message'), messageText = $('share-page-message-text'), pageRetry = $('share-page-retry');
    const pageInput = $('share-page-number'), pageTotal = $('share-page-total'), previous = $('share-previous'), next = $('share-next'), viewport = $('share-viewport');
    const paged = $('share-paged-view'), scrollView = $('share-scroll-view'), slider = $('share-page-slider'), popup = $('share-choice-popover'), menu = $('share-menu'), help = $('share-help-panel');
    let key = null, manifest = null, active = false, sequence = 0, page = 0, heartbeat = null, expiryTimer = null, chromeTimer = null, scrollTimer = null, resizeTimer = null, checking = null, stopped = false, isBusy = false;
    let lifecycle = 0, exitRequested = false, logoutPending = null, unlockPending = null, prefs = readingPrefs(), choice = null, popupOptions = [], touch = null, suppressClickUntil = 0, scrollLoading = false;
    let spreads = null, lastMode = 'single', offsets = [0], rowWidth = 0, scrollRestoring = null;
    const media = new Map(), rows = new Map(), sizes = new Map(), controllers = new Set(), failedPages = new Set();
    const wheel = Model.createWheelPager();
    const storageKey = 'gravelle-share-page:' + token, prefsKey = 'gravelle-share-prefs:' + token, exitKey = 'gravelle-share-exit:' + token;
    const element = (name, className, text) => { const node = doc.createElement(name); node.className = className || ''; if (text !== undefined) node.textContent = text; return node; };
    const style = (node, name, value) => node?.style?.setProperty(name, value);
    const listening = (node, type, handler, options) => node?.addEventListener(type, handler, options);
    function savedExit() { try { return win.sessionStorage.getItem(exitKey) === '1'; } catch (_) { return false; } }
    function saveExit(value) { try { if (value) win.sessionStorage.setItem(exitKey, '1'); else win.sessionStorage.removeItem(exitKey); } catch (_) {} }
    function savePosition() { try { win.sessionStorage.setItem(storageKey, String(page)); } catch (_) {} }
    function readPosition() { try { return pageNumber(win.sessionStorage.getItem(storageKey), Number.MAX_SAFE_INTEGER); } catch (_) { return 0; } }
    function readPreferences() { try { return readingPrefs(JSON.parse(win.sessionStorage.getItem(prefsKey)), manifest); } catch (_) { return readingPrefs(null, manifest); } }
    const mode = () => Model.effectiveMode(prefs.mode, viewport.clientWidth || win.innerWidth || 1200, viewport.clientHeight || win.innerHeight || 800);
    const direction = () => Model.effectiveDirection(prefs.mode, prefs.direction);
    const vertical = () => prefs.mode === 'single' && direction() === 'ttb';
    const validWork = (version, secret) => version === sequence && active && !stopped && key === secret;
    function evict(index) {
      const entry = media.get(index); if (!entry) return;
      media.delete(index); entry.controller?.abort(); controllers.delete(entry.controller);
      entry.image.onload = entry.image.onerror = null; entry.image.removeAttribute('src');
      if (entry.url) win.URL.revokeObjectURL(entry.url);
      const row = rows.get(index); if (row) { row.replaceChildren(element('span', 'scroll-placeholder', '第 ' + (index + 1) + ' 页')); row.className = 'scroll-page'; }
    }
    function clearMedia() {
      sequence++; controllers.forEach(controller => controller.abort()); controllers.clear();
      [...media.keys()].forEach(evict); rows.clear(); failedPages.clear(); host.replaceChildren(); scrollView.replaceChildren(); scrollLoading = false; scrollRestoring = null;
      win.clearTimeout(scrollTimer); scrollTimer = null;
    }
    function closeChoice(focus = false) {
      const selected = choice; choice = null; popup.hidden = true; popupOptions = [];
      if (selected) { const trigger = $('share-' + selected + '-trigger'); trigger.setAttribute('aria-expanded', 'false'); if (focus && active) trigger.focus(); }
    }
    function clearSession() {
      checking?.controller.abort(); checking = null;
      key = manifest = null; active = false; clearMedia(); sizes.clear(); spreads = null;
      for (const timer of [expiryTimer, chromeTimer, scrollTimer, resizeTimer]) win.clearTimeout(timer);
      expiryTimer = chromeTimer = scrollTimer = resizeTimer = null; win.clearInterval(heartbeat); heartbeat = null;
      closeChoice(); menu.hidden = help.hidden = true; touch = null; wheel.reset();
      if (doc.fullscreenElement === reader) Promise.resolve(doc.exitFullscreen?.()).catch(() => {});
    }
    function gateState(text, { password = false, retry = false, title = '分享已结束' } = {}) {
      clearSession(); reader.hidden = true; gate.hidden = false; gateTitle.textContent = title; gateMessage.textContent = text; gateMessage.hidden = !text;
      form.hidden = !password; gateRetry.hidden = !retry; input.value = ''; passwordError.hidden = true; input.disabled = unlock.disabled = exitRequested; isBusy = false; doc.title = '写真分享 · Gravelle';
    }
    function end(status, text) { stopped = true; gateState(messages[status] || text || messages.unavailable); }
    function locked(text = '') { gateState(text, { password: true, title: '输入访问密码' }); input.focus(); }
    async function json(path, options = {}) {
      const response = await fetcher(path, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', ...options });
      let data; try { data = await response.json(); } catch (_) { data = {}; }
      if (!response.ok) { const problem = new Error(data.error || data.message || '暂时无法连接，请稍后重试。'); problem.status = response.status; problem.code = data.status || data.reason || data.code; throw problem; } return data;
    }
    function terminal(problem) { if ([403, 404, 410].includes(problem.status)) { end(problem.code, problem.message); return true; } if (problem.status === 401) { locked('访问凭证已失效，请重新输入密码。'); return true; } return false; }
    function scheduleExpiry(value) {
      win.clearTimeout(expiryTimer); const raw = value.session_expires_at; if (!raw) return;
      const when = typeof raw === 'number' ? raw * (raw < 1e12 ? 1000 : 1) : Date.parse(raw); if (!Number.isFinite(when)) return;
      const wait = when - Date.now(); if (wait <= 0) { locked('本次访问已结束，请重新输入密码。'); return; }
      expiryTimer = win.setTimeout(() => { if (active) locked('本次访问已结束，请重新输入密码。'); }, Math.min(wait + 50, 2147483647));
    }
    async function accept(value, restore) {
      if (!validManifest(value)) throw new Error('分享内容格式不正确。'); clearSession(); key = value.key; manifest = value.manifest; prefs = readPreferences(); active = true; stopped = false;
      page = pageNumber(restore, manifest.page_count); gate.hidden = true; reader.hidden = false; $('share-title').textContent = manifest.title; doc.title = manifest.title + ' · Gravelle';
      pageTotal.textContent = '/ ' + manifest.page_count; pageInput.max = manifest.page_count; slider.max = manifest.page_count - 1;
      scheduleExpiry(value); if (!active) return;
      heartbeat = win.setInterval(() => { if (!doc.hidden) checkSession(); }, 15000); setChrome(true); viewport.focus({ preventScroll: true }); await render(page);
    }
    async function start() {
      if (!token) { end('invalid'); return; } if (exitRequested || savedExit()) return exit();
      const current = ++lifecycle; stopped = false; gateRetry.hidden = true; gateRetry.textContent = '重试'; gateTitle.textContent = '打开分享'; gateMessage.textContent = '正在检查分享…'; gateMessage.hidden = false; form.hidden = true;
      try { const value = await json(api + '/manifest'); if (stopped || current !== lifecycle) return; await accept(value, readPosition()); }
      catch (problem) {
        if (stopped || current !== lifecycle) return;
        if (problem.status === 401) { try { const info = await json(api); if (stopped || current !== lifecycle) return; if (info.status !== 'active') end(info.status); else locked(); } catch (error) { if (stopped || current !== lifecycle) return; if (!terminal(error)) gateState(error.message, { retry: true, title: '暂时无法打开分享' }); } }
        else if (!terminal(problem)) gateState(problem.message, { retry: true, title: '暂时无法打开分享' });
      }
    }
    async function checkSession() {
      if (!active || checking || stopped) return; const checkedKey = key, current = lifecycle, check = { controller: new win.AbortController() }; checking = check;
      try { const value = await json(api + '/manifest', { signal: check.controller.signal }); if (!active || stopped || key !== checkedKey || current !== lifecycle) return;
        if (!validManifest(value) || value.key !== key || value.manifest.page_count !== manifest.page_count) { end('unavailable'); return; } scheduleExpiry(value);
      } catch (problem) { if (!active || stopped || key !== checkedKey || current !== lifecycle) return; if (!terminal(problem)) gateState('暂时无法验证分享，请重试后继续阅读。', { retry: true, title: '连接中断' }); }
      finally { if (checking === check) checking = null; }
    }
    async function exit() {
      if (logoutPending) return logoutPending; exitRequested = true; stopped = true; saveExit(true); const current = ++lifecycle; gateState('正在退出阅读…', { title: '退出阅读' });
      const task = (async () => { try {
        if (unlockPending) await unlockPending.catch(() => {});
        const result = await json(api + '/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        if (result?.logged_out !== true) throw new Error('logout not confirmed'); if (current !== lifecycle) return;
        saveExit(false); exitRequested = false; stopped = false; gateRetry.textContent = '重试'; locked();
      } catch (_) { if (current !== lifecycle) return; gateState('暂时无法完成退出，请重试。', { retry: true, title: '退出未完成' }); gateRetry.textContent = '重试退出'; } })();
      logoutPending = task; try { await task; } finally { if (logoutPending === task) logoutPending = null; }
    }
    function scheduleChrome() {
      win.clearTimeout(chromeTimer); if (!active || reader.dataset.controls !== 'visible') return;
      chromeTimer = win.setTimeout(() => { if (choice || !menu.hidden || !help.hidden || /INPUT|SELECT/.test(doc.activeElement?.tagName || '')) scheduleChrome(); else setChrome(false); }, 3200);
    }
    function setChrome(visible) {
      win.clearTimeout(chromeTimer); reader.dataset.controls = visible ? 'visible' : 'hidden';
      for (const node of reader.querySelectorAll?.('.reader-toolbar,.reader-footer,.reader-floating') || []) { if (!visible && node.contains(doc.activeElement)) viewport.focus({ preventScroll: true }); node.inert = !visible; }
      if (!visible) { closeChoice(); menu.hidden = true; $('share-menu-toggle').setAttribute('aria-expanded', 'false'); }
      else scheduleChrome();
    }
    function groups() {
      if (!spreads) spreads = Model.buildSpreads(Array.from({ length: manifest.page_count }, (_, index) => sizes.get(index) || {}), { coverAlone: prefs.coverAlone, offset: prefs.offset });
      return spreads;
    }
    function visiblePages() { return mode() === 'double' ? groups()[Model.spreadForPage(groups(), page)] || [page] : [page]; }
    function nextIndex(delta) {
      if (mode() !== 'double') return pageNumber(page + delta, manifest.page_count);
      const list = groups(), at = Model.spreadForPage(list, page), target = Math.min(Math.max(at + delta, 0), list.length - 1); return list[target][0];
    }
    function controls() {
      const effective = mode(), flow = direction();
      reader.dataset.mode = effective; reader.dataset.fit = prefs.fit; reader.dataset.theme = prefs.theme; reader.dataset.direction = flow; reader.dataset.paging = vertical() ? 'vertical' : 'horizontal';
      style(reader, '--viewport-height', (viewport.clientHeight || win.innerHeight || 800) + 'px');
      pageInput.value = page + 1; pageInput.disabled = !active; slider.value = page; slider.disabled = !active; slider.dir = flow === 'rtl' ? 'rtl' : 'ltr';
      const navigation = previous.closest?.('.page-navigation'); if (navigation) navigation.dir = flow === 'rtl' ? 'rtl' : 'ltr';
      style(slider, '--page-progress', (manifest?.page_count > 1 ? page / (manifest.page_count - 1) * 100 : 0) + '%');
      pageTotal.textContent = '/ ' + (manifest?.page_count || 0); $('share-floating-page').textContent = (page + 1) + ' / ' + (manifest?.page_count || 0);
      previous.disabled = !active || nextIndex(-1) === page; next.disabled = !active || (mode() === 'double' ? Model.spreadForPage(groups(), page) === groups().length - 1 : page === manifest.page_count - 1);
      const previousGlyph = vertical() ? '↑' : flow === 'rtl' ? '→' : '←', nextGlyph = vertical() ? '↓' : flow === 'rtl' ? '←' : '→';
      for (const [button, glyph] of [[previous, previousGlyph], [next, nextGlyph]]) {
        const path = button.querySelector?.('path');
        if (path) path.setAttribute('d', glyph === '↑' ? 'm5 15 7-7 7 7' : glyph === '↓' ? 'm5 9 7 7 7-7' : glyph === '←' ? 'm15 5-7 7 7 7' : 'm9 5 7 7-7 7');
        else button.textContent = glyph;
      }
      $('share-tap-previous').setAttribute('aria-label', flow === 'rtl' ? '下一页' : '上一页'); $('share-tap-next').setAttribute('aria-label', flow === 'rtl' ? '上一页' : '下一页');
      for (const name of Object.keys(choices)) {
        const value = name === 'direction' ? flow : prefs[name]; const text = name === 'direction' && prefs.mode === 'webtoon' ? '从上到下' : choices[name].find(row => row[0] === value)?.[1];
        $('share-' + name + '-value').textContent = text; const trigger = $('share-' + name + '-trigger'); trigger.disabled = name === 'direction' && prefs.mode === 'webtoon'; trigger.setAttribute('aria-label', labels[name] + '：' + text);
      }
      $('share-cover-alone').checked = prefs.coverAlone; $('share-spread-offset').checked = prefs.offset;
      $('share-theme').setAttribute('aria-label', prefs.theme === 'light' ? '切换为深色阅读背景' : '切换为浅色阅读背景');
      $('share-mode-hint').textContent = effective === 'webtoon' ? '从上到下滚动 · ? 帮助' : vertical() ? '上下翻页 · ↑ ↓ / 滚轮翻页 · ? 帮助' : prefs.mode === 'double' && effective === 'single' ? '窄屏自动单页 · 横屏可使用双页' : '↑ ↓ / ← → 翻页 · ? 帮助';
      $('share-help-arrows').textContent = vertical() ? '上下翻页时使用 ↑ / ↓' : '上一页 / 下一页，跟随阅读方向';
      $('share-help-description').textContent = effective === 'webtoon' ? '上下连续滚动阅读，点击画面中央显示阅读菜单。' : vertical() ? '点击画面上方返回上一页，下方翻到下一页；中央显示菜单。长图先在当前页内滚动，到达边缘后再次操作才翻页。' : '点击中央显示阅读菜单，两侧点击或左右滑动翻页。双页时从右到左会交换图片左右位置。';
    }
    async function loadImage(index, target, version, ready) {
      if (!active || stopped || media.has(index)) return;
      const secret = key, controller = new win.AbortController(), image = doc.createElement('img');
      const entry = { image, url: null, controller, loaded: false, bytes: 24 * 1024 * 1024 }; media.set(index, entry); controllers.add(controller);
      const current = () => validWork(version, secret) && media.get(index) === entry;
      image.alt = manifest.title + ' · 第 ' + (index + 1) + ' 页'; image.draggable = false; image.decoding = 'async';
      try {
        let endpoint = api + '/pages/' + index;
        if (manifest.format === 'pdf') endpoint += '?width=' + Math.min(2400, Model.pdfPageWidth({ width: viewport.clientWidth || win.innerWidth, height: viewport.clientHeight || win.innerHeight, pixelRatio: Math.min(2, win.devicePixelRatio || 1), pages: mode() === 'double' ? 2 : 1, fit: prefs.fit, scroll: mode() === 'webtoon', size: sizes.get(index) }));
        const response = await fetcher(endpoint, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', signal: controller.signal });
        if (!current()) return;
        if (!response.ok) { let data = {}; try { data = await response.json(); } catch (_) {} const problem = new Error(data.error || data.message || '图片加载失败。'); problem.status = response.status; problem.code = data.status || data.reason || data.code; throw problem; }
        const packet = await response.arrayBuffer(); if (!current()) return;
        if (!decrypt?.decrypt) throw new Error('图片解密组件未能加载，请刷新页面。');
        const result = await decrypt.decrypt(packet, secret, 'gravelle-share-v1:' + token + ':' + index); if (!current()) return;
        if (!result?.data || !['image/jpeg', 'image/png', 'image/webp', 'image/avif', 'image/gif', 'image/bmp'].includes(result.mime)) throw new Error('图片数据格式不正确。');
        entry.url = win.URL.createObjectURL(new win.Blob([result.data], { type: result.mime }));
        image.onload = () => {
          if (!current()) return; entry.loaded = true; entry.bytes = Math.max(1, image.naturalWidth || 1) * Math.max(1, image.naturalHeight || 1) * 4;
          if (image.naturalWidth && image.naturalHeight) { sizes.set(index, { width: image.naturalWidth, height: image.naturalHeight }); spreads = null; }
          ready?.(entry); if (!current()) return; if (index === page || mode() !== 'webtoon') savePosition();
        };
        image.onerror = () => { if (!current()) return; evict(index); pageError(index, '图片暂时无法显示。'); };
        target.replaceChildren(image); image.src = entry.url;
      } catch (problem) {
        if (!current() || problem.name === 'AbortError') return;
        evict(index); if (!terminal(problem)) pageError(index, problem.message || '图片加载失败，请重试。');
      } finally { controllers.delete(controller); }
    }
    function showPageError(text) { message.hidden = false; messageText.textContent = text; pageRetry.hidden = false; }
    function pageError(index, text) {
      failedPages.add(index);
      if (mode() !== 'webtoon') { showPageError(text); return; }
      const row = rows.get(index); if (!row) return;
      const retry = element('button', 'button button-secondary', '重试第 ' + (index + 1) + ' 页'); retry.type = 'button';
      listening(retry, 'click', () => render(index)); row.replaceChildren(retry);
    }
    function calculateOffsets() {
      rowWidth = viewport.clientWidth || win.innerWidth || 1000; offsets = [0];
      for (let index = 0; index < manifest.page_count; index++) { const size = sizes.get(index); offsets.push(offsets[index] + Math.max(80, rowWidth * (size ? size.height / size.width : 1.4))); }
    }
    function scrollIndex(top) { let low = 0, high = manifest.page_count; while (low + 1 < high) { const middle = (low + high) >> 1; if (offsets[middle] <= top) low = middle; else high = middle; } return low; }
    function trimScroll(wanted) { for (const index of [...media.keys()]) if (!wanted.includes(index)) evict(index); }
    async function pumpScroll(version) {
      if (scrollLoading || !validWork(version, key) || mode() !== 'webtoon') return;
      const currentEntry = media.get(page), wanted = currentEntry?.loaded ? [page, page + 1, page - 1].filter(index => index >= 0 && index < manifest.page_count) : [page];
      trimScroll(wanted);
      const bytes = [...media.values()].reduce((sum, entry) => sum + entry.bytes, 0);
      const index = wanted.find(index => !media.has(index) && !failedPages.has(index) && rows.has(index) && (index === page || media.size < MEDIA_LIMIT && bytes + (sizes.has(index) ? sizes.get(index).width * sizes.get(index).height * 4 : 24 * 1024 * 1024) <= MEMORY_LIMIT));
      if (index === undefined) return; scrollLoading = true;
      try { await loadImage(index, rows.get(index), version, entry => {
        if (!validWork(version, key) || !rows.has(index)) return;
        const anchor = scrollRestoring?.index ?? scrollIndex(Math.max(0, viewport.scrollTop || 0)), within = scrollRestoring?.within ?? (viewport.scrollTop || 0) - offsets[anchor];
        calculateOffsets(); const anchorTop = offsets[anchor] + within; rows.get(index).className = 'scroll-page loaded';
        if ([...media.values()].reduce((sum, value) => sum + value.bytes, 0) > MEMORY_LIMIT) trimScroll([page]);
        syncScroll(false); viewport.scrollTop = anchorTop; pumpScroll(version);
      }); } finally { if (version === sequence) scrollLoading = false; }
      // Loading neighbours waits for the current image's decoded dimensions.
      if (validWork(version, key) && media.get(page)?.loaded) pumpScroll(version);
    }
    function syncScroll(track = true) {
      if (!active || mode() !== 'webtoon') return;
      if (track) { page = scrollRestoring?.index ?? scrollIndex(Math.max(0, viewport.scrollTop || 0) + Math.min(120, (viewport.clientHeight || 800) * .2)); savePosition(); controls(); }
      const first = Math.max(0, page - 2), last = Math.min(manifest.page_count - 1, page + 3);
      for (const [index] of rows) if (index < first || index > last) { evict(index); rows.delete(index); }
      const top = element('div', 'share-scroll-spacer'); style(top, 'height', offsets[first] + 'px'); top.setAttribute('aria-hidden', 'true');
      const bottom = element('div', 'share-scroll-spacer'); style(bottom, 'height', (offsets[manifest.page_count] - offsets[last + 1]) + 'px'); bottom.setAttribute('aria-hidden', 'true');
      const nodes = [top];
      for (let index = first; index <= last; index++) {
        let row = rows.get(index); if (!row) { row = element('div', 'scroll-page'); row.dataset.page = index; row.append(element('span', 'scroll-placeholder', '第 ' + (index + 1) + ' 页')); rows.set(index, row); }
        style(row, 'height', (offsets[index + 1] - offsets[index]) + 'px'); style(row, 'min-height', '0'); style(row, 'aspect-ratio', 'auto'); nodes.push(row);
      }
      nodes.push(bottom); scrollView.replaceChildren(...nodes);
      message.hidden = true;
      pumpScroll(sequence);
    }
    async function render(index, { preserveTop = false } = {}) {
      if (!active || !manifest || stopped) return; page = pageNumber(index, manifest.page_count); clearMedia(); const version = sequence;
      controls(); lastMode = mode(); wheel.reset(); message.hidden = false; messageText.textContent = '正在加载图片…'; pageRetry.hidden = true;
      const scrolling = mode() === 'webtoon'; paged.hidden = scrolling; scrollView.hidden = !scrolling;
      if (scrolling) { scrollRestoring = { index: page, within: 0 }; calculateOffsets(); syncScroll(false); viewport.scrollTop = offsets[page]; await pumpScroll(version); return; }
      if (!preserveTop) viewport.scrollTop = 0;
      const pages = Model.visualPages(visiblePages(), direction()); host.dataset.pages = pages.length; const targets = pages.map(() => element('span', 'share-paged-slot'));
      // Single pages remain direct children for exactly the same sizing rules.
      await Promise.all(pages.map((index, offset) => loadImage(index, targets[offset], version, () => {
        if (version !== sequence) return;
        const desired = Model.visualPages(visiblePages(), direction());
        if (desired.join(',') !== pages.join(',')) { render(page, { preserveTop: true }); return; }
        if (pages.every(index => media.get(index)?.loaded)) message.hidden = true;
      })));
      if (!validWork(version, key)) return;
      host.replaceChildren(...targets.flatMap(target => Array.from(target.children)));
    }
    async function move(delta) { if (!active || !help.hidden || choice) return; await render(nextIndex(delta)); }
    function change(name, value) {
      if (!active) return; const allowed = name === 'theme' ? ['dark', 'light'] : choices[name]?.map(option => option[0]);
      if (allowed && !allowed.includes(value)) return; if (name === 'direction' && (prefs.mode === 'webtoon' || prefs.mode === 'double' && value === 'ttb')) return;
      prefs[name] = value; spreads = null; try { win.sessionStorage.setItem(prefsKey, JSON.stringify(prefs)); } catch (_) {}
      closeChoice(); setChrome(true); if (name === 'theme') controls(); else render(page);
    }
    function positionChoice() {
      if (!choice) return; const rect = $('share-' + choice + '-trigger').getBoundingClientRect?.(); if (!rect) return;
      const width = win.innerWidth || 1200, height = win.innerHeight || 800;
      style(popup, 'left', Math.max(12, Math.min(rect.right - (popup.offsetWidth || 190), width - (popup.offsetWidth || 190) - 12)) + 'px');
      style(popup, 'top', Math.max(12, Math.min(rect.bottom + 8, height - (popup.offsetHeight || 180) - 12)) + 'px'); style(popup, 'max-height', Math.max(80, height - 24) + 'px');
    }
    function openChoice(name, edge = '') {
      if (!active || name === 'direction' && prefs.mode === 'webtoon') return;
      if (choice === name && !edge) { closeChoice(true); return; } closeChoice(); choice = name; setChrome(true);
      const trigger = $('share-' + name + '-trigger'); trigger.setAttribute('aria-expanded', 'true'); popup.setAttribute('aria-labelledby', trigger.id || 'share-' + name + '-trigger');
      const options = choices[name].filter(option => !(name === 'direction' && option[0] === 'ttb' && prefs.mode === 'double'));
      popupOptions = options.map(([value, text]) => {
        const button = element('button', 'reader-choice-option'); button.type = 'button'; button.tabIndex = -1; button.dataset.value = value; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(value === (name === 'direction' ? direction() : prefs[name])));
        button.append(element('span', '', text), element('span', 'share-choice-check', '✓')); listening(button, 'click', () => { closeChoice(true); change(name, value); }); return button;
      });
      popup.replaceChildren(element('span', 'reader-choice-heading', labels[name]), ...popupOptions); popup.hidden = false; positionChoice();
      (edge === 'End' ? popupOptions.at(-1) : popupOptions.find(button => button.getAttribute('aria-selected') === 'true') || popupOptions[0])?.focus();
    }
    async function fullscreen() { try { if (doc.fullscreenElement) await doc.exitFullscreen?.(); else await reader.requestFullscreen?.(); } catch (_) {} setChrome(true); }
    function toggleHelp() { closeChoice(); help.hidden = !help.hidden; $('share-help').setAttribute('aria-expanded', String(!help.hidden)); setChrome(true); if (!help.hidden) $('share-help-close').focus(); }
    function scrollBy(amount) { if (viewport.scrollBy) viewport.scrollBy({ top: amount, behavior: 'auto' }); else viewport.scrollTop = Math.max(0, (viewport.scrollTop || 0) + amount); }
    function verticalMove(delta, event) { if (Model.canScrollPage(delta, viewport)) { scrollBy(delta * Math.max(80, (viewport.clientHeight || 800) * .8)); wheel.reset(); } else if (!event?.repeat) move(delta); }
    listening(form, 'submit', async event => {
      event.preventDefault(); if (isBusy || stopped || !token || !form.reportValidity()) return; isBusy = true; unlock.disabled = true; passwordError.hidden = true; const password = input.value; input.value = '';
      const current = ++lifecycle, pending = json(api + '/unlock', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) }); unlockPending = pending;
      try { const value = await pending; if (stopped || current !== lifecycle) return; await accept(value, readPosition()); }
      catch (problem) { if (stopped || current !== lifecycle) return; if ([404, 410].includes(problem.status)) end(problem.code, problem.message); else { passwordError.textContent = problem.status === 429 ? '尝试次数较多，请稍后再试。' : problem.message; passwordError.hidden = false; input.focus(); } }
      finally { if (unlockPending === pending) unlockPending = null; if (current === lifecycle) { isBusy = false; unlock.disabled = false; } }
    });
    listening(gateRetry, 'click', start); listening(pageRetry, 'click', () => render(page)); listening(previous, 'click', () => move(-1)); listening(next, 'click', () => move(1)); listening($('share-exit'), 'click', exit);
    listening($('share-page-form'), 'submit', event => { event.preventDefault(); const value = Number(pageInput.value); if (Number.isInteger(value) && value >= 1 && value <= (manifest?.page_count || 0)) render(value - 1); else if (active) controls(); });
    listening(slider, 'input', () => { if (active) { pageInput.value = Number(slider.value) + 1; scheduleChrome(); } }); listening(slider, 'change', () => { if (active) render(Number(slider.value)); });
    for (const name of Object.keys(choices)) {
      listening($('share-' + name + '-trigger'), 'click', () => openChoice(name));
      listening($('share-' + name + '-trigger'), 'keydown', event => { if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) { event.preventDefault(); event.stopPropagation?.(); openChoice(name, event.key); } });
    }
    listening(popup, 'keydown', event => {
      if (!choice) return;
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) { event.preventDefault(); event.stopPropagation?.(); const at = popupOptions.indexOf(doc.activeElement), index = event.key === 'Home' ? 0 : event.key === 'End' ? popupOptions.length - 1 : (at + (event.key === 'ArrowUp' ? -1 : 1) + popupOptions.length) % popupOptions.length; popupOptions[index]?.focus(); }
      else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation?.(); closeChoice(true); }
      else if (event.key === 'Tab') closeChoice();
    });
    listening(reader, 'pointerdown', event => { if (choice && !event.target?.closest?.('.reader-choice,.reader-choice-popover')) closeChoice(); scheduleChrome(); }, true);
    listening(reader, 'focusout', scheduleChrome); listening(reader, 'keydown', event => { if (event.key === 'Tab' && active) setChrome(true); });
    listening($('share-theme'), 'click', () => change('theme', prefs.theme === 'dark' ? 'light' : 'dark')); listening($('share-menu-theme'), 'click', () => change('theme', prefs.theme === 'dark' ? 'light' : 'dark'));
    listening($('share-fullscreen'), 'click', fullscreen); listening($('share-help'), 'click', toggleHelp); listening($('share-help-close'), 'click', () => { help.hidden = true; setChrome(true); });
    listening($('share-menu-toggle'), 'click', () => { menu.hidden = !menu.hidden; $('share-menu-toggle').setAttribute('aria-expanded', String(!menu.hidden)); setChrome(true); });
    listening($('share-menu-mode'), 'click', () => change('mode', prefs.mode === 'double' ? 'single' : 'double'));
    listening($('share-cover-alone'), 'change', event => change('coverAlone', Boolean(event.target?.checked ?? $('share-cover-alone').checked))); listening($('share-spread-offset'), 'change', event => change('offset', Boolean(event.target?.checked ?? $('share-spread-offset').checked)));
    listening($('share-tap-previous'), 'click', event => { event.stopPropagation?.(); if (Date.now() >= suppressClickUntil) move(direction() === 'rtl' ? 1 : -1); });
    listening($('share-tap-next'), 'click', event => { event.stopPropagation?.(); if (Date.now() >= suppressClickUntil) move(direction() === 'rtl' ? -1 : 1); });
    listening(viewport, 'click', event => {
      if (!active || Date.now() < suppressClickUntil || event.target?.closest?.('button,input,a') || !help.hidden) return;
      const rect = viewport.getBoundingClientRect?.(); if (!rect) return;
      const position = vertical() ? (event.clientY - rect.top) / rect.height : (event.clientX - rect.left) / rect.width;
      if (vertical() && (position < .25 || position > .75)) move(position < .25 ? -1 : 1); else if (position >= .25 && position <= .75) setChrome(reader.dataset.controls !== 'visible');
    });
    listening(viewport, 'scroll', () => { if (!active || mode() !== 'webtoon' || scrollTimer) return; scrollTimer = win.setTimeout(() => { scrollTimer = null; syncScroll(); }, 60); }, { passive: true });
    listening(viewport, 'wheel', event => {
      if (active && mode() === 'webtoon' && !event.ctrlKey && !event.metaKey) scrollRestoring = null;
      if (!active || !vertical() || !help.hidden || choice || event.ctrlKey) return;
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight || 800 : 1), scrolling = Model.canScrollPage(delta, viewport);
      const turn = wheel.next(delta, Date.now(), { scrolling, blocked: Math.abs(event.deltaX || 0) > Math.abs(event.deltaY) });
      if (!scrolling) event.preventDefault(); if (turn) move(turn);
    }, { passive: false });
    listening(viewport, 'touchstart', event => {
      if (!active || event.touches.length !== 1) { touch = null; suppressClickUntil = Date.now() + 500; return; }
      touch = { x: event.touches[0].clientX, y: event.touches[0].clientY, top: viewport.scrollTop || 0, page, version: sequence, up: Model.canScrollPage(-1, viewport), down: Model.canScrollPage(1, viewport) };
    }, { passive: true });
    listening(viewport, 'touchmove', event => {
      if (event.touches.length !== 1 || (win.visualViewport?.scale || 1) > 1.01) { touch = null; suppressClickUntil = Date.now() + 500; }
      else if (touch && mode() === 'webtoon' && Math.max(Math.abs(event.touches[0].clientX - touch.x), Math.abs(event.touches[0].clientY - touch.y)) > 10) scrollRestoring = null;
    }, { passive: true });
    listening(viewport, 'touchend', event => {
      const origin = touch; touch = null; if (!origin || !active || event.changedTouches.length !== 1 || (event.touches?.length || 0)) return;
      const dx = event.changedTouches[0].clientX - origin.x, dy = event.changedTouches[0].clientY - origin.y;
      if (Math.max(Math.abs(dx), Math.abs(dy)) > 10) suppressClickUntil = Date.now() + 500;
      if (origin.page !== page || origin.version !== sequence || mode() === 'webtoon') return;
      const delta = Model.swipePageDelta({ dx, dy, vertical: vertical(), direction: direction(), blocked: !help.hidden || Boolean(choice) || (win.visualViewport?.scale || 1) > 1.01 || Math.abs((viewport.scrollTop || 0) - origin.top) > 2 || vertical() && (dy < 0 ? origin.down : origin.up) });
      if (delta) move(delta);
    }, { passive: true }); listening(viewport, 'touchcancel', () => { touch = null; }, { passive: true });
    listening(viewport, 'pointerdown', event => { if (event.target === viewport && event.pointerType !== 'touch') scrollRestoring = null; }, { passive: true });
    listening(doc, 'keydown', event => {
      if (!active || event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
      if (event.key === 'Escape') { event.preventDefault(); if (choice) closeChoice(true); else if (!help.hidden) help.hidden = true; else if (!menu.hidden) menu.hidden = true; else if (doc.fullscreenElement) doc.exitFullscreen?.(); else exit(); return; }
      if (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test((event.target || doc.activeElement)?.tagName || '') || choice) return;
      if (!help.hidden && !['?', '/'].includes(event.key)) return;
      let delta = 0;
      if (['ArrowDown', 'PageDown', ' '].includes(event.key)) delta = event.shiftKey && event.key === ' ' ? -1 : 1;
      else if (['ArrowUp', 'PageUp'].includes(event.key)) delta = -1;
      else if (['ArrowLeft', 'ArrowRight'].includes(event.key)) delta = (event.key === 'ArrowRight' ? 1 : -1) * (direction() === 'rtl' ? -1 : 1);
      if (delta) { event.preventDefault(); if (mode() === 'webtoon') { scrollRestoring = null; scrollBy(delta * Math.max(80, (viewport.clientHeight || 800) * .8)); } else if (vertical()) verticalMove(delta, event); else move(delta); }
      else if (['Home', 'End'].includes(event.key)) { event.preventDefault(); render(event.key === 'Home' ? 0 : manifest.page_count - 1); }
      else if (event.key.toLowerCase() === 'm') { event.preventDefault(); setChrome(reader.dataset.controls !== 'visible'); }
      else if (event.key === '?' || event.key === '/') { event.preventDefault(); toggleHelp(); }
      else if (event.key === 'Enter') { event.preventDefault(); fullscreen(); }
      else if (event.key === 'Backspace') { event.preventDefault(); exit(); }
    });
    listening(doc, 'visibilitychange', () => { if (!doc.hidden && active) checkSession(); });
    listening(win, 'resize', () => { if (!active) return; positionChoice(); controls(); win.clearTimeout(resizeTimer); resizeTimer = win.setTimeout(() => { resizeTimer = null; if (active && (lastMode !== mode() || mode() === 'webtoon' || manifest.format === 'pdf')) render(page); }, 100); });
    listening(doc, 'fullscreenchange', () => { $('share-fullscreen').setAttribute('aria-label', doc.fullscreenElement ? '退出全屏' : '进入全屏'); if (active) controls(); });
    listening(win, 'pagehide', () => { stopped = true; clearSession(); }); listening(win, 'pageshow', event => { if (event.persisted) start(); });
    return { start, reset() { stopped = true; lifecycle++; clearSession(); }, checkSession };
  }
  return { create, tokenFromPath, validManifest, pageNumber, messages, readingPrefs };
});

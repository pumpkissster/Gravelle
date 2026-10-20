/* Isolated anonymous reader. No account state, passwords or keys are persisted. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else { root.GravelleShareReader = api; if (root.document) api.create({ window: root, document: root.document }).start(); }
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const messages = Object.freeze({ expired: '分享已到期，请联系分享者。', closed: '分享已关闭。', exhausted: '分享的访问次数已用完。', unavailable: '分享或作品已不可用。', invalid: '分享链接不正确。' });
  function tokenFromPath(path) { return /^\/share\/([A-Za-z0-9_-]{43})\/?$/.exec(String(path || ''))?.[1] || null; }
  function validManifest(value) { return Boolean(value?.manifest && typeof value.manifest.title === 'string' && Number.isInteger(value.manifest.page_count) && value.manifest.page_count > 0 && value.manifest.page_count <= 100000 && typeof value.key === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value.key) && ['folder', 'zip', 'cbz', 'archive', 'pdf', 'epub'].includes(value.manifest.format)); }
  function pageNumber(value, total) { const number = Number(value); return Number.isInteger(number) ? Math.min(Math.max(number, 0), Math.max(0, total - 1)) : 0; }
  function create({ window: win = window, document: doc = document, fetch: request, crypto: decryptor } = {}) {
    const fetcher = request || win.fetch.bind(win); const decrypt = decryptor || win.GravelleShareCrypto;
    const token = tokenFromPath(win.location.pathname); const api = '/api/share/' + token;
    const $ = id => doc.getElementById(id);
    const gate = $('share-gate'), reader = $('share-reader'), form = $('share-unlock-form'), input = $('share-password'), unlock = $('share-unlock'), gateRetry = $('share-gate-retry');
    const gateTitle = $('share-access-title'), gateMessage = $('share-gate-message'), passwordError = $('share-password-error');
    const host = $('share-image-host'), message = $('share-page-message'), messageText = $('share-page-message-text'), pageRetry = $('share-page-retry');
    const pageInput = $('share-page-number'), pageTotal = $('share-page-total'), previous = $('share-previous'), next = $('share-next'), viewport = $('share-viewport');
    let key = null, manifest = null, active = false, sequence = 0, page = 0, objectURL = null, controller = null, heartbeat = null, expiryTimer = null, checking = false, stopped = false, image = null, isBusy = false;
    let lifecycle = 0, exitRequested = false, logoutPending = null, unlockPending = null;
    const storageKey = 'gravelle-share-page:' + token;
    const exitKey = 'gravelle-share-exit:' + token;
    function savedExit() { try { return win.sessionStorage.getItem(exitKey) === '1'; } catch (_) { return false; } }
    function saveExit(value) { try { if (value) win.sessionStorage.setItem(exitKey, '1'); else win.sessionStorage.removeItem(exitKey); } catch (_) {} }
    const abort = () => { controller?.abort(); controller = null; };
    function clearMedia() { sequence++; abort(); if (image) { image.onload = image.onerror = null; image.removeAttribute('src'); image = null; } host.replaceChildren(); if (objectURL) win.URL.revokeObjectURL(objectURL); objectURL = null; }
    function clearSession() { key = manifest = null; active = false; clearMedia(); win.clearInterval(heartbeat); heartbeat = null; win.clearTimeout(expiryTimer); expiryTimer = null; }
    function gateState(text, { password = false, retry = false, title = '分享已结束' } = {}) { clearSession(); reader.hidden = true; gate.hidden = false; gateTitle.textContent = title; gateMessage.textContent = text; gateMessage.hidden = !text; form.hidden = !password; gateRetry.hidden = !retry; input.value = ''; passwordError.hidden = true; input.disabled = unlock.disabled = exitRequested; isBusy = false; doc.title = '写真分享 · Gravelle'; }
    function end(status, text) { stopped = true; gateState(messages[status] || text || messages.unavailable); }
    function locked(text = '') { gateState(text, { password: true, title: '输入访问密码' }); input.focus(); }
    async function json(path, options = {}) { const response = await fetcher(path, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', ...options }); let data; try { data = await response.json(); } catch (_) { data = {}; } if (!response.ok) { const problem = new Error(data.error || data.message || '暂时无法连接，请稍后重试。'); problem.status = response.status; problem.code = data.status || data.reason || data.code; throw problem; } return data; }
    function terminal(problem) { if ([403, 404, 410].includes(problem.status)) { end(problem.code, problem.message); return true; } if (problem.status === 401) { locked('访问凭证已失效，请重新输入密码。'); return true; } return false; }
    function scheduleExpiry(value) { win.clearTimeout(expiryTimer); const raw = value.session_expires_at; if (!raw) return; const when = typeof raw === 'number' ? raw * (raw < 1e12 ? 1000 : 1) : Date.parse(raw); if (!Number.isFinite(when)) return; const wait = when - Date.now(); if (wait <= 0) { locked('本次访问已结束，请重新输入密码。'); return; } expiryTimer = win.setTimeout(() => checkSession(), Math.min(wait + 50, 2147483647)); }
    async function accept(value, restore) { if (!validManifest(value)) throw new Error('分享内容格式不正确。'); key = value.key; manifest = value.manifest; active = true; stopped = false; page = pageNumber(restore, manifest.page_count); gate.hidden = true; reader.hidden = false; $('share-title').textContent = manifest.title; doc.title = manifest.title + ' · Gravelle'; pageTotal.textContent = '/ ' + manifest.page_count; pageInput.max = manifest.page_count; scheduleExpiry(value); if (!active) return; win.clearInterval(heartbeat); heartbeat = win.setInterval(() => { if (!doc.hidden) checkSession(); }, 15000); await render(page); }
    function readPosition() { try { return pageNumber(win.sessionStorage.getItem(storageKey), Number.MAX_SAFE_INTEGER); } catch (_) { return 0; } }
    async function start() { if (!token) { end('invalid'); return; } if (exitRequested || savedExit()) return exit(); const current = ++lifecycle; stopped = false; gateRetry.hidden = true; gateRetry.textContent = '重试'; gateTitle.textContent = '打开分享'; gateMessage.textContent = '正在检查分享…'; gateMessage.hidden = false; form.hidden = true;
      try { const value = await json(api + '/manifest'); if (stopped || current !== lifecycle) return; await accept(value, readPosition()); }
      catch (problem) { if (stopped || current !== lifecycle) return; if (problem.status === 401) { try { const info = await json(api); if (stopped || current !== lifecycle) return; if (info.status !== 'active') end(info.status); else locked(); } catch (error) { if (stopped || current !== lifecycle) return; if (!terminal(error)) gateState(error.message, { retry: true, title: '暂时无法打开分享' }); } } else if (!terminal(problem)) gateState(problem.message, { retry: true, title: '暂时无法打开分享' }); }
    }
    async function checkSession() { if (!active || checking || stopped) return; const checkedKey = key, current = lifecycle; checking = true; try { const value = await json(api + '/manifest'); if (!active || stopped || key !== checkedKey || current !== lifecycle) return; if (!validManifest(value) || value.key !== key || value.manifest.page_count !== manifest.page_count) { end('unavailable'); return; } scheduleExpiry(value); } catch (problem) { if (!active || stopped || key !== checkedKey || current !== lifecycle) return; if (!terminal(problem)) { gateState('暂时无法验证分享，请重试后继续阅读。', { retry: true, title: '连接中断' }); } } finally { checking = false; } }
    async function exit() {
      if (logoutPending) return logoutPending;
      exitRequested = true; stopped = true; saveExit(true); const current = ++lifecycle;
      gateState('正在退出阅读…', { title: '退出阅读' });
      const task = (async () => {
        try {
          // Wait for a pending unlock's Set-Cookie before deleting that session.
          if (unlockPending) await unlockPending.catch(() => {});
          const result = await json(api + '/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          if (result?.logged_out !== true) throw new Error('logout not confirmed');
          if (current !== lifecycle) return;
          saveExit(false); exitRequested = false; stopped = false; gateRetry.textContent = '重试'; locked();
        } catch (_) {
          if (current !== lifecycle) return;
          gateState('暂时无法完成退出，请重试。', { retry: true, title: '退出未完成' }); gateRetry.textContent = '重试退出';
        }
      })();
      logoutPending = task; try { await task; } finally { if (logoutPending === task) logoutPending = null; }
    }
    function controls() { pageInput.value = page + 1; pageInput.disabled = !active; previous.disabled = !active || page === 0; next.disabled = !active || page >= (manifest?.page_count || 0) - 1; }
    async function render(index) {
      if (!active || !manifest || stopped) return; page = pageNumber(index, manifest.page_count); clearMedia(); const current = sequence; const currentKey = key; controller = new win.AbortController(); const signal = controller.signal;
      controls(); message.hidden = false; messageText.textContent = '正在加载图片…'; pageRetry.hidden = true; viewport.scrollTop = 0;
      try {
        const response = await fetcher(api + '/pages/' + page, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', signal });
        if (!response.ok) { let data = {}; try { data = await response.json(); } catch (_) {} const problem = new Error(data.error || data.message || '图片加载失败。'); problem.status = response.status; problem.code = data.status || data.reason || data.code; throw problem; }
        const packet = await response.arrayBuffer(); if (current !== sequence || !active || key !== currentKey) return;
        if (!decrypt?.decrypt) throw new Error('图片解密组件未能加载，请刷新页面。');
        const result = await decrypt.decrypt(packet, currentKey, 'gravelle-share-v1:' + token + ':' + page); if (current !== sequence || !active || key !== currentKey) return;
        if (!result?.data || !['image/jpeg', 'image/png', 'image/webp', 'image/avif', 'image/gif', 'image/bmp'].includes(result.mime)) throw new Error('图片数据格式不正确。');
        const url = win.URL.createObjectURL(new win.Blob([result.data], { type: result.mime })); if (current !== sequence || !active) { win.URL.revokeObjectURL(url); return; }
        objectURL = url; const node = doc.createElement('img'); image = node; node.alt = `${manifest.title} · 第 ${page + 1} 页`; node.draggable = false; node.decoding = 'async';
        node.onload = () => { if (current !== sequence || !active) return; message.hidden = true; try { win.sessionStorage.setItem(storageKey, String(page)); } catch (_) {} };
        node.onerror = () => { if (current !== sequence || !active) return; clearMedia(); messageText.textContent = '图片暂时无法显示。'; pageRetry.hidden = false; };
        host.replaceChildren(node); node.src = url;
      } catch (problem) { if (current !== sequence || problem.name === 'AbortError' || !active) return; if (terminal(problem)) return; messageText.textContent = problem.message || '图片加载失败，请重试。'; pageRetry.hidden = false; }
    }
    form.addEventListener('submit', async event => { event.preventDefault(); if (isBusy || stopped || !token || !form.reportValidity()) return; isBusy = true; unlock.disabled = true; passwordError.hidden = true; const password = input.value; input.value = '';
      const current = ++lifecycle; const pending = json(api + '/unlock', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) }); unlockPending = pending;
      try { const value = await pending; if (stopped || current !== lifecycle) return; await accept(value, readPosition()); }
      catch (problem) { if (stopped || current !== lifecycle) return; if ([404, 410].includes(problem.status)) end(problem.code, problem.message); else { passwordError.textContent = problem.status === 429 ? '尝试次数较多，请稍后再试。' : problem.message; passwordError.hidden = false; input.focus(); } }
      finally { if (unlockPending === pending) unlockPending = null; if (current === lifecycle) { isBusy = false; unlock.disabled = false; } }
    });
    gateRetry.addEventListener('click', start); pageRetry.addEventListener('click', () => render(page)); previous.addEventListener('click', () => render(page - 1)); next.addEventListener('click', () => render(page + 1));
    $('share-page-form').addEventListener('submit', event => { event.preventDefault(); const value = Number(pageInput.value); if (Number.isInteger(value) && value >= 1 && value <= (manifest?.page_count || 0)) render(value - 1); else controls(); });
    $('share-fit').addEventListener('click', () => { const width = reader.dataset.fit !== 'width'; reader.dataset.fit = width ? 'width' : 'height'; $('share-fit').setAttribute('aria-pressed', String(width)); $('share-fit').textContent = width ? '适应屏幕' : '适应宽度'; });
    $('share-theme').addEventListener('click', () => { const light = reader.dataset.theme !== 'light'; reader.dataset.theme = light ? 'light' : 'dark'; $('share-theme').textContent = light ? '深色' : '浅色'; $('share-theme').setAttribute('aria-label', light ? '切换为深色背景' : '切换为浅色背景'); });
    $('share-exit').addEventListener('click', exit);
    doc.addEventListener('keydown', event => { if (!active || ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(doc.activeElement?.tagName)) return; if (event.key === 'ArrowRight' || event.key === 'PageDown') { event.preventDefault(); if (page < manifest.page_count - 1) render(page + 1); } if (event.key === 'ArrowLeft' || event.key === 'PageUp') { event.preventDefault(); if (page > 0) render(page - 1); } });
    let touch = null; viewport.addEventListener('touchstart', event => { touch = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null; }, { passive: true });
    viewport.addEventListener('touchend', event => { if (!touch || !active || event.changedTouches.length !== 1) return; const dx = event.changedTouches[0].clientX - touch.x, dy = event.changedTouches[0].clientY - touch.y; touch = null; if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.6) { if (dx < 0 && page < manifest.page_count - 1) render(page + 1); else if (dx > 0 && page > 0) render(page - 1); } }, { passive: true });
    doc.addEventListener('visibilitychange', () => { if (!doc.hidden && active) checkSession(); });
    win.addEventListener('pagehide', () => { stopped = true; clearSession(); });
    win.addEventListener('pageshow', event => { if (event.persisted) start(); });
    return { start, reset() { stopped = true; lifecycle++; clearSession(); }, checkSession };
  }
  return { create, tokenFromPath, validManifest, pageNumber, messages };
});

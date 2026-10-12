/* Gravelle — a private library with no build step or external dependencies. */
(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
  const defaultPrefs = { fit: 'height', theme: 'dark' };
  const Model = window.SumiReaderModel;
  const End = window.SumiReaderEnd;
  const Performance = window.SumiPerformance;
  const Avatars = window.GravelleAvatars;
  const Users = window.GravelleUsers;
  const Batch = window.GravelleBatchSelection;
  const Routes = window.GravellePageRouter;
  const routing = { current: Routes.parse(location.hash), ready: false, restoring: false, generation: 0 };
  let activeUser = null;
  let authGeneration = 0;
  const userManagement = { users: [], libraries: [], loaded: false, loading: false, error: false, updatedAt: 0, request: 0, busy: false, editingId: null, generation: 0 };
  const visibleLibraries = { libraries: [], selected: new Set(), loaded: false, loading: false, busy: false, dirty: false, generation: 0 };
  let profileMutationBusy = false;
  let profileFormGeneration = 0;
  const selection = { enabled: false, ids: new Set(), busy: false, revision: 0 };
  const batchModels = { ids: [], selected: new Set(), context: null, generation: 0, busy: false };
  const avatarEdits = {};
  let modelMutationBusy = false;
  let renderedLibraryData = '';
  const catalogPaging = { page: 0, signature: '', revealed: 0 };
  const catalogFlow = { observer: null, epoch: 0, books: [], batchSize: 12 };
  const chapterPaging = { page: 0, signature: '' };
  const modeLabels = { single: '单页', double: '双页', webtoon: '条漫' };
  const allowedPrefs = { mode: ['single', 'double', 'webtoon'], direction: ['ltr', 'rtl'], fit: ['height', 'width'], theme: ['dark', 'light'] };
  const state = { session: null, books: [], scanning: false, lastScan: null, scanErrors: [], filter: 'all', query: '', series: '', sort: 'added', loading: true, loaded: false, poll: null, refreshPromise: null, libraryError: null, libraries: [], libraryId: '', rootPath: '/library', view: 'home', models: [], bookmarks: [], modelId: '', detailBook: null, editingBook: null, selectedModels: new Set(), editingModel: null, bookmarkTarget: null, editingLibrary: null, directoryPath: '', prefs: { mode: 'single', direction: 'ltr', ...readPrefs() } };
  const reader = { open: false, book: null, index: 0, token: 0, epoch: 0, observer: null, rows: [], visible: new Set(), frame: 0, restoring: null, preloads: [], loadedPage: null, lastVisible: null, saveTimer: null, pending: null, saveChains: new Map(), settingsChains: new Map(), saveSeq: 0, noticeType: '', suppressClickUntil: 0, sizes: [], spreads: [], coverAlone: true, offset: false, effective: 'single', chromeTimer: null, turnAnimations: [], scrollJumpBehavior: 'instant' };
  let toastTimer;
  let settingsAccessRequest = null;
  let settingsAccessReadAt = 0;
  let settingsAccessGeneration = 0;
  let settingsUpdateRequest = null;
  let settingsUpdateGeneration = 0;
  reader.pageCache = Performance.createCache(Performance.get().imageCacheLimit, entry => entry.cancel?.());
  const librarySorts = readLibrarySorts();
  reader.endOverlay = null;
  reader.endSequence = null;
  reader.endBoundaryAt = 0;
  reader.lastWheelAt = 0;

  const isAdmin = () => Users.isAdmin(state.session?.user);
  const can = permission => Users.can(state.session?.user, permission);
  function requireAccess(permission = 'admin') {
    const allowed = permission === 'admin' ? isAdmin() : can(permission);
    if (!allowed) toast('当前账号没有此操作的权限。');
    return allowed;
  }
  function stored(name) {
    if (!activeUser) return null;
    try {
      const key = Users.storageKey(name, activeUser);
      let value = localStorage.getItem(key);
      if (value === null && Users.isAdmin(activeUser) && Number(activeUser.id) === 1) {
        value = localStorage.getItem(name);
        if (value !== null) localStorage.setItem(key, value);
      }
      return value;
    } catch (_) { return null; }
  }
  function storePreference(name, value) {
    try { if (activeUser) localStorage.setItem(Users.storageKey(name, activeUser), value); } catch (_) { /* Optional device preference. */ }
  }
  function activateAccount(user) {
    if (String(activeUser?.id ?? '') === String(user?.id ?? '')) { activeUser = user || null; return; }
    routing.ready = false; routing.restoring = false; routing.generation += 1;
    authGeneration += 1;
    reader.pending = null; clearTimeout(reader.saveTimer);
    if (reader.open) closeReader(false);
    ['library-dialog', 'book-detail', 'metadata-dialog', 'model-dialog', 'bookmark-dialog', 'profile-dialog', 'remove-dialog', 'user-dialog', 'users-dialog', 'batch-model-dialog'].forEach(id => { if ($('#' + id).open) $('#' + id).close(); });
    reader.saveChains.clear(); reader.settingsChains.clear(); reader.pageCache.clear();
    clearTimeout(state.poll); state.poll = null; state.refreshPromise = null;
    stopCatalogFlow(); resetSelection(); selection.busy = false;
    state.books = []; state.libraries = []; state.models = []; state.bookmarks = [];
    state.loaded = false; state.loading = true; state.scanning = false; state.scanErrors = []; state.libraryError = null; state.lastScan = null; state.rootPath = '/library';
    state.view = 'home'; state.libraryId = ''; state.modelId = ''; state.query = ''; state.series = ''; state.sort = 'added';
    state.detailBook = null; state.editingBook = null; state.editingModel = null; state.editingLibrary = null; state.selectedModels.clear(); state.bookmarkTarget = null; state.directoryPath = '';
    $('#search').value = ''; $('#sort').value = 'added';
    ['book-grid', 'library-tiles', 'library-nav', 'continue-card', 'models-grid', 'bookmark-list', 'detail-content', 'metadata-models'].forEach(id => $('#' + id).replaceChildren());
    userManagement.generation += 1; Object.assign(userManagement, { users: [], libraries: [], loaded: false, loading: false, error: false, updatedAt: 0, request: 0, busy: false, editingId: null });
    $('#users-list').replaceChildren(); $('#users-status').textContent = ''; $('#user-form').reset();
    visibleLibraries.generation += 1; Object.assign(visibleLibraries, { libraries: [], selected: new Set(), loaded: false, loading: false, busy: false, dirty: false });
    $('#my-libraries-options').replaceChildren(); $('#my-libraries-status').textContent = '';
    activeUser = user || null;
    state.prefs = { mode: 'single', direction: 'ltr', ...readPrefs() };
    librarySorts.clear(); readLibrarySorts().forEach((value, key) => librarySorts.set(key, value));
    applyInterfaceTheme(interfaceTheme());
    Performance.setUser(activeUser);
    catalogPaging.signature = ''; chapterPaging.signature = ''; renderedLibraryData = '';
  }

  function readLibrarySorts() {
    try {
      const saved = JSON.parse(stored('sumi-library-sorts') || '{}');
      return new Map(Object.entries(saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {}).map(([id, sort]) => [id, End.normalizeSort(sort)]));
    } catch (_) { return new Map(); }
  }
  function librarySort(id) { return librarySorts.get(String(id)) || 'added'; }
  function saveLibrarySort(id, sort) {
    librarySorts.set(String(id), End.normalizeSort(sort));
    storePreference('sumi-library-sorts', JSON.stringify(Object.fromEntries(librarySorts)));
  }

  function interfaceTheme() {
    try { return stored('sumi-interface-theme') === 'light' ? 'light' : 'dark'; }
    catch (_) { return 'dark'; }
  }
  function applyInterfaceTheme(theme) {
    const value = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.interfaceTheme = value;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', value === 'light' ? '#fcf8fa' : '#19161a');
    storePreference('sumi-interface-theme', value);
  }
  applyInterfaceTheme(interfaceTheme());

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = String(content);
    return element;
  }

  function joinDistinctText(parts, separator = ' · ') {
    const seen = new Set();
    return parts.map(value => String(value || '').trim()).filter(value => {
      const key = value.normalize('NFKC').toLocaleLowerCase('zh-CN');
      if (!value || seen.has(key)) return false;
      seen.add(key); return true;
    }).join(separator);
  }

  function icon(name) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    element.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    element.append(use);
    return element;
  }

  function readPrefs() {
    let saved = {};
    try { saved = JSON.parse(stored('sumi-preferences') || '{}') || {}; } catch (_) { /* Storage may be disabled. */ }
    const prefs = { ...defaultPrefs };
    Object.keys(defaultPrefs).forEach(key => { if (allowedPrefs[key].includes(saved[key])) prefs[key] = saved[key]; });
    return prefs;
  }

  function savePref(key, value) {
    if (!allowedPrefs[key]?.includes(value)) return;
    state.prefs[key] = value;
    storePreference('sumi-preferences', JSON.stringify(Object.fromEntries(Object.keys(defaultPrefs).map(key => [key, state.prefs[key]]))));
  }

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  async function api(path, options = {}) {
    const generation = authGeneration;
    let response;
    try {
      response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options, headers: { ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
    } catch (_) { throw new ApiError('无法连接媒体库，请检查服务端或网络连接。', 0); }
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (generation !== authGeneration) throw new ApiError('账户已切换。', 401);
    if (!response.ok) {
      if (response.status === 401 && !['/api/login', '/api/register', '/api/profile'].includes(path)) handleUnauthorized();
      throw new ApiError(typeof data.error === 'string' ? data.error : (data.error?.message || data.message || `请求失败（${response.status}）`), response.status);
    }
    return data;
  }

  const post = (path, data, options = {}) => api(path, { method: 'POST', body: JSON.stringify(data), ...options });
  const bookUrl = id => '/api/books/' + encodeURIComponent(id);
  const pageUrl = (id, index) => bookUrl(id) + '/pages/' + index;

  function toast(message) {
    const element = $('#toast');
    element.textContent = message;
    element.hidden = false;
    if (typeof element.showPopover === 'function' && typeof element.hidePopover === 'function') {
      try {
        element.setAttribute('popover', 'manual');
        if (element.matches(':popover-open')) element.hidePopover();
        element.showPopover();
      } catch (_) {
        element.removeAttribute('popover');
      }
    } else {
      element.removeAttribute('popover');
    }
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      if (typeof element.hidePopover === 'function') {
        try { element.hidePopover(); } catch (_) {}
      }
      element.hidden = true;
    }, 3800);
  }

  function handleUnauthorized() {
    clearTimeout(state.poll);
    if (reader.open) closeReader(false);
    state.session = { ...state.session, authenticated: false, user: null };
    showLogin();
    $('#login-error').textContent = '登录已失效，请重新登录。'; $('#login-error').hidden = false;
  }

  function showLogin() {
    activateAccount(null);
    stopCatalogFlow();
    renderedLibraryData = '';
    settingsAccessGeneration += 1;
    settingsAccessRequest = null;
    settingsAccessReadAt = 0;
    settingsUpdateGeneration += 1;
    settingsUpdateRequest = null;
    $('#check-updates').disabled = false;
    $('#check-updates').textContent = '检查新版本';
    $('#update-status').textContent = '点击检查 GitHub 上的稳定版本。升级需要手动完成。';
    $('#update-status').removeAttribute('data-status');
    $('#update-release').hidden = true;
    $('#update-release').removeAttribute('href');
    $('#settings-lan-url').removeAttribute('href');
    $('#settings-lan-url').textContent = '正在获取…';
    ['library-dialog', 'book-detail', 'metadata-dialog', 'model-dialog', 'bookmark-dialog', 'profile-dialog', 'remove-dialog', 'user-dialog', 'users-dialog', 'batch-model-dialog'].forEach(id => { if ($('#' + id).open) $('#' + id).close(); });
    closeAccountMenu();
    $('#app').hidden = true;
    $('#login-screen').hidden = false;
    const registering = Boolean(state.session?.registration_required);
    $('#login-heading').textContent = registering ? '注册账户' : '登录';
    $('#registration-confirm-field').hidden = !registering;
    $('#password-confirm').required = registering;
    $('#password-confirm').disabled = !registering;
    $('#password').autocomplete = registering ? 'new-password' : 'current-password';
    $('#password').minLength = registering ? 8 : 1;
    $('#password').placeholder = registering ? '至少 8 个字符' : '输入密码';
    $('#setup-password-field').hidden = !registering || !state.session?.password_required;
    $('#setup-password').required = registering && Boolean(state.session?.password_required);
    $('#setup-password').disabled = !$('#setup-password').required;
    $('#login-submit').replaceChildren(document.createTextNode(registering ? '注册并进入' : '登录'), icon('arrow'));
    ['password', 'password-confirm', 'setup-password'].forEach(id => { $('#' + id).value = ''; });
    $('#login-error').hidden = true;
    $('#username').focus();
  }

  function showLibrary() {
    activateAccount(state.session?.user);
    $('#login-screen').hidden = true;
    $('#app').hidden = false;
    renderAccount();
    loadVisibleLibraries();
  }

  function closeAccountMenu() {
    $('#account-menu').hidden = true;
    $('#account-toggle').setAttribute('aria-expanded', 'false');
  }
  function renderAccount() {
    const user = state.session?.user;
    const name = user?.username || '个人账户';
    $('#account-name').textContent = name;
    $('#settings-account-name').textContent = name;
    $('#settings-account-role').textContent = isAdmin() ? '管理员' : '用户';
    ['account-avatar', 'settings-account-avatar'].forEach(id => {
      $('#' + id).replaceChildren(Avatars.createImage(user, { alt: '', lazy: false }));
    });
  }
  function modelAvatar(model, className = '') {
    const image = node('img', className); image.alt = ''; image.decoding = 'async';
    const detailed = state.models.find(item => String(item.id) === String(model?.id)) || model;
    const fallback = detailed?.cover_book_id ? bookUrl(detailed.cover_book_id) + '/cover' : '/default-avatar.png';
    const source = Avatars.source(model, Avatars.source(detailed, fallback));
    image.addEventListener('error', () => { if (image.getAttribute('src') !== '/default-avatar.png') image.src = '/default-avatar.png'; });
    Performance.loadCover(image, source); return image;
  }
  function clearAvatarEdit(kind) {
    avatarEdits[kind]?.controller.abort();
    avatarEdits[kind] = { controller: new AbortController(), data: null, reset: false, pending: false, invalid: false };
    $('#' + kind + '-avatar-upload').value = ''; $('#' + kind + '-save').disabled = false;
  }
  function beginAvatarEdit(kind, entity) {
    clearAvatarEdit(kind);
    const fallback = kind === 'model' && entity?.cover_book_id ? bookUrl(entity.cover_book_id) + '/cover' : '/default-avatar.png';
    avatarEdits[kind].fallback = fallback;
    $('#' + kind + '-avatar-preview').src = Avatars.source(entity, fallback);
  }
  async function readAvatarEdit(kind, file) {
    const fallback = avatarEdits[kind]?.fallback || '/default-avatar.png';
    const previous = avatarEdits[kind];
    clearAvatarEdit(kind); const edit = avatarEdits[kind]; edit.fallback = fallback;
    if (!file) { edit.data = previous?.data || null; edit.reset = previous?.reset || false; return; }
    const error = $('#' + kind + '-error'); error.hidden = true;
    edit.pending = true; $('#' + kind + '-save').disabled = true;
    try {
      edit.data = await Avatars.readFile(file, { signal: edit.controller.signal });
      if (avatarEdits[kind] !== edit) return;
      $('#' + kind + '-avatar-preview').src = edit.data;
    } catch (failure) {
      if (avatarEdits[kind] !== edit || failure.name === 'AbortError') return;
      edit.invalid = true; error.textContent = failure.message; error.hidden = false;
    } finally {
      if (avatarEdits[kind] === edit) { edit.pending = false; $('#' + kind + '-save').disabled = edit.invalid || (kind === 'model' && modelMutationBusy); }
    }
  }
  function resetAvatarEdit(kind) {
    const fallback = avatarEdits[kind]?.fallback || '/default-avatar.png'; clearAvatarEdit(kind);
    avatarEdits[kind].fallback = fallback; avatarEdits[kind].reset = true;
    $('#' + kind + '-avatar-preview').src = fallback; $('#' + kind + '-error').hidden = true;
  }
  function avatarPayload(kind) {
    const edit = avatarEdits[kind];
    if (edit?.pending) throw new Error('正在读取头像，请稍后保存。');
    if (edit?.invalid) throw new Error('请选择有效头像，或恢复默认头像。');
    return edit?.data ? { avatar_upload: edit.data } : edit?.reset ? { avatar_reset: true } : {};
  }
  function showProfileForm() {
    closeAccountMenu();
    const user = state.session?.user;
    if (!user) return;
    profileFormGeneration += 1; profileMutationBusy = false;
    $('#profile-username').value = user.username || '';
    beginAvatarEdit('profile', user);
    ['profile-current-password', 'profile-password', 'profile-password-confirm'].forEach(id => { $('#' + id).value = ''; });
    $('#profile-error').hidden = true;
    discardVisibilityDraft(); loadVisibleLibraries(true);
    setProfileTab('basic'); setProfileBusy(false);
    $('#profile-dialog').showModal();
    $('#profile-username').focus();
  }
  function setProfileTab(tab, focus = false) {
    ['basic', 'permissions'].forEach(name => {
      const active = name === tab; $('#profile-' + name).hidden = !active;
      const button = $('#profile-tab-' + name); button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
  }
  function setProfileBusy(busy) {
    profileMutationBusy = busy; visibleLibraries.busy = busy;
    $$('#profile-form input, #profile-form button').forEach(input => { input.disabled = busy; });
    $('#profile-save').textContent = busy ? '正在保存…' : '保存修改';
    syncVisibleLibraries();
    if (!busy) $('#profile-save').disabled = Boolean(avatarEdits.profile?.pending || avatarEdits.profile?.invalid);
  }
  function discardVisibilityDraft() {
    visibleLibraries.generation += 1; visibleLibraries.loading = false; visibleLibraries.busy = false; visibleLibraries.dirty = false;
    visibleLibraries.selected = new Set(visibleLibraries.libraries.filter(library => library.visible).map(library => String(library.id)));
    renderVisibleLibraries();
  }

  const typeLabels = { photo: '写真', manga_jp: '日漫', manga_kr: '韩漫', comic_us: '美漫' };
  const viewLabels = { home: '主页', libraries: '写真库', library: '写真库', recent: '最近阅读', bookmarks: '书签', favorite: '收藏', search: '搜索', models: '模特', model: '模特作品', settings: '设置' };
  function isPhoto(book = reader.book) { return book?.media_type === 'photo'; }
  function isReading(book) { return Boolean(book.progress); }
  function currentLibrary() { return state.libraries.find(library => String(library.id) === String(state.libraryId)); }
  function currentModel() { return state.models.find(model => String(model.id) === String(state.modelId)); }
  function formatDate(value, time = false) {
    if (!value) return '—';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', time ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false } : { year: 'numeric', month: '2-digit', day: '2-digit' });
  }
  function formatAddedDate(value, time = true) {
    if (!value) return '—';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return '—';
    const pad = number => String(number).padStart(2, '0');
    const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    return time ? `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}` : day;
  }
  function scopedBooks() {
    if (state.view === 'library') return state.books.filter(book => String(book.library_id) === String(state.libraryId));
    if (state.view === 'model') return state.books.filter(book => (book.models || []).some(model => String(model.id) === String(state.modelId)));
    if (state.view === 'favorite') return state.books.filter(book => book.favorite);
    if (state.view === 'recent') return state.books.filter(isReading);
    return state.books;
  }
  function filteredBooks() {
    const query = state.query.trim().toLocaleLowerCase();
    return scopedBooks().filter(book => {
      if (state.series && book.series !== state.series) return false;
      if (query && ![book.title, book.series, book.path, book.author, book.library_name, ...(book.models || []).map(model => model.name)].some(value => String(value || '').toLocaleLowerCase().includes(query))) return false;
      return true;
    }).sort((a, b) => End.compareBooks(a, b, state.view === 'home' ? 'added' : state.view === 'recent' ? 'updated' : state.sort));
  }
  function currentViewRoute() {
    return Routes.normalize({ view: state.view, id: state.view === 'library' ? state.libraryId : state.modelId, query: state.query, sort: state.sort, series: state.series });
  }
  function commitRoute(route, { replace = false } = {}) {
    const value = Routes.normalize(route); const hash = Routes.hash(value);
    const previous = Routes.hash(Routes.parse(location.hash));
    routing.current = value;
    if (location.hash !== hash) {
      const entry = { gravelleRoute: hash, gravellePrevious: replace ? history.state?.gravellePrevious : previous };
      history[replace ? 'replaceState' : 'pushState'](entry, '', hash);
    }
  }
  function syncViewRoute(replace = false) {
    if (!routing.ready || routing.restoring || !state.session?.authenticated) return;
    commitRoute(currentViewRoute(), { replace });
  }
  function returnFromOverlay() {
    if (!routing.ready || !state.session?.authenticated) return;
    routing.generation += 1; routing.restoring = false;
    const target = Routes.returnTo(routing.current);
    if (history.state?.gravellePrevious === Routes.hash(target)) history.back();
    else { commitRoute(target, { replace: true }); restoreLocation(); }
  }
  async function restoreLocation() {
    // Keep a deep link intact while the login screen or a failed catalog request is visible.
    if (!state.session?.authenticated || !state.loaded) return;
    const requested = Routes.parse(location.hash);
    if (selection.busy && Routes.hash(requested) !== Routes.hash(routing.current)) {
      commitRoute(routing.current, { replace: true }); toast('批量操作进行中，请稍候。'); return;
    }
    const resolved = Routes.resolve(requested, state);
    const route = resolved.route; const generation = ++routing.generation;
    routing.ready = true; routing.restoring = true;
    commitRoute(route, { replace: true });
    try {
      ['library-dialog', 'metadata-dialog', 'model-dialog', 'bookmark-dialog', 'profile-dialog', 'user-dialog', 'users-dialog', 'batch-model-dialog'].forEach(id => { if ($('#' + id).open) $('#' + id).close(); });
      const base = Routes.base(route);
      navigate(base.view, { libraryId: base.view === 'library' ? base.id : undefined, modelId: base.view === 'model' ? base.id : undefined, query: base.query || '', sort: base.sort, series: base.series || '', fromRoute: true });
      if (reader.open && !(route.view === 'reader' && String(reader.book?.id) === route.id)) closeReader(true, { fromRoute: true });
      if ($('#book-detail').open && !(route.view === 'book' && String(state.detailBook?.id) === route.id)) $('#book-detail').close();
      if (resolved.unavailable) toast('作品或写真库不存在，或当前账号无权访问。');
      if (route.view === 'book') {
        const book = state.books.find(item => String(item.id) === route.id) || { id: route.id, title: '作品' };
        if (!$('#book-detail').open || String(state.detailBook?.id) !== route.id) await showBookDetail(book, { fromRoute: true });
      } else if (route.view === 'reader') {
        const book = state.books.find(item => String(item.id) === route.id) || { id: route.id, title: '作品' };
        if (reader.open && String(reader.book?.id) === route.id) { if (route.page) setReaderPage(route.page - 1); }
        else await openBook(book, route.page ? route.page - 1 : null, null, { fromRoute: true });
      }
    } finally { if (generation === routing.generation) routing.restoring = false; }
  }
  function navigate(view, options = {}) {
    if (!Object.hasOwn(viewLabels, view)) return;
    if (!options.fromRoute && state.loaded) {
      const candidate = Routes.resolve({ view, id: view === 'library' ? String(options.libraryId ?? state.libraryId) : String(options.modelId ?? state.modelId) }, state);
      if (candidate.unavailable) { toast('写真库或模特不存在，或当前账号无权访问。'); view = candidate.route.view; }
    }
    const libraryId = String(options.libraryId ?? state.libraryId);
    const modelId = String(options.modelId ?? state.modelId);
    const samePage = state.view === view && (view !== 'library' || libraryId === String(state.libraryId)) && (view !== 'model' || modelId === String(state.modelId));
    if (selection.busy && !samePage) { toast('批量操作进行中，请稍候。'); return; }
    if (!options.fromRoute) { routing.generation += 1; routing.restoring = false; }
    closeAccountMenu();
    $('#sidebar').classList.remove('is-open'); $('#nav-backdrop').hidden = true; $('#nav-toggle').setAttribute('aria-expanded', 'false');
    if (samePage && !options.fromRoute) {
      if (['book', 'reader'].includes(routing.current.view)) syncViewRoute();
      if (view === 'search') $('#search').focus({ preventScroll: true }); return;
    }
    resetSelection();
    state.view = view; state.query = options.query || ''; state.series = options.series || ''; $('#search').value = state.query;
    if (options.libraryId !== undefined) state.libraryId = String(options.libraryId);
    if (view === 'library') { state.sort = librarySort(state.libraryId); $('#sort').value = state.sort; }
    if (options.sort) { state.sort = End.normalizeSort(options.sort); $('#sort').value = state.sort; }
    if (options.modelId !== undefined) state.modelId = String(options.modelId);
    renderLibraryNavigation(); renderSeries(); renderLibrary();
    syncViewRoute();
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (view === 'search') $('#search').focus();
    if (['models', 'bookmarks'].includes(view) && !options.fromRoute) refreshLibrary();
  }
  function updateCounts() {
    const counts = { libraries: state.libraries.length, recent: state.books.filter(isReading).length, favorite: state.books.filter(book => book.favorite).length, bookmarks: state.bookmarks.length, models: state.models.length };
    $$('[data-count]').forEach(element => { element.textContent = counts[element.dataset.count] ?? ''; });
    $$('#side-nav [data-view]').forEach(button => {
      const active = button.dataset.view === state.view || (button.dataset.view === 'libraries' && state.view === 'library') || (button.dataset.view === 'models' && state.view === 'model');
      button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    const title = state.view === 'library' ? currentLibrary()?.name || '写真库' : state.view === 'model' ? currentModel()?.name || '模特作品' : viewLabels[state.view];
    $('.library-heading').hidden = state.view === 'home';
    $('#library-title').textContent = title;
    const description = {
      libraries: `${state.libraries.length} 个写真库 · ${state.books.length} 本作品。`,
      library: `${typeLabels[currentLibrary()?.media_type] || '日漫'} · ${scopedBooks().length} 本作品`
    }[state.view] || '';
    $('#heading-description').textContent = description; $('#heading-description').hidden = !description;
    $('#create-library').hidden = state.view !== 'libraries' || !isAdmin();
    $('#edit-library').hidden = state.view !== 'library' || !currentLibrary() || !isAdmin();
    $('#create-model').hidden = state.view !== 'models' || !can('manage_models');
  }
  function makeCover(book, containerClass, lazy = true) {
    const wrap = node('div', containerClass); const image = node('img');
    image.alt = ''; image.decoding = 'async';
    Performance.loadCover(image, bookUrl(book.id) + '/cover?v=' + encodeURIComponent(book.cover_version || book.cover_page || 0), lazy);
    image.addEventListener('error', () => { const fallback = node('div', 'cover-fallback'); fallback.append(icon('book'), node('span', '', book.title || '未命名作品')); image.replaceWith(fallback); }, { once: true }); wrap.append(image); return wrap;
  }
  async function toggleFavorite(book) {
    const result = await post(bookUrl(book.id) + '/favorite', { favorite: !book.favorite }); book.favorite = Boolean(result.favorite);
    const cached = state.books.find(item => item.id === book.id); if (cached) cached.favorite = book.favorite;
    if (state.detailBook?.id === book.id) state.detailBook.favorite = book.favorite;
    renderLibrary();
    if ($('#book-detail').open && state.detailBook?.id === book.id) renderDetail(state.detailBook);
  }
  function makeBookCard(book) {
    const article = node('article', 'book-card'); const open = node('button', 'book-open'); open.type = 'button'; open.dataset.bookId = String(book.id);
    open.setAttribute('aria-label', `查看《${book.title}》详情，共 ${book.page_count} 页`); open.addEventListener('click', () => selection.enabled && Batch.kind(state.view, isAdmin()) === 'books' ? toggleSelection(book.id) : showBookDetail(book));
    const cover = makeCover(book, 'cover-wrap'); cover.append(node('span', 'book-pages', `${book.page_count} 页`));
    if (book.progress) { const bar = node('span', 'cover-progress'); bar.style.width = Math.min(100, (book.progress.page + 1) / book.page_count * 100) + '%'; cover.append(bar); }
    const title = node('span', 'book-title', book.title); title.title = book.title;
    const subtitle = node('span', 'book-series', (book.models || []).map(model => model.name).join(' / ') || book.author || book.library_name || typeLabels[book.media_type] || '作品');
    open.append(cover, title, subtitle);
    const showAdded = state.view === 'home' || (state.view !== 'recent' && ['added', 'added_asc'].includes(state.sort));
    const meta = node('span', 'book-date', state.view === 'recent' ? `第 ${book.progress.page + 1} 页 · ${formatDate(book.progress.updated_at, true)}` : showAdded ? `加入 ${formatAddedDate(book.added_at, false)}` : book.release_date || '');
    if (showAdded) meta.title = `加入时间 · ${formatAddedDate(book.added_at)}${book.release_date ? ` / 发布日期 · ${book.release_date}` : ''}`;
    if (meta.textContent) open.append(meta);
    const favorite = node('button', 'favorite-button'); favorite.type = 'button'; favorite.dataset.bookId = String(book.id); favorite.setAttribute('aria-label', `${book.favorite ? '取消收藏' : '收藏'}《${book.title}》`); favorite.setAttribute('aria-pressed', String(Boolean(book.favorite))); favorite.append(icon('heart'));
    favorite.addEventListener('click', async () => { favorite.disabled = true; try { await toggleFavorite(book); } catch (error) { if (error.status !== 401) toast(error.message); favorite.disabled = false; } }); article.append(open, favorite);
    if (Batch.kind(state.view, isAdmin()) === 'books') { article.classList.toggle('is-selected', selection.enabled && selection.ids.has(String(book.id))); article.append(selectionCheckbox(book.id, book.title, 'book-select')); }
    return article;
  }
  function renderContinue() {
    const candidates = state.view === 'library' ? scopedBooks() : state.books;
    const book = candidates.filter(isReading).sort((a, b) => (b.progress.updated_at || 0) - (a.progress.updated_at || 0))[0];
    const visible = Boolean(book) && ['home', 'recent'].includes(state.view) && !state.query.trim() && !state.series; $('#continue-section').hidden = !visible; if (!visible) return;
    const card = node('div', 'continue-card'); const info = node('div', 'continue-info'); info.append(node('h3', '', book.title), node('span', 'continue-subtitle', joinDistinctText([...(book.models || []).map(model => model.name), book.library_name])));
    const progress = node('div', 'continue-progress'); const track = node('div', 'continue-progress-track'); const fill = node('span'); fill.style.width = Math.min(100, (book.progress.page + 1) / book.page_count * 100) + '%'; track.append(fill); progress.append(track, node('span', '', `上次读到第 ${book.progress.page + 1} / ${book.page_count} 页`)); info.append(progress);
    const resume = node('button', 'button button-primary', '继续阅读'); resume.setAttribute('aria-label', `继续阅读《${book.title}》`); resume.addEventListener('click', () => openBook(book)); card.append(makeCover(book, 'continue-cover', false), info, resume); $('#continue-card').replaceChildren(card);
  }
  function renderLibraryTiles() {
    if (!['home', 'libraries'].includes(state.view)) { $('#library-tiles').replaceChildren(); return; }
    $('#library-tiles').replaceChildren(...state.libraries.map(library => {
      const card = node('article', 'library-tile-card');
      card.classList.toggle('is-selected', selection.enabled && selection.ids.has(String(library.id)));
      const button = node('button', 'library-tile'); button.type = 'button'; button.setAttribute('aria-label', `打开写真库 ${library.name}`);
      const books = state.books.filter(book => String(book.library_id) === String(library.id)); const montage = node('div', 'library-montage');
      if (books.length) { montage.dataset.count = Math.min(books.length, 4); books.slice(0, 4).forEach(book => montage.append(makeCover(book, 'tile-cover'))); } else montage.append(icon('folder'));
      const info = node('div', 'library-tile-info'); info.append(node('strong', '', library.name), node('span', '', `${typeLabels[library.media_type] || '日漫'} · ${books.length} 本作品`));
      button.append(montage, info); button.addEventListener('click', () => selection.enabled && state.view === 'libraries' ? toggleSelection(library.id) : selectLibrary(library.id)); card.append(button);
      if (state.view === 'libraries') card.append(selectionCheckbox(library.id, library.name, 'library-select'));
      return card;
    }));
  }
  function resetSelection() {
    selection.enabled = false; selection.ids.clear(); selection.revision += 1;
    batchModels.context = null; batchModels.generation += 1; batchModels.busy = false; batchModels.ids = []; batchModels.selected.clear();
    if ($('#batch-model-dialog').open) $('#batch-model-dialog').close();
  }
  function selectionItems() { return state.view === 'libraries' ? state.libraries : Batch.kind(state.view, isAdmin()) === 'books' ? filteredBooks() : []; }
  function selectionContext() { return { auth: authGeneration, revision: selection.revision, view: state.view, libraryId: state.libraryId, modelId: state.modelId }; }
  function isSelectionCurrent(context) {
    return context && context.auth === authGeneration && context.revision === selection.revision && context.view === state.view && String(context.libraryId) === String(state.libraryId) && String(context.modelId) === String(state.modelId);
  }
  async function refreshAfterBatch(context) {
    // A scan poll begun before the write may contain the old model associations.
    if (state.refreshPromise) await state.refreshPromise;
    if (context.auth === authGeneration) await refreshLibrary();
  }
  function selectionCheckbox(id, name, className) {
    const input = node('input', 'selection-checkbox ' + className); input.type = 'checkbox'; input.value = String(id);
    input.hidden = !selection.enabled; input.checked = selection.ids.has(String(id)); input.disabled = selection.busy;
    input.setAttribute('aria-label', `选择 ${name}`); input.addEventListener('change', () => toggleSelection(id)); return input;
  }
  function toggleSelection(id) {
    if (selection.busy || !selection.enabled) return;
    const key = String(id); if (selection.ids.has(key)) selection.ids.delete(key); else selection.ids.add(key);
    renderSelection();
  }
  function renderSelection() {
    const visible = Boolean(Batch.kind(state.view, isAdmin()));
    const active = visible && selection.enabled;
    $('#selection-toolbar').hidden = !active; $('#app').dataset.selecting = String(active);
    ['search', 'sort', 'series-filter'].forEach(id => { $('#' + id).disabled = selection.busy; });
    $('#selection-manage').hidden = !visible; $('#selection-manage').disabled = selection.busy;
    $('#selection-manage').setAttribute('aria-pressed', String(active));
    if (!visible) return;
    selection.ids = Batch.retain(selection.ids, selectionItems());
    $$('.selection-checkbox').forEach(input => {
      input.hidden = !active; input.checked = active && selection.ids.has(input.value); input.disabled = selection.busy;
      input.closest('article')?.classList.toggle('is-selected', input.checked);
    });
    $('#selection-toggle').textContent = '完成管理';
    $('#selection-toggle').setAttribute('aria-pressed', String(selection.enabled)); $('#selection-toggle').disabled = selection.busy;
    $('#selection-count').hidden = !selection.enabled; $('#selection-count').textContent = `已选择 ${selection.ids.size} 项`;
    const items = selectionItems(); const all = items.length > 0 && items.every(item => selection.ids.has(String(item.id)));
    $('#selection-all').textContent = all ? '取消全选' : '全选'; $('#selection-all').hidden = !selection.enabled; $('#selection-all').disabled = selection.busy || !items.length;
    const actions = Batch.actions(state.view, isAdmin());
    ['models', 'remove', 'scan', 'favorite', 'random'].forEach(action => {
      const button = $('#selection-' + action);
      button.hidden = !selection.enabled || !actions.includes(action);
      button.disabled = selection.busy || !selection.ids.size || (action === 'scan' && state.scanning);
    });
  }
  function removeConfirmation(kind, count, name = '') {
    if (kind === 'users') return `确认删除用户「${name}」？\n\n这会清除该用户的阅读记录、收藏、书签及个人阅读设置，并使其登录失效。共享写真库、作品资料和源文件都会保留。`;
    if (kind === 'models') return `确认删除模特「${name}」？\n\n这会删除模特资料和头像，解除作品关联及写真库的默认模特设置。作品、阅读记录、收藏、书签和源文件都会保留。\n\n扫描不会自动重新创建已删除的模特；需要时可以手动新建。`;
    return kind === 'libraries' ? `确认移除 ${count} 个写真库？\n\n这会移除写真库及其作品、阅读记录、收藏和书签。不会删除或修改任何源文件。` : `确认移除 ${count} 本作品？\n\n这会移除作品及其阅读记录、收藏和书签。不会删除或修改任何源文件。已移除的作品不会在下次扫描时自动重新加入。`;
  }
  let resolveRemoval = null;
  function confirmRemoval(kind, count, name = '') {
    if (resolveRemoval) return Promise.resolve(false);
    $('#remove-title').textContent = kind === 'users' ? '删除用户' : kind === 'models' ? '删除模特' : kind === 'libraries' ? '移除写真库' : '移除作品';
    $('#remove-confirm').textContent = ['models', 'users'].includes(kind) ? '确认删除' : '确认移除';
    $('#remove-description').textContent = removeConfirmation(kind, count, name);
    return new Promise(resolve => { resolveRemoval = resolve; $('#remove-dialog').showModal(); $('#remove-cancel').focus(); });
  }
  function finishRemoval(confirmed = false) {
    const resolve = resolveRemoval; resolveRemoval = null;
    if ($('#remove-dialog').open) $('#remove-dialog').close();
    resolve?.(confirmed);
  }
  async function batchAction(action) {
    if (selection.busy || !selection.ids.size) return;
    const kind = Batch.kind(state.view, isAdmin()); const ids = [...selection.ids]; const context = selectionContext();
    if (!kind || !Batch.actions(state.view, isAdmin()).includes(action)) return;
    if (kind === 'libraries' && !requireAccess()) return;
    if (action === 'remove' && !requireAccess(kind === 'libraries' ? 'admin' : 'remove_books')) return;
    if (action === 'scan' && !requireAccess('scan_libraries')) return;
    if (action === 'random') {
      const selected = new Set(ids); const books = state.books.filter(book => selected.has(String(kind === 'libraries' ? book.library_id : book.id)));
      if (!books.length) { toast('所选项目中没有可阅读的作品。'); return; }
      openBook(books[Math.floor(Math.random() * books.length)], 0); return;
    }
    if (ids.length > 500) { toast('每次最多管理 500 项，请减少选择后重试。'); return; }
    if (action === 'models') { showBatchModelForm(); return; }
    selection.busy = true; renderSelection();
    try {
      if (action === 'remove' && !await confirmRemoval(kind, ids.length)) return;
      if (!isSelectionCurrent(context)) return;
      await post('/api/' + kind + '/batch', { action, ids, ...(action === 'favorite' ? { favorite: true } : {}) });
      if (!isSelectionCurrent(context)) return;
      if (action === 'remove') { selection.ids.clear(); selection.enabled = false; }
      if (action === 'scan') state.scanning = true;
      await refreshAfterBatch(context);
      if (context.auth === authGeneration) toast(action === 'remove' ? '已移除所选记录，源文件保留。' : action === 'favorite' ? '所选作品已收藏。' : '已开始扫描所选写真库。');
    } catch (error) { if (context.auth === authGeneration && error.status !== 401) toast(error.message); }
    finally { if (context.auth === authGeneration) { selection.busy = false; renderLibrary(); } }
  }
  function closeBatchModelForm() {
    if (batchModels.busy) return;
    if ($('#batch-model-dialog').open) $('#batch-model-dialog').close();
  }
  function syncBatchModelForm() {
    const empty = !batchModels.selected.size;
    $('#batch-model-selection').textContent = `已选择 ${batchModels.selected.size} 位模特`;
    $('#batch-model-clear-field').hidden = !empty;
    if (!empty) $('#batch-model-clear').checked = false;
    $$('input, button', $('#batch-model-form')).forEach(input => { input.disabled = batchModels.busy; });
    $('#batch-model-save').disabled = batchModels.busy || (empty && !$('#batch-model-clear').checked);
    $('#batch-model-save').textContent = batchModels.busy ? '保存中…' : empty ? '清除关联' : '保存';
  }
  function renderBatchModels() {
    renderModelOptions($('#batch-model-options'), $('#batch-model-search').value, batchModels.selected, syncBatchModelForm);
    syncBatchModelForm();
  }
  function showBatchModelForm() {
    if (!requireAccess('edit_metadata') || selection.busy || !selection.ids.size) return;
    batchModels.ids = [...selection.ids]; batchModels.context = selectionContext(); batchModels.generation += 1; batchModels.busy = false;
    const ids = new Set(batchModels.ids); batchModels.selected = Batch.initialModelIds(state.books.filter(book => ids.has(String(book.id))));
    $('#batch-model-description').textContent = `替换所选 ${ids.size} 本作品关联的模特。`;
    $('#batch-model-search').value = ''; $('#batch-model-new').value = ''; $('#batch-model-new-row').hidden = true;
    $('#batch-model-show-new').setAttribute('aria-expanded', 'false'); $('#batch-model-clear').checked = false; $('#batch-model-error').hidden = true;
    renderBatchModels(); $('#batch-model-dialog').showModal(); $('#batch-model-search').focus();
  }
  async function createBatchModel() {
    if (!requireAccess('manage_models') || batchModels.busy || !isSelectionCurrent(batchModels.context)) return;
    const name = $('#batch-model-new').value.trim(); if (!name) { $('#batch-model-new').focus(); return; }
    const generation = batchModels.generation; const context = batchModels.context;
    batchModels.busy = true; selection.busy = true; syncBatchModelForm(); renderSelection(); $('#batch-model-error').hidden = true;
    try {
      const result = await post('/api/models', { name });
      if (generation !== batchModels.generation || !isSelectionCurrent(context)) return;
      const existing = state.models.find(model => String(model.id) === String(result.model.id));
      if (existing) Object.assign(existing, result.model); else state.models.push(result.model);
      state.models.sort((a, b) => collator.compare(a.name, b.name)); updateCounts();
      batchModels.selected.add(String(result.model.id)); $('#batch-model-new').value = ''; $('#batch-model-search').value = '';
      $('#batch-model-new-row').hidden = true; $('#batch-model-show-new').setAttribute('aria-expanded', 'false'); renderBatchModels();
    } catch (error) {
      if (generation === batchModels.generation && context.auth === authGeneration && error.status !== 401) { $('#batch-model-error').textContent = error.message; $('#batch-model-error').hidden = false; }
    } finally {
      if (generation === batchModels.generation && context.auth === authGeneration) { batchModels.busy = false; selection.busy = false; syncBatchModelForm(); renderSelection(); }
    }
  }
  async function saveBatchModels(event) {
    event.preventDefault();
    if (!requireAccess('edit_metadata') || batchModels.busy || !isSelectionCurrent(batchModels.context)) return;
    const modelIds = [...batchModels.selected];
    if (!modelIds.length && !$('#batch-model-clear').checked) { $('#batch-model-clear').focus(); return; }
    if (modelIds.length > 100) { $('#batch-model-error').textContent = '每次最多关联 100 位模特。'; $('#batch-model-error').hidden = false; return; }
    const ids = [...batchModels.ids]; const generation = batchModels.generation; const context = batchModels.context;
    batchModels.busy = true; selection.busy = true; syncBatchModelForm(); renderSelection(); $('#batch-model-error').hidden = true;
    try {
      await post('/api/books/batch', { action: 'models', ids, model_ids: modelIds });
      if (generation !== batchModels.generation || !isSelectionCurrent(context)) return;
      // Reflect the successful write even if a subsequent catalog refresh loses its connection.
      const changed = new Set(ids); const models = modelIds.map(id => state.models.find(model => String(model.id) === id)).filter(Boolean);
      state.books.forEach(book => { if (changed.has(String(book.id))) book.models = models; });
      selection.ids = Batch.retain(selection.ids, selectionItems());
      await refreshAfterBatch(context);
      if (generation !== batchModels.generation || context.auth !== authGeneration) return;
      batchModels.busy = false; $('#batch-model-dialog').close(); toast('所选作品的模特已更新。');
    } catch (error) {
      if (generation === batchModels.generation && context.auth === authGeneration && error.status !== 401) { $('#batch-model-error').textContent = error.message; $('#batch-model-error').hidden = false; }
    } finally {
      if (context.auth === authGeneration) { selection.busy = false; if (generation === batchModels.generation) { batchModels.busy = false; syncBatchModelForm(); } renderLibrary(); }
    }
  }
  async function removeCurrent(kind) {
    if (!requireAccess(kind === 'libraries' ? 'admin' : 'remove_books')) return;
    const id = kind === 'libraries' ? state.editingLibrary : state.editingBook?.id;
    if (!id || selection.busy || !await confirmRemoval(kind, 1)) return;
    selection.busy = true;
    const remove = $(kind === 'libraries' ? '#library-remove' : '#metadata-remove'); remove.disabled = true;
    const save = $(kind === 'libraries' ? '#library-save' : '#metadata-save'); save.disabled = true;
    try {
      await post('/api/' + kind + '/' + encodeURIComponent(id) + '/remove', {});
      if (kind === 'books' && ['book', 'reader'].includes(routing.current.view) && routing.current.id === String(id)) commitRoute(Routes.base(routing.current), { replace: true });
      if (kind === 'libraries' && routing.current.view === 'library' && routing.current.id === String(id)) commitRoute({ view: 'libraries' }, { replace: true });
      $(kind === 'libraries' ? '#library-dialog' : '#metadata-dialog').close();
      if (kind === 'books' && $('#book-detail').open) $('#book-detail').close();
      resetSelection(); await refreshLibrary();
      if (kind === 'libraries') navigate('libraries');
      toast('已移除记录，源文件保留。');
    } catch (error) { if (error.status !== 401) toast(error.message); }
    finally { selection.busy = false; remove.disabled = false; save.disabled = false; renderLibrary(); }
  }
  function renderSeries() {
    const select = $('#series-filter'); const books = scopedBooks(); const series = [...new Set(books.filter(book => !isPhoto(book)).map(book => book.series).filter(Boolean))].sort(collator.compare);
    if (state.series && !series.includes(state.series)) state.series = '';
    select.replaceChildren(new Option('所有系列', ''), ...series.map(name => new Option(name, name))); select.value = state.series;
    select.closest('label').hidden = !series.length || (state.view === 'library' && currentLibrary()?.media_type === 'photo');
  }
  function renderEmpty() {
    $('#empty-state').hidden = false; const library = currentLibrary(); const onboarding = !state.libraries.length && ['home', 'libraries', 'library'].includes(state.view);
    const raw = !state.query.trim() && !state.series;
    $('#empty-title').textContent = onboarding ? '从第一个写真库开始。' : !raw ? '没有找到匹配的作品。' : { libraries: '你的第一本作品，正等着入架。', library: state.scanning ? '正在整理你的写真库…' : '这个写真库还没有作品。', recent: '阅读记录将从这里开始。', favorite: '把喜欢的作品留在这里。', search: '没有找到匹配的作品。', bookmarks: '为喜欢的页面留一个位置。', models: '认识藏馆中的每一位模特。', model: '还没有关联这位模特的作品。' }[state.view] || '这里暂时没有内容。';
    $('#empty-description').textContent = onboarding ? '创建写真库，设置目录与阅读习惯。' : !raw ? '试试作品名称、模特或写真库名称，或调整筛选条件。' : { libraries: '把图片文件夹放入媒体目录，再扫描写真库。', library: `将作品放入 ${library?.path || '/library'}，扫描后会自动入库。`, recent: '打开任意作品，Gravelle会记录每次阅读的位置和时间。', favorite: '点击作品旁的爱心，或在详情页收藏作品。', search: '在上方搜索全部写真库中的作品。', bookmarks: '阅读时点击书签按钮，保存当前页并添加备注。', models: '新建模特，再到作品的“编辑资料”中关联。一部作品可以关联多位模特。', model: '在作品的“编辑资料”中选择这位模特。' }[state.view] || '';
    const showGuide = ['home', 'libraries', 'library'].includes(state.view) && raw && isAdmin(); $('#folder-guide').hidden = !showGuide; $('#empty-scan').hidden = !showGuide; $('#empty-hint').hidden = !showGuide;
    if (onboarding && visibleLibraries.loaded && visibleLibraries.libraries.length) { $('#empty-title').textContent = '暂无显示的写真库。'; $('#empty-description').textContent = '可在设置 → 用户管理 → 编辑用户信息 → 权限中选择需要显示的写真库。'; $('#empty-scan').hidden = true; $('#folder-guide').hidden = true; $('#empty-hint').hidden = true; }
    else if (onboarding && !isAdmin()) { $('#empty-title').textContent = '暂无可访问的写真库。'; $('#empty-description').textContent = '请联系管理员分配写真库访问权限。'; }
  }
  function renderNotice() {
    const notice = $('#notice'); notice.replaceChildren(); notice.classList.toggle('error', Boolean(state.libraryError));
    if (state.libraryError) { notice.append(node('span', '', state.libraryError)); const retry = node('button', '', '重新连接'); retry.addEventListener('click', reconnectLibrary); notice.append(retry); notice.hidden = false; }
    else if (state.scanning) { notice.append(node('span', 'spinner'), node('span', '', '正在扫描媒体文件夹，新作品会自动出现在写真库。')); notice.hidden = false; } else notice.hidden = true;
    $$('.scan-trigger').forEach(button => { button.hidden = !isAdmin() || !can('scan_libraries'); button.disabled = state.scanning || (!state.libraries.length && state.loading); $('span', button).textContent = !state.libraries.length ? '创建第一个写真库' : state.scanning ? '扫描中…' : '扫描写真库'; });
  }
  function renderErrors(errors = []) {
    const wrap = $('#scan-errors'); wrap.hidden = !errors.length; wrap.replaceChildren(); if (!errors.length) return;
    const details = node('details'); details.append(node('summary', '', `有 ${errors.length} 个目录未能完整扫描`)); const list = node('ul'); errors.slice(0, 100).forEach(error => list.append(node('li', '', typeof error === 'string' ? error : `${error.path || '写真库'}：${error.message || '扫描失败'}`))); details.append(list); wrap.append(details);
  }
  function renderBookmarks() {
    $('#bookmark-list').replaceChildren(...state.bookmarks.map(bookmark => {
      const book = bookmark.book || state.books.find(item => item.id === bookmark.book_id); if (!book) return node('div');
      const row = node('article', 'bookmark-card'); const open = node('button', 'bookmark-open'); open.type = 'button'; const image = node('img'); image.alt = `${book.title}第 ${bookmark.page + 1} 页`; Performance.loadCover(image, pageUrl(book.id, bookmark.page));
      const info = node('div'); info.append(node('h3', '', book.title), node('p', 'bookmark-location', `第 ${bookmark.page + 1} 页 · ${book.library_name || ''}`), node('p', 'bookmark-note', bookmark.note || '无备注'), node('small', '', formatDate(bookmark.created_at, true))); open.append(image, info); open.setAttribute('aria-label', `打开书签 ${book.title} 第 ${bookmark.page + 1} 页`); open.addEventListener('click', () => openBook(book, bookmark.page));
      const remove = node('button', 'icon-button bookmark-remove'); remove.append(icon('close')); remove.setAttribute('aria-label', `移除书签 ${book.title} 第 ${bookmark.page + 1} 页`); remove.addEventListener('click', async () => { remove.disabled = true; try { await post('/api/bookmarks/' + encodeURIComponent(bookmark.id) + '/delete', {}); state.bookmarks = state.bookmarks.filter(item => item.id !== bookmark.id); renderLibrary(); toast('书签已移除。'); } catch (error) { toast(error.message); remove.disabled = false; } }); row.append(open, remove); return row;
    }));
  }
  function renderModels() {
    $('#models-grid').replaceChildren(...state.models.map(model => {
      const card = node('article', 'model-card'); const open = node('button', 'model-open'); const portrait = node('div', 'model-portrait');
      portrait.append(modelAvatar(model));
      open.append(portrait, node('strong', '', model.name), node('small', '', `${model.book_count || 0} 本作品`)); open.setAttribute('aria-label', `查看模特 ${model.name} 的作品`); open.addEventListener('click', () => navigate('model', { modelId: model.id }));
      const edit = node('button', 'model-edit', '编辑'); edit.addEventListener('click', () => showModelForm(model)); card.append(open); if (can('manage_models')) card.append(edit); return card;
    }));
  }
  function loadSettingsAccess() {
    if (settingsAccessRequest || !isAdmin() || !state.session?.authenticated || (settingsAccessReadAt && Date.now() - settingsAccessReadAt < 30000)) return;
    const link = $('#settings-lan-url');
    const generation = settingsAccessGeneration;
    if (!settingsAccessReadAt) { link.textContent = '正在获取…'; link.removeAttribute('href'); }
    const operation = (async () => {
      try {
        const result = await api('/api/access');
        if (!state.session?.authenticated || generation !== settingsAccessGeneration) return;
        let address = null;
        try {
          const url = new URL(result.lan_url);
          if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) address = url.href;
        } catch (_) { /* No confirmed local-network address is available. */ }
        link.textContent = address ? address.replace(/\/$/, '') : '暂不可用';
        if (address) link.href = address; else link.removeAttribute('href');
        settingsAccessReadAt = Date.now();
      } catch (error) {
        if (error.status !== 401 && generation === settingsAccessGeneration) { link.textContent = '暂不可用'; link.removeAttribute('href'); settingsAccessReadAt = Date.now(); }
      } finally { if (settingsAccessRequest === operation) settingsAccessRequest = null; }
    })();
    settingsAccessRequest = operation;
  }
  function trustedUpdateReleaseUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash) return null;
      if (!/^\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/releases\/tag\/v?[0-9]+\.[0-9]+\.[0-9]+$/.test(url.pathname)) return null;
      return url.href;
    } catch (_) { return null; }
  }
  async function checkUpdates() {
    if (!requireAccess() || settingsUpdateRequest || !state.session?.authenticated) return;
    const generation = settingsUpdateGeneration;
    const button = $('#check-updates');
    const status = $('#update-status');
    const release = $('#update-release');
    button.disabled = true;
    button.textContent = '正在检查…';
    status.dataset.status = 'checking';
    status.textContent = '正在通过 GitHub 检查稳定版本…';
    release.hidden = true;
    release.removeAttribute('href');
    const operation = (async () => {
      try {
        const result = await api('/api/updates');
        if (generation !== settingsUpdateGeneration || !state.session?.authenticated) return;
        const known = ['available', 'up_to_date', 'unconfigured', 'error'];
        if (!known.includes(result.status)) throw new ApiError('无法识别版本检查结果，请重试。', 0);
        status.dataset.status = result.status;
        status.textContent = String(result.message || '版本检查已完成。');
        if (result.status === 'available') {
          const url = trustedUpdateReleaseUrl(result.release_url);
          if (!url) throw new ApiError('发布页面地址无效，请稍后重试。', 0);
          release.href = url;
          release.hidden = false;
        }
      } catch (error) {
        if (generation !== settingsUpdateGeneration || error.status === 401) return;
        status.dataset.status = 'error';
        status.textContent = error.message + ' 可以稍后重试。';
      } finally {
        if (generation === settingsUpdateGeneration) {
          button.disabled = false;
          button.textContent = status.dataset.status === 'error' ? '重试检查' : '检查新版本';
        }
        if (settingsUpdateRequest === operation) settingsUpdateRequest = null;
      }
    })();
    settingsUpdateRequest = operation;
    return operation;
  }
  function renderSettings() {
    $('.service-panel').hidden = !isAdmin(); $('#users-panel').hidden = false; $('#subaccounts-setting').hidden = !isAdmin();
    renderSubaccountSummary();
    if (isAdmin()) loadUsers();
    Object.keys(defaultPrefs).forEach(key => { $('#pref-' + key).value = state.prefs[key]; }); $('#settings-version').textContent = 'v' + String(state.session?.version || '1.8.2').replace(/^v/, '');
    $('#settings-connection').textContent = state.libraryError ? '连接异常 · ' + state.libraryError : '已连接';
    $('#settings-connection').classList.toggle('connection-error', Boolean(state.libraryError));
    $('#interface-theme').value = interfaceTheme();
    renderAccount();
    loadSettingsAccess();
    window.SumiPerformance?.render();
  }
  function renderSubaccountSummary() {
    const count = $('#subaccounts-count');
    if (!isAdmin()) { count.textContent = ''; return; }
    if (userManagement.loaded) {
      const total = userManagement.users.filter(Users.canManage).length;
      count.textContent = `${total} 个子账户${userManagement.loading ? ' · 正在更新…' : userManagement.error ? ' · 更新失败' : ''}`;
    } else count.textContent = userManagement.loading ? '正在读取…' : userManagement.error ? '暂时无法读取数量' : '尚未读取';
  }
  function renderUsers() {
    renderSubaccountSummary();
    $('#user-create').disabled = userManagement.loading || userManagement.busy || !userManagement.loaded;
    $('#users-list').setAttribute('aria-busy', String(userManagement.busy || userManagement.loading));
    $('#users-close').disabled = userManagement.busy; $('#users-done').disabled = userManagement.busy;
    const users = userManagement.users.filter(Users.canManage);
    $('#users-list').replaceChildren(...users.map(user => {
      const row = node('div', 'user-list-row'); const info = node('div', 'user-list-info');
      info.append(node('strong', '', user.username), node('span', 'user-list-summary', user.role === 'admin' ? '管理员 · 全部写真库' : `${user.enabled ? '已启用' : '已禁用'} · ${user.library_ids?.length || 0} 个写真库`)); row.append(info);
      if (Users.canManage(user)) {
        const actions = node('div', 'user-list-actions'); const edit = node('button', 'button button-secondary', '编辑'); edit.type = 'button'; edit.disabled = userManagement.busy; edit.setAttribute('aria-label', `编辑用户 ${user.username}`); edit.addEventListener('click', () => showUserForm(user));
        const toggle = node('button', 'button button-secondary', user.enabled ? '禁用' : '启用'); toggle.type = 'button'; toggle.disabled = userManagement.busy; toggle.setAttribute('aria-label', `${user.enabled ? '禁用' : '启用'}用户 ${user.username}`); toggle.addEventListener('click', () => toggleUser(user)); actions.append(edit, toggle); row.append(actions);
      }
      return row;
    }));
    if (!users.length && userManagement.loaded && !userManagement.loading) $('#users-list').append(node('p', 'field-hint users-empty', '暂无子账户。'));
  }
  function showUsersList() {
    if (!requireAccess()) return;
    if (!$('#users-dialog').open) $('#users-dialog').showModal();
    loadUsers(true);
  }
  async function loadUsers(force = false) {
    if (!isAdmin() || (userManagement.loading && !force) || (userManagement.loaded && !force && Date.now() - userManagement.updatedAt < 30000)) return;
    const generation = userManagement.generation; const request = ++userManagement.request;
    userManagement.loading = true; userManagement.error = false; $('#users-status').textContent = '正在读取用户…'; renderUsers();
    try {
      const result = await api('/api/users');
      if (generation !== userManagement.generation || request !== userManagement.request || !isAdmin()) return;
      if (!Array.isArray(result?.users) || !Array.isArray(result?.libraries)) throw new Error('无法读取子账户资料，请重试。');
      userManagement.users = result.users; userManagement.libraries = result.libraries;
      userManagement.loaded = true; userManagement.updatedAt = Date.now(); $('#users-status').textContent = '';
    } catch (error) {
      if (generation === userManagement.generation && request === userManagement.request && error.status !== 401) {
        userManagement.error = true;
        $('#users-status').textContent = error.message;
        const retry = node('button', 'button button-secondary', '重试'); retry.type = 'button'; retry.addEventListener('click', () => loadUsers(true)); $('#users-status').append(document.createTextNode(' '), retry);
      }
    } finally { if (generation === userManagement.generation && request === userManagement.request) { userManagement.loading = false; renderUsers(); } }
  }
  function syncUserLibrarySelection() {
    const choices = $$('#user-libraries input'); const selected = choices.filter(input => input.checked).length;
    $('#user-libraries-all').checked = choices.length > 0 && selected === choices.length;
    $('#user-libraries-all').indeterminate = selected > 0 && selected < choices.length;
    $('#user-libraries-all').disabled = userManagement.busy || !choices.length;
    $('#user-libraries-none').disabled = userManagement.busy || !choices.length;
    choices.forEach(input => { $('.visibility-library-content small', input.closest('label')).textContent = input.checked ? '已授权' : '未授权'; });
  }
  function setUserTab(tab, focus = false) {
    ['basic', 'permissions'].forEach(name => {
      const active = name === tab; $('#user-' + name).hidden = !active;
      const button = $('#user-tab-' + name); button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    $('.dialog-body', $('#user-dialog')).scrollTop = 0;
  }
  function setUserBusy(busy) {
    userManagement.busy = busy;
    $$('#user-form input, #user-form button').forEach(input => { input.disabled = busy; });
    syncUserLibrarySelection(); renderUsers();
  }
  async function showUserForm(user = null) {
    if (!requireAccess() || userManagement.busy) return;
    if (!userManagement.loaded) { await loadUsers(); if (!userManagement.loaded || !isAdmin()) return; }
    if (user && !Users.canManage(user)) return;
    userManagement.editingId = user?.id ?? null;
    $('#user-form').reset(); $('#user-error').hidden = true; setUserTab('basic');
    $('#user-dialog-title').textContent = user ? '编辑用户' : '新增用户'; $('#user-username').value = user?.username || '';
    $('#user-password-label').textContent = user ? '重置密码' : '密码'; $('#user-password').required = !user; $('#user-password').placeholder = user ? '留空则不修改密码' : '至少 8 个字符';
    $('#user-password-confirm').required = !user; $('#user-enabled').checked = user?.enabled !== false; $('#user-delete').hidden = !user;
    $('#user-save').textContent = user ? '保存修改' : '创建用户';
    const allowed = new Set((user?.library_ids || []).map(String));
    $('#user-libraries').replaceChildren(...userManagement.libraries.map(library => {
      const label = node('label', 'library-visibility-card'); const tile = node('span', 'visibility-library-icon'); tile.append(icon('folder'));
      const content = node('span', 'visibility-library-content'); content.append(node('strong', '', library.name), node('small', '', allowed.has(String(library.id)) ? '已授权' : '未授权'));
      const input = node('input', 'visibility-input'); input.type = 'checkbox'; input.value = String(library.id); input.checked = allowed.has(String(library.id)); input.setAttribute('role', 'switch'); input.setAttribute('aria-label', `授权写真库 ${library.name}`); input.addEventListener('change', syncUserLibrarySelection);
      const toggle = node('span', 'visibility-switch'); toggle.setAttribute('aria-hidden', 'true'); label.append(tile, content, input, toggle); return label;
    }));
    if (!userManagement.libraries.length) $('#user-libraries').append(node('p', 'field-hint', '暂无写真库'));
    setUserBusy(false); if (!$('#user-dialog').open) $('#user-dialog').showModal(); $('#user-username').focus();
  }
  async function toggleUser(user) {
    if (!requireAccess() || !Users.canManage(user) || userManagement.busy) return;
    const generation = userManagement.generation; setUserBusy(true); $('#users-status').textContent = '';
    try { await post('/api/users/' + encodeURIComponent(user.id), { enabled: !user.enabled }); await loadUsers(true); toast(user.enabled ? '用户已禁用，其登录已失效。' : '用户已启用。'); }
    catch (error) { if (generation === userManagement.generation && error.status !== 401) $('#users-status').textContent = error.message; }
    finally { if (generation === userManagement.generation) setUserBusy(false); }
  }
  function syncVisibleLibraries() {
    const choices = $$('#my-libraries-options input'); const selected = choices.filter(input => input.checked).length;
    $('#my-libraries-all').checked = choices.length > 0 && selected === choices.length;
    $('#my-libraries-all').indeterminate = selected > 0 && selected < choices.length;
    $('#my-libraries-all').disabled = visibleLibraries.busy || visibleLibraries.loading || !visibleLibraries.loaded || !choices.length;
    $('#my-libraries-all').closest('label').hidden = !visibleLibraries.libraries.length;
    $('#my-libraries-none').disabled = visibleLibraries.busy || visibleLibraries.loading || !visibleLibraries.loaded || !choices.length;
    choices.forEach(input => { input.disabled = visibleLibraries.busy || visibleLibraries.loading || !visibleLibraries.loaded; });
  }
  function renderVisibleLibraries() {
    $('#my-libraries-options').replaceChildren(...visibleLibraries.libraries.map(library => {
      const label = node('label', 'library-visibility-card'); const tile = node('span', 'visibility-library-icon'); tile.append(icon('folder'));
      const content = node('span', 'visibility-library-content'); const title = node('strong', '', library.name); const status = node('small', '', visibleLibraries.selected.has(String(library.id)) ? '显示中' : '已隐藏'); content.append(title, status);
      const input = node('input', 'visibility-input'); input.type = 'checkbox'; input.value = String(library.id); input.checked = visibleLibraries.selected.has(String(library.id)); input.setAttribute('role', 'switch'); input.setAttribute('aria-label', `显示写真库 ${library.name}`);
      input.addEventListener('change', event => {
        if (event.target.checked) visibleLibraries.selected.add(String(library.id)); else visibleLibraries.selected.delete(String(library.id));
        status.textContent = event.target.checked ? '显示中' : '已隐藏';
        visibleLibraries.dirty = true; syncVisibleLibraries();
      }); const toggle = node('span', 'visibility-switch'); toggle.setAttribute('aria-hidden', 'true'); label.append(tile, content, input, toggle); return label;
    }));
    syncVisibleLibraries();
  }
  async function loadVisibleLibraries(force = false) {
    if (!state.session?.authenticated || visibleLibraries.loading || (visibleLibraries.loaded && !force)) return;
    const generation = visibleLibraries.generation;
    if (force) visibleLibraries.loaded = false;
    visibleLibraries.loading = true; $('#my-libraries-status').textContent = '正在读取写真库…'; syncVisibleLibraries();
    try {
      const result = await api('/api/my-libraries');
      if (generation !== visibleLibraries.generation) return;
      visibleLibraries.libraries = result.libraries || [];
      if (!visibleLibraries.dirty) visibleLibraries.selected = new Set(visibleLibraries.libraries.filter(library => library.visible).map(library => String(library.id)));
      else { const authorized = new Set(visibleLibraries.libraries.map(library => String(library.id))); visibleLibraries.selected.forEach(id => { if (!authorized.has(id)) visibleLibraries.selected.delete(id); }); }
      visibleLibraries.loaded = true; $('#my-libraries-status').textContent = visibleLibraries.libraries.length ? '' : '暂无可显示的写真库。';
      if (!state.libraries.length && state.loaded && ['home', 'libraries'].includes(state.view)) renderEmpty();
    } catch (error) {
      if (generation === visibleLibraries.generation && error.status !== 401) { $('#my-libraries-status').textContent = error.message; const retry = node('button', 'button button-secondary', '重试'); retry.type = 'button'; retry.addEventListener('click', () => loadVisibleLibraries(true)); $('#my-libraries-status').append(document.createTextNode(' '), retry); }
    } finally { if (generation === visibleLibraries.generation) { visibleLibraries.loading = false; renderVisibleLibraries(); } }
  }
  function renderPagination(container, page, onChange) {
    container.replaceChildren();
    const previous = node('button', 'button button-secondary', '上一页'); previous.type = 'button'; previous.disabled = page.page === 0; previous.addEventListener('click', () => onChange(page.page - 1));
    const current = node('span', '', `第 ${page.page + 1} / ${page.count} 页`); current.setAttribute('aria-live', 'polite');
    const next = node('button', 'button button-secondary', '下一页'); next.type = 'button'; next.disabled = page.page >= page.count - 1; next.addEventListener('click', () => onChange(page.page + 1));
    container.append(previous, current, next); container.hidden = page.count < 2;
  }
  function stopCatalogFlow() {
    catalogFlow.observer?.disconnect(); catalogFlow.observer = null; catalogFlow.epoch += 1;
  }

  function updateCatalogCount(books, page) {
    $('#results-label').textContent = state.loading ? '正在读取写真库…' : `${books.length} 本作品${page.flow && page.hasMore ? ` · 已显示 ${page.end}` : page.count > 1 ? ` · 当前 ${page.start + 1}–${page.end}` : ''}`;
  }

  function watchCatalogFlow(sentinel) {
    if (sentinel.hidden || typeof IntersectionObserver === 'undefined') return;
    const epoch = catalogFlow.epoch;
    catalogFlow.observer ||= new IntersectionObserver(entries => {
      if (epoch === catalogFlow.epoch && entries.some(entry => entry.isIntersecting)) loadMoreCatalogBooks();
    }, { rootMargin: '400px 0px' });
    // Re-arm after appending so a tall viewport keeps filling until the end is below it.
    catalogFlow.observer.unobserve(sentinel); catalogFlow.observer.observe(sentinel);
  }

  function loadMoreCatalogBooks(manual = false) {
    const sentinel = $('#book-flow-sentinel');
    if (!sentinel || sentinel.hidden || reader.open || !state.session?.authenticated || Performance.get().bookPageSize !== 0 || $('#catalog-section').hidden) return;
    const start = catalogPaging.revealed;
    const page = Performance.catalogWindow(catalogFlow.books.length, 0, 0, start + catalogFlow.batchSize, catalogFlow.batchSize);
    if (page.end <= start) return;
    const cards = catalogFlow.books.slice(start, page.end).map(makeBookCard);
    $('#book-grid').append(...cards); catalogPaging.revealed = page.end;
    updateCatalogCount(catalogFlow.books, page);
    sentinel.hidden = !page.hasMore;
    if (sentinel.hidden) catalogFlow.observer?.disconnect(); else watchCatalogFlow(sentinel);
    if (manual) $('.book-open', cards[0])?.focus({ preventScroll: true });
  }

  function renderLibrary() {
    stopCatalogFlow();
    const active = document.activeElement; const focusBookId = active?.closest('#book-grid') ? active.dataset.bookId : null; const focusClass = active?.classList.contains('favorite-button') ? '.favorite-button' : '.book-open';
    $('#app').dataset.view = state.view; document.documentElement.dataset.view = state.view;
    renderSelection(); updateCounts(); renderContinue(); renderNotice(); renderErrors(state.scanErrors); renderLibraryTiles();
    $('#library-home').hidden = !['home', 'libraries'].includes(state.view) || !state.libraries.length;
    const catalog = !['libraries', 'bookmarks', 'models', 'settings'].includes(state.view); $('#catalog-section').hidden = !catalog || (state.view === 'home' && !state.books.length && !state.loading); $('#bookmark-section').hidden = state.view !== 'bookmarks'; $('#models-section').hidden = state.view !== 'models'; $('#settings-page').hidden = state.view !== 'settings';
    $('#loading-state').hidden = !state.loading; $('#shelf-heading').textContent = { home: '最近加入', library: '', recent: '阅读记录', favorite: '收藏的作品', search: '', model: '关联作品' }[state.view] ?? '作品';
    $('#shelf-heading').hidden = ['library', 'search'].includes(state.view);
    $('.search-box').hidden = !['library', 'search', 'model', 'favorite', 'recent'].includes(state.view); $('#sort').closest('label').hidden = ['home', 'recent'].includes(state.view);
    $('.select-tools').hidden = state.view === 'home' || $$('label', $('.select-tools')).every(label => label.hidden);
    $('.shelf-meta').hidden = state.view === 'home';
    const books = filteredBooks();
    const capacity = Performance.get().bookPageSize;
    const signature = JSON.stringify([state.view, state.libraryId, state.modelId, state.query, state.series, state.sort, capacity]);
    if (catalogPaging.signature !== signature) { catalogPaging.signature = signature; catalogPaging.page = 0; catalogPaging.revealed = 0; }
    const grid = $('#book-grid'); const columns = Math.max(1, getComputedStyle(grid).gridTemplateColumns.split(' ').filter(value => parseFloat(value) > 0).length);
    const page = Performance.catalogWindow(books.length, catalogPaging.page, capacity, catalogPaging.revealed, Math.max(12, columns * 3));
    catalogPaging.page = page.page; catalogPaging.revealed = page.end;
    grid.replaceChildren(...(catalog ? books.slice(page.start, page.end).map(makeBookCard) : [])); updateCatalogCount(books, page);
    let pagination = $('#book-pagination'); if (!pagination) { pagination = node('nav', 'page-pagination'); pagination.id = 'book-pagination'; pagination.setAttribute('aria-label', '作品分页'); grid.after(pagination); }
    renderPagination(pagination, page, value => { catalogPaging.page = value; renderLibrary(); $('#catalog-section').scrollIntoView({ block: 'start', behavior: 'smooth' }); }); pagination.hidden = !catalog || page.flow || page.count < 2;
    let sentinel = $('#book-flow-sentinel');
    if (!sentinel) { sentinel = node('div', 'book-flow-sentinel'); sentinel.id = 'book-flow-sentinel'; const more = node('button', 'button button-secondary', '加载更多作品'); more.type = 'button'; more.addEventListener('click', () => loadMoreCatalogBooks(true)); sentinel.append(more); pagination.after(sentinel); }
    sentinel.hidden = !catalog || !page.flow || !page.hasMore;
    catalogFlow.books = books; catalogFlow.batchSize = page.size;
    watchCatalogFlow(sentinel);
    if (state.view === 'bookmarks') renderBookmarks(); else $('#bookmark-list').replaceChildren();
    if (state.view === 'models') renderModels(); else $('#models-grid').replaceChildren();
    if (state.view === 'settings') renderSettings();
    const empty = state.view === 'bookmarks' ? !state.bookmarks.length : state.view === 'models' ? !state.models.length : ['home', 'libraries'].includes(state.view) ? !state.libraries.length : catalog ? !books.length : false;
    $('#empty-state').hidden = state.loading || !empty || Boolean(state.libraryError && !state.loaded); if (!$('#empty-state').hidden) renderEmpty();
    if (focusBookId && !reader.open) $$(focusClass).find(element => element.dataset.bookId === focusBookId)?.focus({ preventScroll: true });
    renderedLibraryData = libraryDataSignature();
  }
  function libraryDataSignature() {
    // Compare with what is on screen, since optimistic edits can change state
    // before the server responds. Scan timestamps alone are not visible.
    const libraries = state.libraries.map(({ last_scan, ...library }) => library);
    return JSON.stringify([libraries, state.books, state.models, state.bookmarks, state.rootPath,
      state.scanning, state.scanErrors, state.loading, state.loaded, state.libraryError, state.view, state.libraryId, state.modelId, state.session?.user]);
  }
  async function refreshLibrary() {
    if (state.refreshPromise) return state.refreshPromise; if (!state.session?.authenticated) return;
    const generation = authGeneration;
    const operation = (async () => { try {
      const session = await api('/api/session');
      if (!session.authenticated) { handleUnauthorized(); return; }
      state.session = session;
      if (String(session.user?.id) !== String(activeUser?.id)) { showLibrary(); await refreshLibrary(); return; }
      activeUser = session.user;
      const [libraries, data, models, bookmarks] = await Promise.all([api('/api/libraries'), api('/api/library'), api('/api/models'), api('/api/bookmarks')]);
      if (generation !== authGeneration) return;
      state.libraries = libraries.libraries || []; state.rootPath = libraries.root_path || '/library'; if (state.libraryId && !currentLibrary()) { state.libraryId = ''; if (state.view === 'library') state.view = 'libraries'; }
      state.books = (data.books || []).filter(book => book.id !== undefined && book.page_count > 0); state.models = models.models || []; state.bookmarks = bookmarks.bookmarks || []; state.scanning = Boolean(data.scanning); state.lastScan = data.last_scan; state.scanErrors = data.errors || []; state.libraryError = null; state.loaded = true;
    } catch (error) { if (error.status !== 401) state.libraryError = error.message; } finally {
      if (generation !== authGeneration) return;
      state.loading = false;
      if (state.session?.authenticated && renderedLibraryData !== libraryDataSignature()) { renderLibraryNavigation(); renderSeries(); renderLibrary(); }
      clearTimeout(state.poll); if (state.scanning && state.session?.authenticated) state.poll = setTimeout(() => refreshLibrary(), 1500);
    } })();
    state.refreshPromise = operation; try { await operation; } finally { if (state.refreshPromise === operation) state.refreshPromise = null; }
    if (state.session?.authenticated && state.loaded && !routing.restoring) {
      const resolved = Routes.resolve(routing.current, state);
      if (!routing.ready || Routes.hash(resolved.route) !== Routes.hash(routing.current)) {
        if (resolved.unavailable) commitRoute(resolved.route, { replace: true });
        await restoreLocation();
      }
    }
  }
  async function reconnectLibrary() {
    if (!state.session?.authenticated) { try { state.session = await api('/api/session'); if (!state.session.authenticated) { showLogin(); return; } showLibrary(); } catch (error) { if (error.status !== 401) { state.libraryError = error.message; renderNotice(); } return; } } await refreshLibrary();
  }
  function renderLibraryNavigation() {
    $('#library-nav').replaceChildren(...state.libraries.map(library => { const button = node('button', 'library-nav-item'); button.type = 'button'; button.append(node('span', 'library-type-dot', typeLabels[library.media_type] === '写真' ? '◈' : '▤'), node('span', '', library.name)); button.classList.toggle('active', state.view === 'library' && String(library.id) === String(state.libraryId)); if (button.classList.contains('active')) button.setAttribute('aria-current', 'page'); button.addEventListener('click', () => selectLibrary(library.id)); return button; }));
  }
  function selectLibrary(id) { navigate('library', { libraryId: id }); }
  let libraryBrowseInput = null;
  let libraryBrowseRequest = 0;
  let libraryFormGeneration = 0;
  function syncLibraryPathRows() {
    const rows = $$('.library-path-row', $('#library-paths'));
    rows.forEach((row, index) => {
      const input = $('.library-path-input', row); input.id = index ? 'library-path-' + index : 'library-path'; input.setAttribute('aria-label', '媒体目录路径 ' + (index + 1));
      const browse = $('.library-path-browse', row); browse.id = index ? 'browse-directories-' + index : 'browse-directories'; browse.setAttribute('aria-label', '浏览媒体目录 ' + (index + 1));
      const remove = $('.library-path-remove', row); remove.disabled = rows.length === 1; remove.setAttribute('aria-label', '移除媒体目录 ' + (index + 1));
    });
    $('#library-add-path').disabled = rows.length >= 20;
  }
  function addLibraryPath(path = '', focus = false) {
    const row = node('div', 'library-path-row'); const input = node('input', 'library-path-input'); input.required = true; input.value = path; input.placeholder = '/library/写真'; input.autocomplete = 'off';
    const browse = node('button', 'button button-secondary library-path-browse'); browse.type = 'button'; browse.append(icon('folder'), document.createTextNode('浏览')); browse.addEventListener('click', () => { libraryBrowseInput = input; browseDirectory(input.value.trim()); });
    const remove = node('button', 'icon-button library-path-remove'); remove.type = 'button'; remove.append(icon('close')); remove.title = '移除此目录'; remove.addEventListener('click', () => { if ($$('.library-path-row', $('#library-paths')).length < 2) return; if (libraryBrowseInput === input) { libraryBrowseInput = null; libraryBrowseRequest += 1; $('#directory-picker').hidden = true; } row.remove(); syncLibraryPathRows(); });
    row.append(input, browse, remove); $('#library-paths').append(row); syncLibraryPathRows(); if (focus) input.focus();
  }
  function renderLibraryAuthors(selected = '') {
    const select = $('#library-author'); const choices = [node('option', '', '不设置默认模特')]; choices[0].value = '';
    state.models.slice().sort((a, b) => collator.compare(a.name, b.name)).forEach(model => { const option = node('option', '', model.name); option.value = String(model.id); choices.push(option); });
    const create = node('option', '', '＋ 新建模特…'); create.value = '__new__'; choices.push(create); select.replaceChildren(...choices); select.value = String(selected || ''); if (select.selectedIndex < 0) select.value = '';
  }
  function updateLibraryReadingDirection() {
    const select = $('#library-direction');
    if (!select.disabled && ['ltr', 'rtl'].includes(select.value)) select.dataset.pagedDirection = select.value;
    if ($('#library-mode').value === 'webtoon') {
      const option = node('option', '', '从上到下'); option.value = 'ttb'; select.replaceChildren(option); select.value = 'ttb'; select.disabled = true;
    } else {
      if (select.disabled) { const left = node('option', '', '从左到右'); left.value = 'ltr'; const right = node('option', '', '从右到左'); right.value = 'rtl'; select.replaceChildren(left, right); }
      select.disabled = false; select.value = select.dataset.pagedDirection || 'ltr';
    }
  }
  function showLibraryForm(library = null) {
    if (!requireAccess()) return;
    libraryFormGeneration += 1; $('#library-create-author').disabled = false; $('#library-save').disabled = false;
    $('#library-remove').hidden = !library; $('#library-remove').disabled = false;
    const type = library?.media_type || 'photo'; const typeSelect = $('#library-type');
    typeSelect.replaceChildren(new Option('写真', 'photo'));
    if (type !== 'photo' && Object.hasOwn(typeLabels, type)) typeSelect.append(new Option(typeLabels[type] + '（已有类型）', type));
    typeSelect.value = type; typeSelect.disabled = type !== 'photo';
    state.editingLibrary = library?.id || null; $('#library-dialog-title').textContent = library ? '管理写真库' : '创建写真库'; $('#library-name').value = library?.name || ''; $('#library-mode').value = library?.reading_mode || 'single';
    const direction = $('#library-direction'); direction.dataset.pagedDirection = ['ltr', 'rtl'].includes(library?.reading_direction) ? library.reading_direction : 'ltr'; direction.disabled = true; updateLibraryReadingDirection();
    libraryBrowseInput = null; libraryBrowseRequest += 1; $('#library-paths').replaceChildren(); const paths = library?.paths?.length ? library.paths : [library?.path || state.rootPath]; paths.forEach(path => addLibraryPath(path));
    $('#library-excludes').value = (library?.exclude_rules || []).join('\n'); const author = library?.default_author_id || state.models.find(model => model.name.normalize('NFKC').toLocaleLowerCase('zh-CN') === String(library?.default_author || '').normalize('NFKC').toLocaleLowerCase('zh-CN'))?.id || ''; renderLibraryAuthors(author); $('#library-author-new').hidden = true; $('#library-new-author').value = ''; $('#library-author-error').hidden = true;
    $('#library-hide-date').checked = Boolean(library?.hide_date_prefix); $('#library-apply-reading').checked = false; $('#library-apply-reading-field').hidden = !library; $('#library-save').textContent = library ? '保存并扫描' : '创建并扫描'; $('#library-form-error').hidden = true; $('#directory-picker').hidden = true; updateLibraryTypeFields(); if (!$('#library-dialog').open) $('#library-dialog').showModal(); $('#library-name').focus();
  }
  function updateLibraryTypeFields() {
    const photo = $('#library-type').value === 'photo';
    $('#library-hide-date-field').hidden = !photo;
  }
  async function browseDirectory(path) {
    if (!requireAccess()) return;
    const request = ++libraryBrowseRequest;
    $('#directory-picker').hidden = false; $('#directory-list').replaceChildren(node('p', 'field-hint', '正在读取目录…')); $('#directory-error').hidden = true; $('#directory-select').disabled = true; $('#directory-parent').disabled = true; $('#directory-roots').disabled = true;
    try { const data = await api('/api/directories?path=' + encodeURIComponent(path || state.rootPath)); if (request !== libraryBrowseRequest) return; state.directoryPath = data.path; $('#directory-current').textContent = data.path; $('#directory-current').title = data.path; $('#directory-select').disabled = false; $('#directory-parent').disabled = !data.parent; $('#directory-parent').dataset.path = data.parent || '';
      const roots = (data.root_paths || [data.root_path || state.rootPath]).filter(value => typeof value === 'string' && value); const select = $('#directory-roots'); select.replaceChildren(...roots.map(root => { const option = node('option', '', root); option.value = root; return option; })); select.value = data.root_path || roots[0] || ''; select.disabled = false; $('#directory-roots-field').hidden = roots.length < 2;
      const buttons = (data.directories || []).map(directory => { const button = node('button', 'directory-item'); button.type = 'button'; button.append(icon('folder'), node('span', '', directory.name), icon('right')); button.addEventListener('click', () => browseDirectory(directory.path)); return button; }); $('#directory-list').replaceChildren(...(buttons.length ? buttons : [node('p', 'field-hint', '没有子目录')]));
    } catch (error) {
      if (request !== libraryBrowseRequest) return;
      if (path && path !== state.rootPath && [400, 403, 422].includes(error.status)) { await browseDirectory(''); if ($('#library-dialog').open) toast('原目录暂不可访问，请重新选择。'); return; }
      $('#directory-list').replaceChildren(); $('#directory-error').textContent = error.message; $('#directory-error').hidden = false; $('#directory-roots').disabled = false;
    }
  }
  async function scanLibrary() {
    if (!requireAccess('scan_libraries')) return;
    if (!state.libraries.length) { showLibraryForm(); return; } if (state.scanning) return; state.scanning = true; state.libraryError = null; renderLibrary();
    try { await post(state.view === 'library' && state.libraryId ? '/api/libraries/' + encodeURIComponent(state.libraryId) + '/scan' : '/api/scan', {}); await refreshLibrary(); } catch (error) { state.scanning = false; if (error.status !== 401) state.libraryError = error.message; renderLibrary(); }
  }
  function showSettings() { navigate('settings'); }
  async function handleUnavailableBook(book, error) {
    if (![403, 404].includes(error.status) || !['book', 'reader'].includes(routing.current.view) || routing.current.id !== String(book.id)) return false;
    commitRoute({ view: 'libraries' }, { replace: true });
    await restoreLocation(); toast('作品不存在，或当前账号无权访问。'); return true;
  }
  async function showBookDetail(book, options = {}) {
    if (!book) return;
    if (!options.fromRoute && routing.ready) {
      routing.generation += 1; routing.restoring = false;
      commitRoute({ view: 'book', id: String(book.id), from: Routes.base(routing.current) });
    }
    state.detailBook = book; $('#detail-content').replaceChildren(makeLoading('正在打开作品资料…')); if (!$('#book-detail').open) $('#book-detail').showModal();
    try { const latest = await api(bookUrl(book.id)); if (state.detailBook?.id !== book.id) return; state.detailBook = latest; const cached = state.books.find(item => item.id === latest.id); if (cached) Object.assign(cached, latest); renderDetail(latest); }
    catch (error) { if (state.detailBook?.id !== book.id || await handleUnavailableBook(book, error)) return; if (error.status !== 401) $('#detail-content').replaceChildren(makePageError(error.message, () => showBookDetail(book))); }
  }
  function renderDetail(book) {
    const layout = node('div', 'detail-layout'); const poster = makeCover(book, 'detail-poster', false); const info = node('div', 'detail-info');
    const libraryLink = node('button', 'detail-library-link', book.library_name || '写真库'); libraryLink.type = 'button'; libraryLink.id = 'detail-library-link';
    libraryLink.setAttribute('aria-label', `打开写真库 ${book.library_name || ''}`);
    libraryLink.addEventListener('click', () => { $('#book-detail').close(); selectLibrary(book.library_id); });
    info.append(libraryLink, node('h2', '', book.title)); info.lastElementChild.id = 'detail-title';
    const facts = node('div', 'detail-facts'); facts.append(node('span', '', book.release_date || '发布日期未设置'), node('span', '', `${book.page_count} 页`), node('span', '', `${modeLabels[book.reading_mode] || '单页'} · ${book.reading_mode === 'webtoon' ? '从上到下' : book.reading_direction === 'rtl' ? '从右到左' : '从左到右'}`)); info.append(facts);
    info.append(node('p', 'detail-author', `加入时间 · ${formatAddedDate(book.added_at)}`));
    const modelHeading = node('p', 'detail-section-label', '模特'); const chips = node('div', 'model-chips'); (book.models || []).forEach(model => { const button = node('button', 'model-chip'); button.append(modelAvatar(model, 'model-chip-avatar'), node('span', '', model.name)); button.addEventListener('click', () => { $('#book-detail').close(); navigate('model', { modelId: model.id }); }); chips.append(button); }); if (!chips.childElementCount) chips.append(node('span', 'detail-muted', '尚未关联模特')); info.append(modelHeading, chips);
    if (book.author && joinDistinctText([...(book.models || []).map(model => model.name), book.author]) !== joinDistinctText((book.models || []).map(model => model.name))) info.append(node('p', 'detail-author', `作者 · ${book.author}`));
    const actions = node('div', 'detail-actions'); const read = node('button', 'button button-primary', book.progress ? '▶ 继续阅读' : '▶ 开始阅读'); read.id = 'detail-read'; read.addEventListener('click', () => { $('#book-detail').close(); openBook(book); });
    const favorite = node('button', 'button button-secondary', book.favorite ? '♥ 已收藏' : '♡ 收藏'); favorite.id = 'detail-favorite'; favorite.setAttribute('aria-pressed', String(Boolean(book.favorite))); favorite.addEventListener('click', async () => { favorite.disabled = true; try { await toggleFavorite(book); } catch (error) { toast(error.message); favorite.disabled = false; } });
    const edit = node('button', 'button button-secondary', '编辑资料'); edit.id = 'detail-edit'; edit.addEventListener('click', () => showMetadataForm(book)); actions.append(read, favorite); if (can('edit_metadata')) actions.append(edit); info.append(actions);
    const history = node('div', 'detail-history'); history.append(node('h3', '', '阅读记录')); if (book.progress) history.append(node('p', '', `上次读到第 ${book.progress.page + 1} / ${book.page_count} 页`), node('small', '', `最近阅读 · ${formatDate(book.progress.updated_at, true)}`)); else history.append(node('p', 'detail-muted', '还没有阅读记录，翻开后会自动记录位置与时间。')); info.append(history);
    if ((book.bookmarks || []).length) { const saved = node('div', 'detail-bookmarks'); saved.append(node('h3', '', `书签 · ${book.bookmarks.length}`)); book.bookmarks.forEach(bookmark => { const button = node('button', 'detail-bookmark', `第 ${bookmark.page + 1} 页${bookmark.note ? ' · ' + bookmark.note : ''}`); button.addEventListener('click', () => { $('#book-detail').close(); openBook(book, bookmark.page); }); saved.append(button); }); info.append(saved); }
    const path = node('p', 'detail-path', book.path); path.title = book.path; info.append(path); layout.append(poster, info); $('#detail-content').replaceChildren(layout);
  }
  let metadataDirectionValue = '';
  let metadataFormGeneration = 0;
  let metadataCoverUpload = null;
  let metadataCoverGeneration = 0;

  function clearMetadataCoverUpload(resetInput = true) {
    metadataCoverGeneration += 1;
    if (metadataCoverUpload?.url) URL.revokeObjectURL(metadataCoverUpload.url);
    metadataCoverUpload = null;
    if (resetInput) $('#metadata-cover-upload').value = '';
    $('#metadata-save').disabled = false;
  }
  function showMetadataForm(book) {
    if (!requireAccess('edit_metadata')) return;
    metadataFormGeneration += 1;
    clearMetadataCoverUpload();
    $('#metadata-remove').disabled = false;
    $('#metadata-remove').hidden = !can('remove_books');
    $('#metadata-show-new-model').hidden = !can('manage_models');
    state.editingBook = book;
    state.selectedModels = new Set((book.models || []).map(model => String(model.id)));
    $('#metadata-name').value = book.title;
    $('#metadata-release').value = book.release_date || '';
    $('#metadata-mode').value = book.reading_mode_override || '';
    metadataDirectionValue = book.reading_direction_override || '';
    $('#metadata-direction').value = metadataDirectionValue;
    $('#metadata-cover').max = book.page_count;
    $('#metadata-cover').value = (book.cover_page || 0) + 1;
    $('#metadata-cover-hint').textContent = `可选第 1–${book.page_count} 页`;
    $('#metadata-cover-source').value = book.cover_source === 'upload' ? 'upload' : 'page';
    $('#metadata-model-search').value = '';
    $('#metadata-new-model').value = '';
    $('#metadata-new-model-row').hidden = true;
    $('#metadata-show-new-model').setAttribute('aria-expanded', 'false');
    $('#metadata-error').hidden = true;
    renderMetadataModels(); updateMetadataReadingDirection(); updateCoverPreview();
    if (!$('#metadata-dialog').open) $('#metadata-dialog').showModal();
  }
  function renderModelOptions(container, search, selected, onChange) {
    const query = search.trim().normalize('NFKC').toLocaleLowerCase('zh-CN');
    const models = state.models.filter(model => !query || model.name.normalize('NFKC').toLocaleLowerCase('zh-CN').includes(query));
    container.replaceChildren(...models.map(model => {
      const label = node('label', 'model-checkbox'); const input = node('input');
      input.type = 'checkbox'; input.value = model.id; input.checked = selected.has(String(model.id));
      input.addEventListener('change', () => {
        if (input.checked) selected.add(String(model.id)); else selected.delete(String(model.id));
        onChange();
      });
      label.append(input, node('span', '', model.name)); return label;
    }));
    if (!models.length) container.append(node('p', 'field-hint', query ? '未找到模特' : '暂无模特'));
  }
  function renderMetadataModels() {
    renderModelOptions($('#metadata-models'), $('#metadata-model-search').value, state.selectedModels, () => {
      $('#metadata-model-selection').textContent = `已选择 ${state.selectedModels.size} 位模特`;
    });
    $('#metadata-model-selection').textContent = `已选择 ${state.selectedModels.size} 位模特`;
  }
  function updateMetadataReadingDirection() {
    const book = state.editingBook; if (!book) return;
    const library = state.libraries.find(item => String(item.id) === String(book.library_id));
    const mode = $('#metadata-mode').value || library?.reading_mode || book.reading_mode || 'single';
    const direction = $('#metadata-direction');
    if (direction.value !== 'ttb') metadataDirectionValue = direction.value;
    direction.disabled = mode === 'webtoon';
    direction.value = mode === 'webtoon' ? 'ttb' : metadataDirectionValue;
  }
  function updateCoverPreview() {
    const book = state.editingBook; if (!book) return;
    const upload = $('#metadata-cover-source').value === 'upload';
    $('#metadata-cover-page-field').hidden = upload;
    $('#metadata-cover-upload-field').hidden = !upload;
    $('#metadata-cover').required = !upload;
    $('#metadata-cover').disabled = upload;
    const preview = $('#metadata-cover-preview');
    if (upload) {
      const existing = book.cover_source === 'upload';
      const url = metadataCoverUpload?.url || (existing ? `${bookUrl(book.id)}/cover?v=${encodeURIComponent(book.cover_version || book.cover_revision || '')}` : '');
      preview.hidden = !url;
      if (url) preview.src = url; else preview.removeAttribute('src');
      preview.alt = '上传的作品封面预览';
      $('#metadata-upload-status').textContent = metadataCoverUpload ? (metadataCoverUpload.pending ? '正在读取图片…' : `已选择 ${metadataCoverUpload.name}`) : existing ? '已上传封面' : 'JPG / PNG / WebP，最大 6 MB';
    } else {
      const page = Math.max(1, Math.min(book.page_count, Number($('#metadata-cover').value) || 1)) - 1;
      preview.hidden = false;
      preview.src = book.cover_source !== 'upload' && page === (book.cover_page || 0) ? `${bookUrl(book.id)}/cover?v=${encodeURIComponent(book.cover_version || book.cover_revision || '')}` : pageUrl(book.id, page);
      preview.alt = `作品第 ${page + 1} 页封面预览`;
    }
  }
  async function readMetadataCover(file) {
    clearMetadataCoverUpload(false);
    if (!file) { updateCoverPreview(); return; }
    const generation = metadataCoverGeneration;
    $('#metadata-error').hidden = true;
    try {
      if (file.size > 6 * 1024 * 1024) throw new Error('封面图片不能超过 6 MB。');
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) && !(file.type === '' && /\.(jpe?g|png|webp)$/i.test(file.name))) throw new Error('请选择 JPEG、PNG 或 WebP 图片。');
      const selection = { name: file.name, url: URL.createObjectURL(file), data: null, pending: true };
      metadataCoverUpload = selection; $('#metadata-save').disabled = true; updateCoverPreview();
      const read = new Promise((resolve, reject) => {
        const fileReader = new FileReader();
        fileReader.onload = () => resolve(String(fileReader.result).split(',')[1]);
        fileReader.onerror = () => reject(new Error('无法读取封面图片，请重新选择。'));
        fileReader.readAsDataURL(file);
      });
      const decode = new Promise((resolve, reject) => {
        const image = new Image(); image.onload = resolve; image.onerror = () => reject(new Error('图片无法打开，请选择有效的 JPEG、PNG 或 WebP 文件。')); image.src = selection.url;
      });
      const [data] = await Promise.all([read, decode]);
      if (generation !== metadataCoverGeneration || metadataCoverUpload !== selection) return;
      selection.data = data; selection.pending = false; updateCoverPreview();
    } catch (error) {
      if (generation !== metadataCoverGeneration) return;
      clearMetadataCoverUpload(); updateCoverPreview();
      $('#metadata-error').textContent = error.message; $('#metadata-error').hidden = false;
    } finally { if (generation === metadataCoverGeneration) $('#metadata-save').disabled = false; }
  }
  function setModelMutationBusy(busy) {
    modelMutationBusy = busy;
    ['model-name', 'model-avatar-upload', 'model-avatar-reset', 'model-remove', 'model-close'].forEach(id => { $('#' + id).disabled = busy; });
    $('#model-save').disabled = busy || Boolean(avatarEdits.model?.pending || avatarEdits.model?.invalid);
  }
  function showModelForm(model = null) { if (!requireAccess('manage_models') || modelMutationBusy) return; beginAvatarEdit('model', model); setModelMutationBusy(false); state.editingModel = model?.id || null; $('#model-dialog-title').textContent = model ? '编辑模特' : '新建模特'; $('#model-name').value = model?.name || ''; $('#model-remove').hidden = !model; $('#model-error').hidden = true; $('#model-dialog').showModal(); $('#model-name').focus(); }
  function showBookmarkForm() { if (!reader.book || reader.loadedPage === null) { toast('请等当前图片加载完成。'); return; } state.bookmarkTarget = { bookId: reader.book.id, page: reader.index }; $('#bookmark-page-label').textContent = `《${reader.book.title}》 · 第 ${reader.index + 1} 页`; $('#bookmark-note').value = ''; $('#bookmark-error').hidden = true; $('#bookmark-dialog').showModal(); $('#bookmark-note').focus(); }

  function readerMessage(message, type = 'info', actionLabel, action) {
    const element = $('#reader-notice');
    element.replaceChildren(node('span', '', message));
    if (actionLabel && action) {
      const button = node('button', '', actionLabel);
      button.addEventListener('click', action);
      element.append(button);
    }
    element.hidden = false;
    reader.noticeType = type;
  }

  function clearReaderMessage(type) {
    if (type && reader.noticeType !== type) return;
    $('#reader-notice').hidden = true;
    reader.noticeType = '';
  }

  function scheduleReaderChrome() {
    clearTimeout(reader.chromeTimer);
    if (!reader.open || $('#reader').dataset.controls !== 'visible') return;
    reader.chromeTimer = setTimeout(() => {
      const editing = $('.reader-toolbar input:focus, .reader-toolbar select:focus, .reader-footer input:focus');
      if (editing || reader.choice?.active || !$('#reader-menu').hidden || !$('#reader-help-panel').hidden || !$('#chapter-drawer').hidden || $('#bookmark-dialog').open) { scheduleReaderChrome(); return; }
      setReaderChrome(false);
    }, 3200);
  }

  function setReaderChrome(visible, intro = false) {
    clearTimeout(reader.chromeTimer);
    const dialog = $('#reader');
    dialog.dataset.controls = visible ? 'visible' : 'hidden';
    ['.reader-toolbar', '.reader-footer', '.reader-floating'].forEach(selector => {
      const element = $(selector, dialog);
      if (!visible && element.contains(document.activeElement)) $('#reader-viewport').focus({ preventScroll: true });
      element.inert = !visible;
    });
    $('#reader-turn-hint').hidden = !intro || !reader.book;
    if (!visible) { closeReaderChoice(); $('#reader-menu').hidden = true; $('#reader-menu-toggle').setAttribute('aria-expanded', 'false'); }
    if (visible) scheduleReaderChrome();
  }

  // The hidden native selects remain the source of truth for reading settings.
  // A shared listbox keeps the popup outside the scrolling mobile toolbar.
  function setupReaderChoices() {
    const panel = node('div', 'reader-choice-popover'); panel.id = 'reader-choice-popover'; panel.hidden = true; panel.setAttribute('role', 'listbox');
    reader.choice = { panel, active: null, controls: new Map() };
    $('#reader').append(panel);
    [['mode', '阅读方式'], ['direction', '阅读方向'], ['fit', '图片适配']].forEach(([key, label]) => {
      const select = $('#reader-' + key); const wrapper = node('div', 'reader-choice');
      const trigger = node('button', 'reader-choice-trigger'); trigger.type = 'button'; trigger.id = 'reader-' + key + '-trigger';
      const value = node('span', 'reader-choice-value'); const chevron = icon('right'); chevron.classList.add('reader-choice-chevron'); chevron.setAttribute('aria-hidden', 'true');
      trigger.append(value, chevron); trigger.setAttribute('aria-haspopup', 'listbox'); trigger.setAttribute('aria-expanded', 'false'); trigger.setAttribute('aria-controls', panel.id);
      select.closest('label').replaceWith(wrapper); select.hidden = true; select.tabIndex = -1; select.setAttribute('aria-hidden', 'true'); wrapper.append(select, trigger);
      reader.choice.controls.set(key, { select, trigger, value, label });
      trigger.addEventListener('click', () => reader.choice.active === key ? closeReaderChoice(true) : openReaderChoice(key));
      trigger.addEventListener('keydown', event => {
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); event.stopPropagation(); openReaderChoice(key, event.key);
      });
      select.addEventListener('change', syncReaderChoices);
    });
    panel.addEventListener('keydown', event => {
      const options = $$('[role="option"]:not(:disabled)', panel); const current = options.indexOf(document.activeElement);
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); event.stopPropagation();
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
        options[index]?.focus();
      } else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); document.activeElement?.click(); }
      else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeReaderChoice(true); }
      else if (event.key === 'Tab') closeReaderChoice(true);
    });
    $('#reader').addEventListener('pointerdown', event => {
      if (reader.choice.active && !event.target.closest('.reader-choice, .reader-choice-popover')) closeReaderChoice();
    }, true);
    $('#reader').addEventListener('focusin', event => {
      if (reader.choice.active && !event.target.closest('.reader-choice, .reader-choice-popover')) closeReaderChoice();
    });
    window.addEventListener('resize', positionReaderChoice);
    $('.reader-options').addEventListener('scroll', positionReaderChoice, { passive: true });
    syncReaderChoices();
  }
  function syncReaderChoices() {
    if (!reader.choice) return;
    reader.choice.controls.forEach(({ select, trigger, value, label }, key) => {
      value.textContent = select.selectedOptions[0]?.textContent || label;
      trigger.disabled = select.disabled;
      trigger.setAttribute('aria-label', `${label}：${value.textContent}`);
      trigger.title = key === 'direction' && state.prefs.mode === 'webtoon' ? '条漫模式固定从上到下' : label;
      if (select.disabled && reader.choice.active === key) closeReaderChoice();
    });
    if (reader.choice.active) {
      const selected = reader.choice.controls.get(reader.choice.active).select.value;
      $$('[role="option"]', reader.choice.panel).forEach(option => option.setAttribute('aria-selected', String(option.dataset.value === selected)));
    }
  }
  function openReaderChoice(key, edge = '') {
    const control = reader.choice.controls.get(key);
    if (!reader.open || control.select.disabled) return;
    closeReaderChoice(); reader.choice.active = key;
    const panel = reader.choice.panel; control.trigger.setAttribute('aria-expanded', 'true'); panel.setAttribute('aria-labelledby', control.trigger.id);
    const heading = node('span', 'reader-choice-heading', control.label); heading.setAttribute('aria-hidden', 'true');
    panel.replaceChildren(heading, ...Array.from(control.select.options).filter(option => !option.hidden).map(option => {
      const button = node('button', 'reader-choice-option'); button.type = 'button'; button.tabIndex = -1; button.disabled = option.disabled; button.dataset.value = option.value;
      button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(option.selected)); button.append(node('span', '', option.textContent), icon('check'));
      button.addEventListener('click', () => {
        if (control.select.disabled) { closeReaderChoice(); return; }
        control.select.value = option.value; closeReaderChoice(true); control.select.dispatchEvent(new Event('change', { bubbles: true })); scheduleReaderChrome();
      });
      return button;
    }));
    panel.hidden = false; positionReaderChoice(); setReaderChrome(true);
    const options = $$('[role="option"]:not(:disabled)', panel);
    const focused = edge === 'Home' ? options[0] : edge === 'End' ? options.at(-1) : options.find(option => option.getAttribute('aria-selected') === 'true') || (edge === 'ArrowUp' ? options.at(-1) : options[0]);
    focused?.focus({ preventScroll: true });
  }
  function positionReaderChoice() {
    if (!reader.choice?.active) return;
    const panel = reader.choice.panel; const rect = reader.choice.controls.get(reader.choice.active).trigger.getBoundingClientRect();
    const width = document.documentElement.clientWidth; const height = window.innerHeight;
    panel.style.maxHeight = Math.max(80, height - 24) + 'px';
    const left = Math.max(12, Math.min(rect.right - panel.offsetWidth, width - panel.offsetWidth - 12));
    const top = rect.bottom + 8 + panel.offsetHeight <= height - 12 ? rect.bottom + 8 : Math.max(12, rect.top - panel.offsetHeight - 8);
    panel.style.left = left + 'px'; panel.style.top = top + 'px';
  }
  function closeReaderChoice(restoreFocus = false) {
    if (!reader.choice?.active) return;
    const { trigger } = reader.choice.controls.get(reader.choice.active);
    const containedFocus = reader.choice.panel.contains(document.activeElement);
    reader.choice.active = null; reader.choice.panel.hidden = true; trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus && !trigger.disabled && reader.open) trigger.focus({ preventScroll: true });
    else if (containedFocus && reader.open) $('#reader-viewport').focus({ preventScroll: true });
    scheduleReaderChrome();
  }

  function cancelPageTurn() {
    reader.turnAnimations.forEach(animation => animation.cancel());
    reader.turnAnimations = [];
    $$('.page-turn-layer').forEach(element => element.remove());
  }

  function animatePageTurn(wrap, outgoing, side) {
    if (!outgoing || !side || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    outgoing.classList.add('page-turn-layer');
    outgoing.removeAttribute('id');
    outgoing.setAttribute('aria-hidden', 'true');
    $('#paged-view').append(outgoing);
    const options = { duration: 300, easing: 'cubic-bezier(.22,.61,.36,1)' };
    const oldAnimation = outgoing.animate([{ transform: 'translateX(0)' }, { transform: `translateX(${-side * 100}%)` }], options);
    const newAnimation = wrap.animate([{ transform: `translateX(${side * 100}%)` }, { transform: 'translateX(0)' }], options);
    reader.turnAnimations = [oldAnimation, newAnimation];
    Promise.allSettled(reader.turnAnimations.map(animation => animation.finished)).then(() => outgoing.remove());
  }

  function changeReaderSetting(key, value) {
    if (!reader.book || !allowedPrefs[key]?.includes(value)) return;
    if (key === 'direction' && state.prefs.mode === 'webtoon') return;
    const bookId = reader.book.id;
    const field = key === 'mode' ? 'reading_mode' : 'reading_direction';
    const generation = authGeneration;
    savePref(key, value);
    reader.book[field] = value; reader.book.reading_preferences = { ...reader.book.reading_preferences, [field]: value };
    reader.book.reading_direction = state.prefs.mode === 'webtoon' ? 'ttb' : state.prefs.direction;
    const cached = state.books.find(book => book.id === bookId);
    if (cached) { cached[field] = value; cached.reading_preferences = { ...cached.reading_preferences, [field]: value }; cached.reading_direction = reader.book.reading_direction; }
    applyReaderPrefs();
    if (key === 'mode') setReaderMode();
    else if (state.prefs.mode !== 'webtoon') renderPage();
    scheduleReaderChrome();
    // Serialize settings independently from progress so rapid changes end with
    // the last selected value, even when a previous request is still in flight.
    const previous = reader.settingsChains.get(bookId) || Promise.resolve();
    const operation = previous.catch(() => {}).then(async () => {
      if (generation !== authGeneration || !state.session?.authenticated) return;
      try {
        await post(bookUrl(bookId) + '/reading-preferences', { [field]: value }, { keepalive: true });
        if (reader.open && reader.book?.id === bookId) clearReaderMessage('settings-error');
      } catch (error) {
        if (error.status !== 401) {
          if (reader.open && reader.book?.id === bookId) readerMessage('阅读设置未保存：' + error.message, 'settings-error', '重试保存', () => changeReaderSetting(key, value));
          else toast('阅读设置未能保存：' + error.message);
        }
      }
    });
    reader.settingsChains.set(bookId, operation);
    operation.finally(() => { if (reader.settingsChains.get(bookId) === operation) reader.settingsChains.delete(bookId); });
  }

  function applyReaderPrefs() {
    const dialog = $('#reader');
    const viewport = $('#reader-viewport');
    if (viewport.clientHeight) dialog.style.setProperty('--viewport-height', viewport.clientHeight + 'px');
    ['fit', 'theme'].forEach(key => { dialog.dataset[key] = state.prefs[key]; });
    dialog.dataset.mode = readerMode();
    const vertical = state.prefs.mode === 'webtoon';
    dialog.dataset.direction = vertical ? 'ltr' : state.prefs.direction;
    const photo = isPhoto();
    ['reader-chapters', 'reader-menu-chapters', 'chapter-hover-edge'].forEach(id => { $('#' + id).hidden = photo; });
    if (photo) toggleChapters(false);
    $('#reader-mode-hint').textContent = vertical ? '从上到下滚动 · ↑ ↓ 翻页 · ? 帮助' : state.prefs.mode === 'double' && readerMode() === 'single' ? '窄屏自动单页 · 横屏可使用双页' : photo ? '↑ ↓ / ← → 翻页 · ? 帮助' : '↑ ↓ 翻页 · ← → 章节 · ? 帮助';
    $('#reader-help-arrows').textContent = photo ? '上一页 / 下一页，跟随阅读方向' : '上一话 / 下一话，跟随阅读方向';
    ['mode', 'fit'].forEach(key => { $('#reader-' + key).value = state.prefs[key]; });
    $('#reader-mode').disabled = !reader.book;
    $('#reader-fit').disabled = !reader.book;
    $('#reader-direction').disabled = vertical || !reader.book;
    $('#reader-direction-vertical').hidden = !vertical;
    $('#reader-direction').value = vertical ? 'ttb' : state.prefs.direction;
    syncReaderChoices();
    $('#reader-theme').setAttribute('aria-label', `切换为${state.prefs.theme === 'dark' ? '浅' : '深'}色阅读背景`);
    $('#tap-left').setAttribute('aria-label', state.prefs.direction === 'rtl' ? '下一页' : '上一页');
    $('#tap-right').setAttribute('aria-label', state.prefs.direction === 'rtl' ? '上一页' : '下一页');
    $('#previous-page use').setAttribute('href', !vertical && state.prefs.direction === 'rtl' ? '#i-right' : '#i-left');
    $('#next-page use').setAttribute('href', !vertical && state.prefs.direction === 'rtl' ? '#i-left' : '#i-right');
    $('.page-navigation').dir = vertical ? 'ltr' : state.prefs.direction;
    $('#page-slider').dir = vertical ? 'ltr' : state.prefs.direction;
    $('#reader-turn-hint').dataset.side = vertical ? 'center' : state.prefs.direction === 'rtl' ? 'left' : 'right';
    $('#reader-turn-arrow').textContent = vertical ? '↓' : state.prefs.direction === 'rtl' ? '←' : '→';
    $('#reader-turn-text').textContent = vertical ? '上下滚动，继续阅读' : state.prefs.direction === 'rtl' ? '点击左侧，翻到下一页' : '点击右侧，翻到下一页';
  }

  async function openBook(book, startPage = null, sequence = null, options = {}) {
    if (!book) return;
    if (!options.fromRoute && routing.ready) {
      routing.generation += 1; routing.restoring = false;
      let from = routing.current.view === 'reader' ? Routes.returnTo(routing.current) : routing.current;
      if (routing.current.view === 'reader' && from.view === 'book') from = { ...from, id: String(book.id) };
      commitRoute({ view: 'reader', id: String(book.id), page: startPage === null ? undefined : Number(startPage) + 1, from });
    }
    if ($('#book-detail').open) $('#book-detail').close();
    hideReaderEnd(false);
    reader.endSequence = sequence?.libraryId === String(book.library_id) ? sequence : End.readingSequence(state.books, book, librarySort(book.library_id));
    reader.endBoundaryAt = 0; reader.lastWheelAt = 0;
    flushProgress(true);
    const queuedSave = Promise.allSettled([reader.saveChains.get(book.id), reader.settingsChains.get(book.id)].filter(Boolean));
    cancelPageTurn();
    clearTimeout(reader.chromeTimer);
    disconnectScroll();
    clearReaderImages();
    reader.token += 1;
    const token = reader.token;
    reader.open = true;
    reader.book = null;
    reader.loadedPage = null;
    reader.lastVisible = null;
    reader.restoring = null;
    clearReaderMessage();
    $('#reader-title').textContent = book.title;
    $('#reader-series').textContent = book.series || '独立作品';
    $('#page-image-wrap').replaceChildren(makeLoading('正在打开作品…'));
    $('#paged-view').hidden = false;
    $('#scroll-view').hidden = true;
    $('#page-input').value = '';
    $('#page-total').textContent = '/ —';
    $('#page-slider').disabled = true;
    $('#previous-page').disabled = true;
    $('#next-page').disabled = true;
    $('#save-status').textContent = '进度自动保存';
    applyReaderPrefs();
    if (!$('#reader').open) $('#reader').showModal();
    setReaderChrome(true);
    document.body.style.overflow = 'hidden';
    try {
      if (queuedSave) await queuedSave;
      if (!reader.open || token !== reader.token) return;
      const latest = await api(bookUrl(book.id));
      if (!reader.open || token !== reader.token) return;
      if (!latest.page_count) throw new ApiError('这个文件夹里暂时没有可阅读的图片。', 404);
      reader.book = latest;
      if (reader.endSequence?.libraryId !== String(latest.library_id)) reader.endSequence = End.readingSequence(state.books, latest, librarySort(latest.library_id));
      const library = state.libraries.find(item => String(item.id) === String(latest.library_id));
      state.prefs.mode = latest.reading_mode || library?.reading_mode || 'single';
      state.prefs.direction = Users.readingDirection(latest, library);
      reader.sizes = Array.from({ length: latest.page_count }, () => ({}));
      reader.coverAlone = true; reader.offset = false;
      $('#cover-alone').checked = true; $('#spread-offset').checked = false;
      $('#reader-menu').hidden = true; $('#reader-menu-toggle').setAttribute('aria-expanded', 'false');
      toggleChapters(false); $('#reader-help-panel').hidden = true;
      applyReaderPrefs(); renderChapters();
      const cached = state.books.find(item => item.id === latest.id);
      if (cached) Object.assign(cached, latest);
      reader.index = Math.min(latest.page_count - 1, Math.max(0, startPage === null ? latest.progress?.page || 0 : Number(startPage) || 0));
      $('#reader-title').textContent = latest.title;
      $('#reader-series').textContent = joinDistinctText([latest.library_name || library?.name, ...(isPhoto(latest) ? (latest.models || []).map(model => model.name) : [latest.series]), latest.author]);
      $('#page-slider').disabled = false;
      $('#page-slider').max = latest.page_count - 1;
      $('#page-input').max = latest.page_count;
      setReaderMode();
      updateReaderPosition();
      $('#reader-viewport').focus({ preventScroll: true });
      setReaderChrome(true, true);
    } catch (error) {
      if (token !== reader.token || !reader.open) return;
      if (await handleUnavailableBook(book, error)) return;
      if (error.status !== 401) {
        $('#page-image-wrap').replaceChildren(makePageError(error.message, () => openBook(book)));
        readerMessage(error.message, 'error', '重新打开', () => openBook(book));
      }
    }
  }

  function makeLoading(text) {
    const loading = node('div', 'page-loading');
    loading.setAttribute('role', 'status');
    loading.append(node('span', 'spinner'), node('span', '', text));
    return loading;
  }

  function makePageError(text, retry) {
    const error = node('div', 'page-error');
    error.setAttribute('role', 'alert');
    const button = node('button', 'button button-secondary', '重新加载');
    button.addEventListener('click', event => { event.stopPropagation(); retry(); });
    error.append(icon('book'), node('span', '', text), button);
    return error;
  }

  function updateReaderPosition() {
    if (!reader.book) return;
    if (routing.ready && routing.current.view === 'reader' && routing.current.id === String(reader.book.id)) commitRoute({ ...routing.current, page: reader.index + 1 }, { replace: true });
    const spread = state.prefs.mode === 'webtoon' ? [reader.index] : currentSpread();
    $('#page-input').value = spread[0] + 1;
    $('#page-total').textContent = (spread.length > 1 ? '–' + (spread[spread.length - 1] + 1) : '') + ' / ' + reader.book.page_count;
    $('#floating-page').textContent = spread.map(index => index + 1).join('–') + ' / ' + reader.book.page_count;
    $('#page-slider').value = reader.index;
    $('#page-slider').setAttribute('aria-valuetext', `第 ${reader.index + 1} 页，共 ${reader.book.page_count} 页`);
    $('#page-slider').style.setProperty('--page-progress', `${reader.book.page_count > 1 ? reader.index / (reader.book.page_count - 1) * 100 : 100}%`);
    $('#previous-page').disabled = reader.index <= 0;
    // The final page keeps its forward control so another click can open the end panel.
    $('#next-page').disabled = false;
    if (reader.index < reader.book.page_count - 1) clearReaderMessage('complete');
  }

  function disconnectScroll() {
    reader.observer?.disconnect();
    reader.observer = null;
    reader.visible.clear();
    cancelAnimationFrame(reader.frame);
    reader.frame = 0;
  }

  function readerMode() {
    const viewport = $('#reader-viewport');
    return Model.effectiveMode(state.prefs.mode, viewport.clientWidth || window.innerWidth, viewport.clientHeight || window.innerHeight);
  }

  function rebuildSpreads() {
    reader.effective = readerMode();
    reader.spreads = reader.effective === 'double' ? Model.buildSpreads(reader.sizes, { coverAlone: reader.coverAlone, offset: reader.offset }) : Array.from({ length: reader.book.page_count }, (_, index) => [index]);
  }

  function currentSpread() {
    if (!reader.book) return [];
    return reader.spreads[Model.spreadForPage(reader.spreads, reader.index)] || [reader.index];
  }

  function setReaderMode() {
    if (!reader.book) return;
    disconnectScroll();
    clearReaderImages();
    cancelPageTurn();
    reader.scrollJumpBehavior = 'instant';
    reader.epoch += 1;
    reader.loadedPage = null;
    rebuildSpreads(); applyReaderPrefs();
    const scroll = state.prefs.mode === 'webtoon';
    $('#paged-view').hidden = scroll;
    $('#scroll-view').hidden = !scroll;
    $('#reader-viewport').scrollTop = 0;
    if (scroll) buildScrollView();
    else { $('#scroll-view').replaceChildren(); reader.rows = []; renderPage(); }
    updateReaderPosition();
  }

  async function renderPage(turnSide = 0) {
    if (!reader.book || !reader.open) return;
    const token = reader.token;
    const epoch = ++reader.epoch;
    const book = reader.book;
    const spread = currentSpread();
    const wrap = $('#page-image-wrap');
    cancelPageTurn();
    const outgoing = turnSide && $('img', wrap) ? wrap.cloneNode(true) : null;
    reader.loadedPage = null;
    if (!outgoing) wrap.replaceChildren(makeLoading('正在加载第 ' + spread.map(index => index + 1).join('、') + ' 页'));
    $('#reader-viewport').scrollTop = 0;
    try {
      const pendingImages = spread.map(index => loadReaderImage(index));
      reader.pageCache.setLimit(Performance.get().imageCacheLimit, [...spread, ...$$('img', wrap).map(image => Number(image.dataset.page))]);
      const images = await Promise.all(pendingImages);
      if (!reader.open || token !== reader.token || epoch !== reader.epoch || state.prefs.mode === 'webtoon') return;
      rebuildSpreads();
      const nextSpread = currentSpread();
      // Once dimensions are known, a wide original stays intact on its own spread.
      if (nextSpread.join(',') !== spread.join(',')) { reader.index = nextSpread[0]; updateReaderPosition(); renderPage(turnSide); return; }
      reader.index = spread[0];
      wrap.dataset.pages = spread.length;
      const byPage = new Map(images.map(image => [Number(image.dataset.page), image]));
      wrap.replaceChildren(...Model.visualPages(spread, state.prefs.direction).map(index => byPage.get(index)));
      animatePageTurn(wrap, outgoing, turnSide);
      reader.loadedPage = Math.max(...spread);
      updateReaderPosition();
      markVisiblePage(reader.loadedPage);
      preloadNeighbors(reader.loadedPage);
    } catch (error) {
      if (!reader.open || token !== reader.token || epoch !== reader.epoch) return;
      wrap.replaceChildren(makePageError(error.message, renderPage));
    }
  }

  function preloadNeighbors(index) {
    if (!reader.book || state.prefs.mode === 'webtoon') return;
    const protectedPages = currentSpread(); const limit = Performance.get().imageCacheLimit;
    const candidates = [Math.max(...protectedPages) + 1, Math.min(...protectedPages) - 1].filter(value => value >= 0 && value < reader.book.page_count && !protectedPages.includes(value));
    reader.preloads = candidates.slice(0, limit ? Math.max(0, limit - protectedPages.length) : candidates.length);
    reader.preloads.forEach(value => loadReaderImage(value).catch(() => {}));
    reader.pageCache.setLimit(limit, protectedPages);
  }

  function clearReaderImages() {
    reader.pageCache.clear(); reader.preloads = [];
    reader.rows.forEach(row => row.cancel?.());
  }

  function loadReaderImage(index) {
    const existing = reader.pageCache.get(index); if (existing) return existing.promise;
    const book = reader.book; const image = node('img');
    image.alt = `${book.title}，第 ${index + 1} 页`; image.dataset.page = index; image.decoding = 'async'; image.draggable = false;
    const entry = { image, promise: null, cancel: null };
    entry.promise = new Promise((resolve, reject) => {
      let settled = false;
      entry.cancel = () => { image.onload = null; image.onerror = null; image.removeAttribute('src'); if (!settled) { settled = true; reject(new Error('图片加载已取消。')); } };
      image.onload = () => { settled = true; if (reader.book?.id === book.id) reader.sizes[index] = { width: image.naturalWidth, height: image.naturalHeight }; resolve(image); };
      image.onerror = () => { settled = true; if (reader.pageCache.peek(index) === entry) reader.pageCache.delete(index); reject(new Error(`第 ${index + 1} 页加载失败。请检查网络，或确认图片文件仍在写真库中。`)); };
      image.src = pageUrl(book.id, index);
    });
    reader.pageCache.set(index, entry); return entry.promise;
  }

  function scrollProtectedPages() {
    return new Set([...reader.visible, reader.index, ...(reader.restoring === null ? [] : [reader.restoring])]);
  }

  function trimScrollImages() {
    const limit = Performance.get().imageCacheLimit; if (!limit) return;
    const protectedPages = scrollProtectedPages(); const retained = reader.rows.map((row, index) => ({ row, index })).filter(item => item.row.loaded || item.row.loading);
    let count = retained.length;
    retained.sort((a, b) => Math.abs(b.index - reader.index) - Math.abs(a.index - reader.index)).forEach(({ row, index }) => {
      if (count <= limit || protectedPages.has(index)) return;
      const image = $('img', row.element);
      if (image?.naturalWidth && image.naturalHeight) row.ratio = image.naturalWidth / image.naturalHeight;
      row.cancel?.(); row.loaded = false; row.loading = false; row.failed = false; row.element.classList.remove('loaded');
      if (row.ratio) row.element.style.aspectRatio = String(row.ratio);
      row.element.replaceChildren(node('span', 'scroll-placeholder', `第 ${index + 1} 页`)); count -= 1;
    });
  }

  function loadScrollBuffer(index) {
    const step = Performance.get().webtoonStep; const limit = Performance.get().imageCacheLimit;
    const protectedPages = scrollProtectedPages();
    const candidates = [];
    for (let distance = 1; distance <= step; distance += 1) candidates.push(index + distance, index - distance);
    const available = limit ? Math.max(0, limit - protectedPages.size) : candidates.length;
    candidates.filter(value => value >= 0 && value < reader.rows.length && !protectedPages.has(value)).slice(0, available).forEach(value => loadScrollPage(value));
    trimScrollImages();
  }

  function buildScrollView() {
    const container = $('#scroll-view');
    container.replaceChildren();
    const fragment = document.createDocumentFragment();
    reader.rows = [];
    const token = reader.token;
    const epoch = reader.epoch;
    for (let index = 0; index < reader.book.page_count; index += 1) {
      const row = node('div', 'scroll-page');
      row.dataset.index = index;
      row.setAttribute('aria-label', `第 ${index + 1} 页`);
      row.append(node('span', 'scroll-placeholder', `第 ${index + 1} 页`));
      reader.rows.push({ element: row, loaded: false, loading: false, failed: false });
      fragment.append(row);
    }
    container.append(fragment);
    reader.restoring = reader.index;
    reader.observer = new IntersectionObserver(entries => {
      if (!reader.open || token !== reader.token || epoch !== reader.epoch) return;
      entries.forEach(entry => {
        const index = Number(entry.target.dataset.index);
        if (entry.isIntersecting) {
          reader.visible.add(index);
          loadScrollPage(index);
        } else reader.visible.delete(index);
      });
      scheduleVisibleScroll();
    }, { root: $('#reader-viewport'), threshold: [0, .1, .25, .5, .75, 1] });
    reader.rows.forEach(row => reader.observer.observe(row.element));
    const target = reader.index;
    reader.rows[target].element.scrollIntoView({ block: 'start', behavior: 'instant' });
    loadScrollPage(target);
    loadScrollBuffer(target);
  }

  function loadScrollPage(index, retry = false) {
    const row = reader.rows[index];
    if (!row || row.loaded || row.loading || (row.failed && !retry)) return;
    const token = reader.token;
    const epoch = reader.epoch;
    const book = reader.book;
    row.loading = true;
    row.failed = false;
    row.element.replaceChildren(node('span', 'scroll-placeholder', `正在加载第 ${index + 1} 页…`));
    const image = node('img');
    image.alt = `${book.title}，第 ${index + 1} 页`;
    image.decoding = 'async';
    image.draggable = false;
    const onLoad = () => {
      if (!reader.open || token !== reader.token || epoch !== reader.epoch || reader.rows[index] !== row || !row.loading) return;
      row.loaded = true;
      row.loading = false;
      row.ratio = image.naturalWidth / image.naturalHeight;
      row.element.style.aspectRatio = '';
      row.element.classList.add('loaded');
      row.element.replaceChildren(image);
      if (reader.restoring === index) {
        row.element.scrollIntoView({ block: 'start', behavior: reader.scrollJumpBehavior });
        reader.restoring = null;
      }
      trimScrollImages();
      scheduleVisibleScroll();
    };
    const onError = () => {
      if (!reader.open || token !== reader.token || epoch !== reader.epoch || reader.rows[index] !== row || !row.loading) return;
      row.loading = false;
      row.failed = true;
      row.element.replaceChildren(makePageError(`第 ${index + 1} 页加载失败`, () => loadScrollPage(index, true)));
    };
    image.addEventListener('load', onLoad, { once: true });
    image.addEventListener('error', onError, { once: true });
    row.cancel = () => { image.removeEventListener('load', onLoad); image.removeEventListener('error', onError); image.removeAttribute('src'); row.loading = false; };
    image.src = pageUrl(book.id, index);
  }

  function scheduleVisibleScroll() {
    if (reader.frame || !reader.open || state.prefs.mode !== 'webtoon') return;
    reader.frame = requestAnimationFrame(() => { reader.frame = 0; chooseVisibleScroll(); syncReaderEndBoundary(); });
  }

  function chooseVisibleScroll() {
    if (!reader.book || !reader.open || state.prefs.mode !== 'webtoon') return;
    const viewport = $('#reader-viewport').getBoundingClientRect();
    const readingLine = viewport.top + Math.min(viewport.height * .35, 220);
    let current = null;
    let distance = Infinity;
    // Only pages the IntersectionObserver reports as visible can affect progress.
    reader.visible.forEach(index => {
      const row = reader.rows[index];
      const rect = row.element.getBoundingClientRect();
      if (rect.bottom <= viewport.top || rect.top >= viewport.bottom) return;
      const delta = rect.top <= readingLine && rect.bottom >= readingLine ? 0 : Math.min(Math.abs(rect.top - readingLine), Math.abs(rect.bottom - readingLine));
      if (delta < distance) { distance = delta; current = index; }
    });
    if (current === null || (reader.restoring !== null && current !== reader.restoring)) return;
    if (reader.index !== current) { reader.index = current; updateReaderPosition(); }
    loadScrollBuffer(current);
    if (reader.rows[current].loaded) {
      reader.loadedPage = current;
      markVisiblePage(current);
    }
  }

  function setReaderPage(value) {
    if (!reader.book || !reader.open) return;
    const index = Math.min(reader.book.page_count - 1, Math.max(0, Math.round(Number(value)) || 0));
    if (index === reader.index && state.prefs.mode !== 'webtoon') return;
    const turnSide = Math.sign(index - reader.index) * (state.prefs.direction === 'rtl' ? -1 : 1);
    reader.index = index;
    updateReaderPosition();
    if (state.prefs.mode !== 'webtoon') renderPage(turnSide);
    else {
      reader.scrollJumpBehavior = 'smooth';
      reader.restoring = index;
      reader.rows[index].element.scrollIntoView({ block: 'start', behavior: 'smooth' });
      loadScrollPage(index);
      loadScrollBuffer(index);
      if (reader.rows[index].loaded) { reader.restoring = null; scheduleVisibleScroll(); }
    }
  }

  function canFinishReader() {
    if (!reader.open || !reader.book || reader.endOverlay) return false;
    const vertical = state.prefs.mode === 'webtoon';
    return End.canFinish({
      mode: state.prefs.mode, pageCount: reader.book.page_count,
      loadedPage: vertical ? (reader.rows[reader.book.page_count - 1]?.loaded ? reader.book.page_count - 1 : null) : reader.loadedPage,
      pages: vertical ? [] : currentSpread(), restoring: reader.restoring,
      atBottom: vertical && End.atScrollBottom($('#reader-viewport'))
    });
  }

  function syncReaderEndBoundary() {
    if (canFinishReader()) reader.endBoundaryAt ||= Date.now();
    else reader.endBoundaryAt = 0;
  }

  function hideReaderEnd(restoreFocus = true) {
    const overlay = reader.endOverlay;
    if (!overlay) return;
    reader.endOverlay = null;
    overlay.element.remove();
    overlay.siblings.forEach(([element, wasInert]) => { if (element.isConnected) element.inert = wasInert; });
    $('#reader').removeAttribute('data-end-open');
    if (restoreFocus && reader.open) {
      setReaderChrome(true);
      $('#reader-viewport').focus({ preventScroll: true });
    }
    reader.endBoundaryAt = 0;
    reader.lastWheelAt = Date.now();
  }

  function showReaderEnd() {
    if (!canFinishReader()) return false;
    const book = reader.book;
    if (state.prefs.mode === 'webtoon') {
      reader.index = book.page_count - 1; reader.loadedPage = reader.index;
      updateReaderPosition(); markVisiblePage(reader.index);
    }
    const sequence = reader.endSequence || End.readingSequence(state.books, book, librarySort(book.library_id));
    const next = End.nextInSequence(state.books, book, sequence);
    const overlay = node('section', 'reader-end-overlay'); overlay.id = 'reader-end';
    overlay.setAttribute('role', 'dialog'); overlay.setAttribute('aria-modal', 'true'); overlay.setAttribute('aria-labelledby', 'reader-end-title');
    const panel = node('div', 'reader-end-panel');
    const heading = node('header', 'reader-end-heading');
    const title = node('h2', '', book.title); title.id = 'reader-end-title';
    heading.append(node('p', '', '已到作品最后一页'), title);
    const actions = node('div', 'reader-end-actions');
    const primary = node('div', 'reader-end-action-row'); const secondary = node('div', 'reader-end-action-row secondary');
    const action = (name, text, handler, className = '') => {
      const button = node('button', 'reader-end-action ' + className); button.type = 'button';
      button.append(icon(name), node('span', '', text)); button.addEventListener('click', handler); return button;
    };
    const favorite = action('heart', book.favorite ? '取消收藏' : '收藏作品', async () => {
      favorite.disabled = true;
      try { await toggleFavorite(book); updateFavorite(); }
      catch (error) { if (error.status !== 401) showError(error.message); }
      finally { favorite.disabled = false; }
    });
    favorite.id = 'reader-end-favorite';
    const updateFavorite = () => { favorite.setAttribute('aria-pressed', String(Boolean(book.favorite))); $('span', favorite).textContent = book.favorite ? '取消收藏' : '收藏作品'; };
    updateFavorite();
    const nextButton = action('arrow', '阅读下一本', async () => {
      if (nextButton.disabled) return;
      const token = reader.token;
      nextButton.disabled = true;
      error.hidden = true;
      await Promise.allSettled([flushProgress(true), ...reader.saveChains.values(), ...reader.settingsChains.values()]);
      if (!reader.open || reader.token !== token || reader.endOverlay?.element !== overlay) return;
      if (reader.pending?.bookId === book.id || reader.noticeType === 'settings-error') {
        showError('阅读记录或设置尚未保存，请检查连接后重试。');
        nextButton.disabled = false; return;
      }
      const target = End.nextInSequence(state.books, book, sequence);
      if (!target) { note.textContent = '已是当前排列中的最后一本。'; return; }
      hideReaderEnd(false);
      openBook(target, 0, sequence);
    }, 'next');
    nextButton.id = 'reader-end-next'; nextButton.disabled = !next;
    const last = action('left', '返回最后一页', () => hideReaderEnd()); last.id = 'reader-end-last';
    const first = action('book', '返回第一页', () => { hideReaderEnd(); setReaderPage(0); }); first.id = 'reader-end-first';
    const library = action('folder', '返回写真库', () => { closeReader(true, { fromRoute: true }); if ($('#book-detail').open) $('#book-detail').close(); selectLibrary(book.library_id); }); library.id = 'reader-end-library';
    const detail = action('book', '返回作品详情', () => { closeReader(true, { fromRoute: true }); showBookDetail(book); }); detail.id = 'reader-end-detail';
    primary.append(favorite, nextButton); secondary.append(last, first, library, detail); actions.append(primary, secondary);
    const note = node('p', 'reader-end-next-note', next ? `下一本 · ${next.title}` : '已是当前排列中的最后一本。'); note.id = 'reader-end-next-note';
    const error = node('p', 'reader-end-error'); error.hidden = true; error.setAttribute('role', 'alert');
    const showError = message => { error.textContent = message; error.hidden = false; };
    const related = node('section', 'reader-end-recommendations'); related.setAttribute('aria-label', '同写真库作品推荐');
    related.append(node('h3', '', '作品推荐'), node('p', '', `来自 ${book.library_name || '同一写真库'}`));
    const grid = node('div', 'reader-end-recommendation-grid');
    const suggestions = End.recommendations(state.books, book, sequence.sort);
    suggestions.forEach(item => {
      const button = node('button', 'reader-end-recommendation'); button.type = 'button'; button.dataset.bookId = String(item.id);
      button.setAttribute('aria-label', `查看推荐作品《${item.title}》详情`);
      button.append(makeCover(item, 'reader-end-cover'), node('strong', '', item.title), node('small', '', `${item.page_count} 页`));
      button.addEventListener('click', () => { closeReader(true, { fromRoute: true }); showBookDetail(item); }); grid.append(button);
    });
    related.append(suggestions.length ? grid : node('p', 'reader-end-empty', '这个写真库暂时没有其他作品。'));
    panel.append(heading, actions, note, error, related); overlay.append(panel);
    setReaderChrome(false); toggleChapters(false); $('#reader-help-panel').hidden = true; clearReaderMessage('complete');
    const shell = $('.reader-shell');
    const siblings = Array.from(shell.children).map(element => [element, element.inert]);
    siblings.forEach(([element]) => { element.inert = true; });
    reader.endOverlay = { element: overlay, siblings };
    shell.append(overlay); $('#reader').dataset.endOpen = 'true';
    overlay.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); hideReaderEnd(); return; }
      if (event.key === 'Tab') {
        const buttons = $$('button:not(:disabled)', overlay); const first = buttons[0]; const lastButton = buttons.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); lastButton?.focus(); }
        else if (!event.shiftKey && document.activeElement === lastButton) { event.preventDefault(); first?.focus(); }
      }
      // Keep reader shortcuts from turning pages behind this modal.
      event.stopPropagation();
    });
    last.focus({ preventScroll: true });
    flushProgress(true);
    return true;
  }

  function scrollReader(delta, allowFinish = true) {
    if (delta > 0 && allowFinish && showReaderEnd()) return;
    $('#reader-viewport').scrollBy({ top: delta * $('#reader-viewport').clientHeight * .8, behavior: 'smooth' });
  }

  function movePage(delta) {
    if (!reader.book || reader.endOverlay) return;
    if (delta > 0 && showReaderEnd()) return;
    if (state.prefs.mode === 'webtoon') { setReaderPage(reader.index + delta); return; }
    const position = Model.spreadForPage(reader.spreads, reader.index);
    const target = Math.min(reader.spreads.length - 1, Math.max(0, position + delta));
    setReaderPage(reader.spreads[target][0]);
  }
  function physicalPage(side) { movePage(side * (state.prefs.mode !== 'webtoon' && state.prefs.direction === 'rtl' ? -1 : 1)); }

  function chapterBooks() {
    if (!reader.book || isPhoto()) return [];
    const parentPath = book => { const path = String(book.path || '').replaceAll('\\', '/'); return path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''; };
    const parent = parentPath(reader.book);
    return state.books.filter(book => String(book.library_id) === String(reader.book.library_id) && parentPath(book) === parent).sort((a, b) => collator.compare(a.path || a.title, b.path || b.title));
  }

  function moveChapter(delta) {
    if (!reader.book || isPhoto()) return;
    const chapters = chapterBooks();
    const current = chapters.findIndex(book => book.id === reader.book.id);
    const next = chapters[current + delta];
    if (next) openBook(next);
    else readerMessage(delta > 0 ? '已经是最后一话。' : '已经是第一话。', 'info');
  }

  function renderChapters(requestedPage = null) {
    const chapters = chapterBooks();
    $('#chapter-context').textContent = reader.book?.series || reader.book?.title || '';
    const size = Performance.get().chapterPageSize || Math.max(5, Math.floor((window.innerHeight - 210) / 54));
    const signature = JSON.stringify([reader.book?.id, chapters.map(book => book.id), size]);
    if (chapterPaging.signature !== signature) { chapterPaging.signature = signature; chapterPaging.page = Math.floor(Math.max(0, chapters.findIndex(book => book.id === reader.book?.id)) / size); }
    if (requestedPage !== null) chapterPaging.page = requestedPage;
    const page = Performance.paginate(chapters.length, chapterPaging.page, size); chapterPaging.page = page.page;
    $('#chapter-list').replaceChildren(...chapters.slice(page.start, page.end).map((book, offset) => {
      const index = page.start + offset;
      const button = node('button', 'chapter-item'); button.type = 'button';
      button.classList.toggle('active', book.id === reader.book.id);
      if (book.id === reader.book.id) button.setAttribute('aria-current', 'page');
      button.append(node('span', 'chapter-order', String(index + 1).padStart(2, '0')), node('span', '', book.title), node('small', '', `${book.page_count} 页`));
      button.addEventListener('click', () => { toggleChapters(false); if (book.id !== reader.book.id) openBook(book); });
      return button;
    }));
    let pagination = $('#chapter-pagination'); if (!pagination) { pagination = node('nav', 'page-pagination chapter-pagination'); pagination.id = 'chapter-pagination'; pagination.setAttribute('aria-label', '章节分页'); $('#chapter-list').after(pagination); }
    renderPagination(pagination, page, value => renderChapters(value));
  }

  function toggleChapters(value = $('#chapter-drawer').hidden) {
    if (isPhoto()) value = false;
    $('#chapter-drawer').hidden = !value;
    $('#reader-chapters').setAttribute('aria-expanded', String(value));
    if (value) $('#chapter-list .active')?.scrollIntoView({ block: 'nearest' });
  }

  function toggleHelp() { $('#reader-help-panel').hidden = !$('#reader-help-panel').hidden; }

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if ($('#reader').requestFullscreen) await $('#reader').requestFullscreen();
      else readerMessage('当前浏览器不支持全屏，请使用浏览器的全屏功能。', 'info');
    } catch (_) { readerMessage('暂时无法进入全屏，请使用浏览器的全屏功能。', 'info'); }
  }

  function markVisiblePage(index) {
    if (!reader.book || !reader.open || document.visibilityState === 'hidden') return;
    const completed = !isPhoto() && index === reader.book.page_count - 1;
    // The first visible page of every visit refreshes its reading timestamp,
    // including a return to the same saved page on another device.
    const alreadyCaptured = reader.lastVisible?.bookId === reader.book.id
      && reader.lastVisible.page === index && reader.lastVisible.completed === completed;
    if (!alreadyCaptured) {
      reader.pending = { bookId: reader.book.id, page: index, completed, seq: ++reader.saveSeq, accountGeneration: authGeneration };
      reader.lastVisible = { bookId: reader.book.id, page: index, completed };
      $('#save-status').textContent = '等待保存…';
      clearTimeout(reader.saveTimer);
      reader.saveTimer = setTimeout(() => flushProgress(), 550);
    }
  }

  function flushProgress(keepalive = false) {
    clearTimeout(reader.saveTimer);
    const item = reader.pending;
    if (!item) return Promise.resolve();
    reader.pending = null;
    const predecessor = reader.saveChains.get(item.bookId) || Promise.resolve();
    const operation = predecessor.catch(() => {}).then(async () => {
      if (item.accountGeneration !== authGeneration || !state.session?.authenticated) return;
      if (reader.open && reader.book?.id === item.bookId) $('#save-status').textContent = '正在保存…';
      try {
        const data = await post(bookUrl(item.bookId) + '/progress', { page: item.page, completed: item.completed }, { keepalive });
        const progress = data.progress || { page: item.page, completed: item.completed, updated_at: Date.now() / 1000 };
        const cached = state.books.find(book => book.id === item.bookId);
        if (cached) cached.progress = progress;
        if (reader.book?.id === item.bookId) reader.book.progress = progress;
        if (reader.open && reader.book?.id === item.bookId && !reader.pending) {
          $('#save-status').textContent = '进度已保存';
          clearReaderMessage('error');
        }
        if (!reader.open) renderLibrary();
      } catch (error) {
        if (error.status !== 401) {
          if (!reader.pending || reader.pending.seq < item.seq) reader.pending = item;
          if (reader.open && reader.book?.id === item.bookId) {
            $('#save-status').textContent = '进度未保存';
            readerMessage('进度保存失败：' + error.message, 'error', '重试保存', () => flushProgress(true));
          } else toast('进度未能保存：' + error.message);
        }
      }
    });
    reader.saveChains.set(item.bookId, operation);
    operation.finally(() => { if (reader.saveChains.get(item.bookId) === operation) reader.saveChains.delete(item.bookId); });
    return operation;
  }

  function closeReader(save = true, options = {}) {
    closeReaderChoice();
    hideReaderEnd(false);
    const saved = save ? Promise.allSettled([flushProgress(true), ...reader.saveChains.values(), ...reader.settingsChains.values()]) : Promise.resolve();
    const closingBookId = reader.book?.id;
    if (document.fullscreenElement === $('#reader')) document.exitFullscreen().catch(() => {});
    reader.open = false;
    reader.token += 1;
    reader.epoch += 1;
    clearTimeout(reader.chromeTimer);
    cancelPageTurn();
    disconnectScroll();
    clearReaderImages();
    reader.rows = [];
    $('#page-image-wrap').replaceChildren();
    $('#scroll-view').replaceChildren();
    if ($('#reader').open) $('#reader').close();
    document.body.style.overflow = '';
    renderLibrary();
    if (save) {
      const returnTo = $$('.book-open').find(element => element.dataset.bookId === String(closingBookId));
      (returnTo || $('#search')).focus({ preventScroll: true });
    }
    if (save) saved.then(() => { if (!reader.open && state.session?.authenticated) refreshLibrary(); });
    if (save && !options.fromRoute && routing.current.view === 'reader') returnFromOverlay();
  }

  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#login-submit');
    button.disabled = true;
    $('#login-error').hidden = true;
    try {
      if (!state.session) { state.session = await api('/api/session'); showLogin(); return; }
      const registering = Boolean(state.session?.registration_required);
      if (registering && $('#password').value !== $('#password-confirm').value) throw new Error('两次输入的密码不一致。');
      const payload = { username: $('#username').value.trim(), password: $('#password').value };
      if (registering) {
        if (state.session?.password_required) payload.setup_password = $('#setup-password').value;
      }
      await post(registering ? '/api/register' : '/api/login', payload);
      state.session = await api('/api/session');
      ['password', 'password-confirm', 'setup-password'].forEach(id => { $('#' + id).value = ''; });
      showLibrary();
      state.loading = !state.loaded;
      await refreshLibrary();
      await restoreLocation();
    } catch (error) {
      $('#login-error').textContent = error.status === 401 ? '用户名或密码不正确，请再试一次。' : error.message;
      $('#login-error').hidden = false;
      $('#password').focus();
    } finally { button.disabled = false; }
  });

  $('#logout').addEventListener('click', async () => {
    try { await flushProgress(true); state.session = await post('/api/logout', {}); handleUnauthorized(); $('#login-error').hidden = true; }
    catch (error) { if (error.status !== 401) toast(error.message); }
  });
  $('#account-toggle').addEventListener('click', () => {
    const open = $('#account-menu').hidden;
    $('#account-menu').hidden = !open;
    $('#account-toggle').setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', event => { if (!event.target.closest('.account-area')) closeAccountMenu(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#account-menu').hidden) { closeAccountMenu(); $('#account-toggle').focus(); } });
  ['account-settings', 'sidebar-settings'].forEach(id => { $('#' + id).addEventListener('click', () => navigate('settings')); });
  $('#interface-theme').addEventListener('change', event => applyInterfaceTheme(event.target.value));
  $('#check-updates').addEventListener('click', checkUpdates);
  $('#manage-users').addEventListener('click', showUsersList);
  ['users-close', 'users-done'].forEach(id => $('#' + id).addEventListener('click', () => { if (!userManagement.busy) $('#users-dialog').close(); }));
  $('#users-dialog').addEventListener('cancel', event => { if (userManagement.busy) event.preventDefault(); });
  $('#my-libraries-none').addEventListener('click', () => {
    if (!state.session?.authenticated || visibleLibraries.busy || visibleLibraries.loading || !visibleLibraries.loaded) return;
    visibleLibraries.selected.clear(); visibleLibraries.dirty = true; renderVisibleLibraries();
  });
  $('#my-libraries-all').addEventListener('change', event => {
    if (!state.session?.authenticated || visibleLibraries.busy || visibleLibraries.loading || !visibleLibraries.loaded) return;
    visibleLibraries.selected = new Set(event.target.checked ? visibleLibraries.libraries.map(library => String(library.id)) : []);
    visibleLibraries.dirty = true; renderVisibleLibraries();
  });
  $('#user-create').addEventListener('click', () => showUserForm());
  $('#user-close').addEventListener('click', () => { if (!userManagement.busy) $('#user-dialog').close(); });
  $('#user-dialog').addEventListener('cancel', event => { if (userManagement.busy) event.preventDefault(); });
  $('#user-dialog').addEventListener('close', () => { $('#user-password').value = ''; $('#user-password-confirm').value = ''; $('#user-error').hidden = true; userManagement.editingId = null; });
  ['basic', 'permissions'].forEach(tab => $('#user-tab-' + tab).addEventListener('click', () => { if (!userManagement.busy) setUserTab(tab); }));
  $('#user-dialog .profile-tabs').addEventListener('keydown', event => { if (userManagement.busy || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); setUserTab(event.key === 'Home' ? 'basic' : event.key === 'End' ? 'permissions' : $('#user-basic').hidden ? 'basic' : 'permissions', true); });
  $('#user-libraries-all').addEventListener('change', event => { if (userManagement.busy) return; $$('#user-libraries input').forEach(input => { input.checked = event.target.checked; }); syncUserLibrarySelection(); });
  $('#user-libraries-none').addEventListener('click', () => { if (userManagement.busy) return; $$('#user-libraries input').forEach(input => { input.checked = false; }); syncUserLibrarySelection(); });
  $('#user-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!requireAccess() || userManagement.busy) return;
    const generation = userManagement.generation; $('#user-error').hidden = true;
    try {
      const payload = Users.payload({ username: $('#user-username').value, password: $('#user-password').value, password_confirm: $('#user-password-confirm').value, enabled: $('#user-enabled').checked, library_ids: $$('#user-libraries input:checked').map(input => input.value) }, { creating: userManagement.editingId === null, libraries: userManagement.libraries });
      const id = userManagement.editingId; const changedPassword = Boolean(payload.password); setUserBusy(true);
      await post('/api/users' + (id === null ? '' : '/' + encodeURIComponent(id)), payload);
      if (generation !== userManagement.generation) return;
      $('#user-dialog').close(); userManagement.loaded = false; await loadUsers(true); toast(id === null ? '用户已创建。' : changedPassword ? '用户资料与密码已更新，其登录已失效。' : '用户资料已更新。');
    } catch (error) { if (generation === userManagement.generation && error.status !== 401) { if (error.field) setUserTab('basic'); $('#user-error').textContent = error.message; $('#user-error').hidden = false; if (error.field) $('#user-' + error.field)?.focus(); } }
    finally { if (generation === userManagement.generation) setUserBusy(false); }
  });
  $('#user-delete').addEventListener('click', async () => {
    if (!requireAccess() || userManagement.busy) return;
    const id = userManagement.editingId; const user = userManagement.users.find(item => String(item.id) === String(id));
    if (!Users.canManage(user)) return;
    const generation = userManagement.generation;
    if (!await confirmRemoval('users', 1, user.username) || generation !== userManagement.generation) return;
    setUserBusy(true); $('#user-error').hidden = true;
    try { await post('/api/users/' + encodeURIComponent(id) + '/delete', {}); if (generation !== userManagement.generation) return; $('#user-dialog').close(); userManagement.loaded = false; await loadUsers(true); toast('用户及其个人记录已删除，共享资料和源文件保留。'); }
    catch (error) { if (generation === userManagement.generation && error.status !== 401) { $('#user-error').textContent = error.message; $('#user-error').hidden = false; } }
    finally { if (generation === userManagement.generation) setUserBusy(false); }
  });
  $('#edit-profile').addEventListener('click', showProfileForm);
  ['basic', 'permissions'].forEach(tab => $('#profile-tab-' + tab).addEventListener('click', () => { if (!profileMutationBusy) setProfileTab(tab); }));
  $('#profile-dialog .profile-tabs').addEventListener('keydown', event => { if (profileMutationBusy || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); setProfileTab(event.key === 'Home' ? 'basic' : event.key === 'End' ? 'permissions' : $('#profile-basic').hidden ? 'basic' : 'permissions', true); });
  $('#profile-close').addEventListener('click', () => { if (!profileMutationBusy) $('#profile-dialog').close(); });
  $('#profile-dialog').addEventListener('cancel', event => { if (profileMutationBusy) event.preventDefault(); });
  $('#profile-dialog').addEventListener('close', () => { profileFormGeneration += 1; clearAvatarEdit('profile'); discardVisibilityDraft(); setProfileBusy(false); ['profile-current-password', 'profile-password', 'profile-password-confirm'].forEach(id => { $('#' + id).value = ''; }); });
  $('#profile-form').addEventListener('submit', async event => {
    event.preventDefault(); if (profileMutationBusy || !state.session?.authenticated) return;
    const generation = profileFormGeneration; let savingPart = 'basic'; $('#profile-error').hidden = true;
    try {
      const payload = Users.profilePayload({ username: $('#profile-username').value, current_password: $('#profile-current-password').value, password: $('#profile-password').value, password_confirm: $('#profile-password-confirm').value }, avatarPayload('profile'));
      const profile = payload.username !== state.session.user.username || payload.password || payload.avatar_upload || payload.avatar_reset ? payload : null;
      if (visibleLibraries.dirty && (!visibleLibraries.loaded || visibleLibraries.loading)) { savingPart = 'permissions'; throw new Error('写真库设置尚未加载，请重试读取后保存。'); }
      const visibility = visibleLibraries.dirty ? Users.visibilityPayload([...visibleLibraries.selected], visibleLibraries.libraries) : null;
      setProfileBusy(true);
      const ensureCurrent = () => { if (generation !== profileFormGeneration || !state.session?.authenticated) throw new ApiError('账户已切换。', 401); };
      await Users.saveChanges({ profile, visibility,
        saveProfile: data => { savingPart = 'basic'; return post('/api/profile', data); },
        saveVisibility: data => { savingPart = 'permissions'; return post('/api/my-libraries', data); },
        onProfileSaved: session => { ensureCurrent(); state.session = session; activeUser = session.user; $('#profile-username').value = session.user.username; beginAvatarEdit('profile', session.user); ['profile-current-password', 'profile-password', 'profile-password-confirm'].forEach(id => { $('#' + id).value = ''; }); renderAccount(); setProfileBusy(true); },
        onVisibilitySaved: result => { ensureCurrent(); visibleLibraries.libraries = result.libraries || []; visibleLibraries.selected = new Set(visibleLibraries.libraries.filter(library => library.visible).map(library => String(library.id))); visibleLibraries.dirty = false; resetSelection(); }
      });
      ensureCurrent(); await refreshLibrary(); ensureCurrent(); setProfileBusy(false); $('#profile-dialog').close(); toast('用户信息已保存。');
    } catch (error) {
      if (generation !== profileFormGeneration || !state.session?.authenticated) return;
      if (error.status === 401) {
        try { const session = await api('/api/session'); if (!session.authenticated) { handleUnauthorized(); return; } if (String(session.user?.id) !== String(activeUser?.id)) { state.session = session; showLibrary(); return; } state.session = session; }
        catch (_) { if (!state.session?.authenticated) return; }
      }
      if (generation !== profileFormGeneration) return;
      setProfileTab(error.field ? 'basic' : savingPart);
      $('#profile-error').textContent = error.profileSaved ? '个人资料已保存，写真库显示未保存：' + error.message : error.message; $('#profile-error').hidden = false;
      if (error.field) $('#profile-' + error.field)?.focus();
    } finally { if (generation === profileFormGeneration) setProfileBusy(false); }
  });
  $('#create-library').addEventListener('click', () => showLibraryForm());
  $('#home-all-libraries').addEventListener('click', () => navigate('libraries'));
  ['selection-manage', 'selection-toggle'].forEach(id => $('#' + id).addEventListener('click', () => {
    if (selection.busy || !Batch.kind(state.view, isAdmin())) return;
    selection.enabled = !selection.enabled; selection.ids.clear(); renderSelection();
    if (!selection.enabled) $('#selection-manage').focus({ preventScroll: true });
  }));
  $('#selection-all').addEventListener('click', () => {
    if (selection.busy) return;
    const items = selectionItems(); const all = items.every(item => selection.ids.has(String(item.id)));
    items.forEach(item => { if (all) selection.ids.delete(String(item.id)); else selection.ids.add(String(item.id)); }); renderSelection();
  });
  ['models', 'remove', 'scan', 'favorite', 'random'].forEach(action => $('#selection-' + action).addEventListener('click', () => batchAction(action)));
  ['batch-model-close', 'batch-model-cancel'].forEach(id => $('#' + id).addEventListener('click', closeBatchModelForm));
  $('#batch-model-dialog').addEventListener('cancel', event => { if (batchModels.busy) event.preventDefault(); });
  $('#batch-model-dialog').addEventListener('close', () => { batchModels.context = null; batchModels.generation += 1; batchModels.ids = []; batchModels.selected.clear(); batchModels.busy = false; });
  $('#batch-model-search').addEventListener('input', renderBatchModels);
  $('#batch-model-search').addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
  $('#batch-model-show-new').addEventListener('click', () => {
    if (batchModels.busy || !requireAccess('manage_models')) return;
    const row = $('#batch-model-new-row'); row.hidden = !row.hidden;
    $('#batch-model-show-new').setAttribute('aria-expanded', String(!row.hidden));
    if (!row.hidden) { if (!$('#batch-model-new').value) $('#batch-model-new').value = $('#batch-model-search').value.trim(); $('#batch-model-new').focus(); }
  });
  $('#batch-model-new').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); createBatchModel(); } });
  $('#batch-model-add').addEventListener('click', createBatchModel);
  $('#batch-model-clear').addEventListener('change', syncBatchModelForm);
  $('#batch-model-form').addEventListener('submit', saveBatchModels);
  $('#library-remove').addEventListener('click', () => removeCurrent('libraries'));
  $('#metadata-remove').addEventListener('click', () => removeCurrent('books'));
  $('#remove-cancel').addEventListener('click', () => finishRemoval(false));
  $('#remove-confirm').addEventListener('click', () => finishRemoval(true));
  $('#remove-dialog').addEventListener('cancel', event => { event.preventDefault(); finishRemoval(false); });
  $('#remove-dialog').addEventListener('close', () => finishRemoval(false));
  ['profile', 'model'].forEach(kind => {
    $('#' + kind + '-avatar-upload').addEventListener('change', event => readAvatarEdit(kind, event.target.files[0]));
    $('#' + kind + '-avatar-reset').addEventListener('click', () => resetAvatarEdit(kind));
  });
  $('#model-dialog').addEventListener('close', () => clearAvatarEdit('model'));
  $('#edit-library').addEventListener('click', () => showLibraryForm(currentLibrary()));
  $$('#side-nav [data-view]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.view)));
  $('#nav-toggle').addEventListener('click', () => {
    const open = $('#sidebar').classList.toggle('is-open'); $('#nav-backdrop').hidden = !open; $('#nav-toggle').setAttribute('aria-expanded', String(open));
  });
  $('#nav-backdrop').addEventListener('click', () => { $('#sidebar').classList.remove('is-open'); $('#nav-backdrop').hidden = true; $('#nav-toggle').setAttribute('aria-expanded', 'false'); });
  $('#library-dialog-close').addEventListener('click', () => $('#library-dialog').close());
  $('#library-dialog').addEventListener('close', () => { libraryFormGeneration += 1; libraryBrowseRequest += 1; });
  $('#library-type').addEventListener('change', updateLibraryTypeFields);
  $('#library-mode').addEventListener('change', updateLibraryReadingDirection);
  $('#library-add-path').addEventListener('click', () => { if ($$('.library-path-row', $('#library-paths')).length < 20) addLibraryPath('', true); });
  $('#directory-parent').addEventListener('click', event => browseDirectory(event.currentTarget.dataset.path));
  $('#directory-roots').addEventListener('change', event => browseDirectory(event.currentTarget.value));
  $('#directory-select').addEventListener('click', () => { if (libraryBrowseInput?.isConnected) libraryBrowseInput.value = state.directoryPath; $('#directory-picker').hidden = true; });
  $('#library-author').addEventListener('change', () => { const creating = $('#library-author').value === '__new__'; $('#library-author-new').hidden = !creating; $('#library-author-error').hidden = true; if (creating) $('#library-new-author').focus(); });
  $('#library-new-author').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); $('#library-create-author').click(); } });
  $('#library-create-author').addEventListener('click', async () => {
    if (!requireAccess()) return;
    const name = $('#library-new-author').value.trim(); if (!name) { $('#library-author-error').textContent = '请输入新模特名称。'; $('#library-author-error').hidden = false; $('#library-new-author').focus(); return; }
    const generation = libraryFormGeneration;
    const button = $('#library-create-author'); button.disabled = true; $('#library-save').disabled = true; $('#library-author-error').hidden = true;
    try {
      const result = await post('/api/models', { name }); const existing = state.models.find(model => String(model.id) === String(result.model.id)); if (existing) Object.assign(existing, result.model); else state.models.push(result.model);
      state.models.sort((a, b) => collator.compare(a.name, b.name)); updateCounts(); if (generation !== libraryFormGeneration || !$('#library-dialog').open) return;
      renderLibraryAuthors(result.model.id); $('#library-author-new').hidden = true; $('#library-new-author').value = ''; $('#library-author').focus();
    } catch (error) { if (generation === libraryFormGeneration && $('#library-dialog').open) { $('#library-author-error').textContent = error.message; $('#library-author-error').hidden = false; } }
    finally { if (generation === libraryFormGeneration) { button.disabled = false; $('#library-save').disabled = false; } }
  });
  $('#library-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!requireAccess()) return;
    if ($('#library-author').value === '__new__') { $('#library-author-error').textContent = '请先新建模特，或从已有模特中选择一人。'; $('#library-author-error').hidden = false; $('#library-new-author').focus(); return; }
    const button = $('#library-save'); button.disabled = true; $('#library-form-error').hidden = true;
    const data = { name: $('#library-name').value.trim(), media_type: $('#library-type').value, paths: $$('.library-path-input', $('#library-paths')).map(input => input.value.trim()), reading_mode: $('#library-mode').value, reading_direction: $('#library-direction').disabled ? ($('#library-direction').dataset.pagedDirection || 'ltr') : $('#library-direction').value, hide_date_prefix: $('#library-hide-date').checked, apply_reading_defaults: Boolean(state.editingLibrary && $('#library-apply-reading').checked), exclude_rules: $('#library-excludes').value.split(/\r?\n/).map(value => value.trim()).filter(value => value && !value.startsWith('#')), default_author_id: $('#library-author').value || null };
    try {
      const result = await post('/api/libraries' + (state.editingLibrary ? '/' + encodeURIComponent(state.editingLibrary) : ''), data);
      if (state.refreshPromise) await state.refreshPromise;
      state.libraryId = String(result.library.id); state.query = ''; state.series = ''; $('#search').value = '';
      storePreference('sumi-library', state.libraryId);
      $('#library-dialog').close();
      resetSelection(); state.view = 'library'; state.sort = librarySort(state.libraryId); $('#sort').value = state.sort; state.loading = true; syncViewRoute(); await refreshLibrary();
      toast(state.editingLibrary ? '写真库设置已保存，正在重新扫描。' : '写真库已创建，正在整理你的作品。');
    } catch (error) { $('#library-form-error').textContent = error.message; $('#library-form-error').hidden = false; }
    finally { button.disabled = false; }
  });
  $$('.scan-trigger').forEach(button => button.addEventListener('click', scanLibrary));
  $('#search').addEventListener('input', event => { state.query = event.target.value; renderLibrary(); syncViewRoute(true); });
  $('#series-filter').addEventListener('change', event => { state.series = event.target.value; renderLibrary(); syncViewRoute(true); });
  $('#sort').addEventListener('change', event => { state.sort = End.normalizeSort(event.target.value); if (state.view === 'library') saveLibrarySort(state.libraryId, state.sort); renderLibrary(); syncViewRoute(true); });
  Object.keys(defaultPrefs).forEach(key => $('#pref-' + key).addEventListener('change', event => savePref(key, event.target.value)));
  $('#detail-close').addEventListener('click', () => { state.detailBook = null; $('#book-detail').close(); });
  $('#book-detail').addEventListener('close', () => {
    if ($('#book-detail').open) return;
    state.detailBook = null;
    if (routing.current.view === 'book') returnFromOverlay();
  });
  $('#metadata-close').addEventListener('click', () => $('#metadata-dialog').close());
  $('#metadata-dialog').addEventListener('close', () => { metadataFormGeneration += 1; clearMetadataCoverUpload(); state.editingBook = null; });
  $('#metadata-model-search').addEventListener('input', renderMetadataModels);
  $('#metadata-show-new-model').addEventListener('click', () => {
    if (!requireAccess('manage_models')) return;
    const row = $('#metadata-new-model-row'); row.hidden = !row.hidden;
    $('#metadata-show-new-model').setAttribute('aria-expanded', String(!row.hidden));
    if (!row.hidden) { if (!$('#metadata-new-model').value) $('#metadata-new-model').value = $('#metadata-model-search').value.trim(); $('#metadata-new-model').focus(); }
  });
  $('#metadata-new-model').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); $('#metadata-add-model').click(); } });
  $('#metadata-model-search').addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
  $('#metadata-mode').addEventListener('change', updateMetadataReadingDirection);
  $('#metadata-direction').addEventListener('change', () => { if (!$('#metadata-direction').disabled) metadataDirectionValue = $('#metadata-direction').value; });
  $('#metadata-cover-source').addEventListener('change', updateCoverPreview);
  $('#metadata-cover').addEventListener('input', updateCoverPreview);
  $('#metadata-cover-upload').addEventListener('change', event => readMetadataCover(event.target.files[0]));
  $('#metadata-add-model').addEventListener('click', async () => {
    if (!requireAccess('manage_models')) return;
    const name = $('#metadata-new-model').value.trim(); if (!name) { $('#metadata-new-model').focus(); return; }
    const generation = metadataFormGeneration;
    const button = $('#metadata-add-model'); button.disabled = true; $('#metadata-error').hidden = true;
    try {
      const result = await post('/api/models', { name });
      const existing = state.models.find(model => model.id === result.model.id); if (existing) Object.assign(existing, result.model); else state.models.push(result.model);
      state.models.sort((a, b) => collator.compare(a.name, b.name)); updateCounts();
      if (generation === metadataFormGeneration) {
        state.selectedModels.add(String(result.model.id)); $('#metadata-new-model').value = ''; $('#metadata-model-search').value = '';
        $('#metadata-new-model-row').hidden = true; $('#metadata-show-new-model').setAttribute('aria-expanded', 'false'); renderMetadataModels();
      }
    } catch (error) { if (generation === metadataFormGeneration) { $('#metadata-error').textContent = error.message; $('#metadata-error').hidden = false; } }
    finally { button.disabled = false; }
  });
  $('#metadata-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!requireAccess('edit_metadata')) return; const book = state.editingBook; if (!book) return;
    const generation = metadataFormGeneration;
    const button = $('#metadata-save'); button.disabled = true; $('#metadata-error').hidden = true;
    try {
      const payload = { reading_mode: $('#metadata-mode').value || null, reading_direction: metadataDirectionValue || null, cover_source: $('#metadata-cover-source').value };
      if (payload.cover_source === 'page') payload.cover_page = Number($('#metadata-cover').value) - 1;
      else if (metadataCoverUpload?.pending) throw new Error('正在读取封面图片，请稍后保存。');
      else if (metadataCoverUpload?.data) payload.cover_upload = { data: metadataCoverUpload.data };
      else if (book.cover_source !== 'upload') { $('#metadata-cover-upload').focus(); throw new Error('请选择要上传的封面图片。'); }
      // Only changed fields become manual overrides; cover/title edits retain automatic import values.
      const title = $('#metadata-name').value.trim();
      if (title !== book.title) payload.title = title || null;
      const releaseDate = $('#metadata-release').value;
      if (releaseDate !== (book.release_date || '')) payload.release_date = releaseDate;
      const previousModels = new Set((book.models || []).map(model => String(model.id)));
      if (previousModels.size !== state.selectedModels.size || [...state.selectedModels].some(id => !previousModels.has(String(id)))) payload.model_ids = [...state.selectedModels];
      const result = await post(bookUrl(book.id) + '/metadata', payload);
      Object.assign(book, result.book); const cached = state.books.find(item => item.id === book.id); if (cached) Object.assign(cached, result.book);
      if (state.detailBook?.id === book.id) { state.detailBook = result.book; renderDetail(result.book); }
      if (generation === metadataFormGeneration) $('#metadata-dialog').close();
      await refreshLibrary(); toast('作品资料已保存');
    } catch (error) { if (generation === metadataFormGeneration) { $('#metadata-error').textContent = error.message; $('#metadata-error').hidden = false; } }
    finally { if (generation === metadataFormGeneration) button.disabled = Boolean(metadataCoverUpload?.pending); }
  });
  $('#create-model').addEventListener('click', () => showModelForm());
  $('#model-close').addEventListener('click', () => { if (!modelMutationBusy) $('#model-dialog').close(); });
  $('#model-dialog').addEventListener('cancel', event => { if (modelMutationBusy) event.preventDefault(); });
  $('#model-remove').addEventListener('click', async () => {
    if (!requireAccess('manage_models')) return;
    if (modelMutationBusy || !state.editingModel) return;
    const id = state.editingModel;
    const model = state.models.find(item => String(item.id) === String(id));
    const edit = avatarEdits.model;
    if (!model || !await confirmRemoval('models', 1, model.name) || avatarEdits.model !== edit) return;
    setModelMutationBusy(true); $('#model-error').hidden = true;
    try {
      await post('/api/models/' + encodeURIComponent(id) + '/remove', {});
      if (avatarEdits.model === edit) $('#model-dialog').close();
      state.models = state.models.filter(item => String(item.id) !== String(id));
      state.selectedModels.delete(String(id));
      if (String(state.modelId) === String(id)) { state.modelId = ''; if (state.view === 'model') navigate('models'); }
      await refreshLibrary(); toast('模特已删除，作品和源文件保留。');
    } catch (error) {
      if (avatarEdits.model === edit) { $('#model-error').textContent = error.message; $('#model-error').hidden = false; }
      else if (error.status !== 401) toast(error.message);
    } finally {
      setModelMutationBusy(false);
    }
  });
  $('#model-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!requireAccess('manage_models') || modelMutationBusy) return; setModelMutationBusy(true); $('#model-error').hidden = true;
    const edit = avatarEdits.model;
    try { await post('/api/models' + (state.editingModel ? '/' + encodeURIComponent(state.editingModel) : ''), { name: $('#model-name').value.trim(), ...avatarPayload('model') }); if (avatarEdits.model === edit) $('#model-dialog').close(); await refreshLibrary(); toast('模特资料已保存。'); }
    catch (error) { if (avatarEdits.model === edit) { $('#model-error').textContent = error.message; $('#model-error').hidden = false; } }
    finally { setModelMutationBusy(false); }
  });
  $('#reader-bookmark').addEventListener('click', showBookmarkForm);
  $('#bookmark-close').addEventListener('click', () => $('#bookmark-dialog').close());
  $('#bookmark-form').addEventListener('submit', async event => {
    event.preventDefault(); const target = state.bookmarkTarget; if (!target) return;
    const button = $('#bookmark-save'); button.disabled = true; $('#bookmark-error').hidden = true;
    try {
      const result = await post(bookUrl(target.bookId) + '/bookmarks', { page: target.page, note: $('#bookmark-note').value.trim() });
      const book = reader.book?.id === target.bookId ? reader.book : state.books.find(item => item.id === target.bookId);
      state.bookmarks = [{ ...result.bookmark, book }, ...state.bookmarks.filter(item => item.id !== result.bookmark.id)];
      if (reader.book?.id === target.bookId) reader.book.bookmarks = [result.bookmark, ...(reader.book.bookmarks || []).filter(item => item.id !== result.bookmark.id)];
      $('#bookmark-dialog').close(); updateCounts(); toast('书签已保存');
    } catch (error) { $('#bookmark-error').textContent = error.message; $('#bookmark-error').hidden = false; }
    finally { button.disabled = false; }
  });

  $('#reader-close').addEventListener('click', () => closeReader());
  $('#reader').addEventListener('cancel', event => { event.preventDefault(); if (reader.choice?.active) closeReaderChoice(true); else if (reader.endOverlay) hideReaderEnd(); else closeReader(); });
  $('#reader-mode').addEventListener('change', event => changeReaderSetting('mode', event.target.value));
  $('#reader-direction').addEventListener('change', event => changeReaderSetting('direction', event.target.value));
  $('#reader-fit').addEventListener('change', event => {
    savePref('fit', event.target.value);
    applyReaderPrefs();
    if (state.prefs.mode === 'webtoon' && reader.book) {
      reader.restoring = reader.index;
      reader.rows[reader.index]?.element.scrollIntoView({ block: 'start', behavior: 'instant' });
      reader.restoring = null;
      scheduleVisibleScroll();
    }
  });
  $('#reader-theme').addEventListener('click', () => { savePref('theme', state.prefs.theme === 'dark' ? 'light' : 'dark'); applyReaderPrefs(); });
  $('#reader-chapters').addEventListener('click', () => toggleChapters());
  $('#chapter-drawer-close').addEventListener('click', () => toggleChapters(false));
  $('#chapter-hover-edge').addEventListener('mouseenter', () => { if (!isPhoto() && matchMedia('(hover:hover) and (min-width: 900px)').matches) toggleChapters(true); });
  $('#reader-fullscreen').addEventListener('click', toggleFullscreen);
  $('#reader-help').addEventListener('click', toggleHelp);
  $('#reader-help-close').addEventListener('click', toggleHelp);
  $('#reader-menu-toggle').addEventListener('click', () => { const menu = $('#reader-menu'); menu.hidden = !menu.hidden; $('#reader-menu-toggle').setAttribute('aria-expanded', String(!menu.hidden)); scheduleReaderChrome(); });
  $('#reader-menu-mode').addEventListener('click', () => changeReaderSetting('mode', state.prefs.mode === 'double' ? 'single' : 'double'));
  $('#reader-menu-theme').addEventListener('click', () => { savePref('theme', state.prefs.theme === 'dark' ? 'light' : 'dark'); applyReaderPrefs(); });
  $('#reader-menu-chapters').addEventListener('click', () => { $('#reader-menu').hidden = true; $('#reader-menu-toggle').setAttribute('aria-expanded', 'false'); toggleChapters(true); });
  $('#cover-alone').addEventListener('change', event => { reader.coverAlone = event.target.checked; setReaderMode(); });
  $('#spread-offset').addEventListener('change', event => { reader.offset = event.target.checked; setReaderMode(); });
  document.addEventListener('fullscreenchange', () => $('#reader-fullscreen').setAttribute('aria-label', document.fullscreenElement ? '退出全屏' : '进入全屏'));
  let resizeTimer;
  new ResizeObserver(() => {
    if (!reader.open || !reader.book) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!reader.open || !reader.book) return;
      applyReaderPrefs();
      if (state.prefs.mode === 'double' && readerMode() !== reader.effective) setReaderMode();
    }, 120);
  }).observe($('#reader-viewport'));
  $('#previous-page').addEventListener('click', () => movePage(-1));
  $('#next-page').addEventListener('click', () => movePage(1));
  $('#tap-left').addEventListener('click', () => { if (Date.now() >= reader.suppressClickUntil) { physicalPage(-1); if (!reader.endOverlay) $('#reader-viewport').focus({ preventScroll: true }); } });
  $('#tap-right').addEventListener('click', () => { if (Date.now() >= reader.suppressClickUntil) { physicalPage(1); if (!reader.endOverlay) $('#reader-viewport').focus({ preventScroll: true }); } });
  $('#page-slider').addEventListener('input', event => setReaderPage(event.target.value));
  $('#page-input').addEventListener('change', event => setReaderPage(Number(event.target.value) - 1));
  $('#page-input').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); setReaderPage(Number(event.target.value) - 1); event.target.blur(); } });
  $('#reader-viewport').addEventListener('scroll', scheduleVisibleScroll, { passive: true });
  $('#reader-viewport').addEventListener('wheel', event => {
    const now = Date.now(); const idle = now - reader.lastWheelAt;
    reader.lastWheelAt = now;
    if (state.prefs.mode === 'webtoon' && event.deltaY > 0 && canFinishReader()) {
      syncReaderEndBoundary();
      // A fresh wheel gesture after reaching the bottom opens the panel;
      // momentum from the scroll that arrived there cannot open it accidentally.
      if (idle > 180 && now - reader.endBoundaryAt >= 250) { event.preventDefault(); showReaderEnd(); }
    }
    reader.restoring = null;
  }, { passive: false });
  $('#reader-viewport').addEventListener('click', event => {
    if (Date.now() < reader.suppressClickUntil || event.target.closest('button,input,select,a')) return;
    const rect = $('#reader-viewport').getBoundingClientRect();
    const position = (event.clientX - rect.left) / rect.width;
    if (position >= .25 && position <= .75) setReaderChrome($('#reader').dataset.controls !== 'visible');
  });
  ['.reader-toolbar', '.reader-footer', '.reader-floating'].forEach(selector => {
    $(selector).addEventListener('pointerdown', scheduleReaderChrome);
    $(selector).addEventListener('focusout', scheduleReaderChrome);
  });
  $('#reader').addEventListener('keydown', event => {
    if (!reader.endOverlay && event.key === 'Tab' && $('#reader').dataset.controls !== 'visible') setReaderChrome(true);
  });
  let touchStart = null;
  $('#reader-viewport').addEventListener('touchstart', event => {
    const atBottom = state.prefs.mode === 'webtoon' && canFinishReader();
    reader.restoring = null;
    if (event.touches.length !== 1) { touchStart = null; return; }
    touchStart = { x: event.touches[0].clientX, y: event.touches[0].clientY, mode: state.prefs.mode, atBottom };
  }, { passive: true });
  $('#reader-viewport').addEventListener('touchend', event => {
    if (!touchStart || event.changedTouches.length !== 1) return;
    const dx = event.changedTouches[0].clientX - touchStart.x;
    const dy = event.changedTouches[0].clientY - touchStart.y;
    const origin = touchStart;
    touchStart = null;
    if (origin.mode === 'webtoon') {
      if (origin.atBottom && dy < -55 && Math.abs(dy) > Math.abs(dx) * 1.5) {
        reader.suppressClickUntil = Date.now() + 500; showReaderEnd();
      }
      return;
    }
    if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      reader.suppressClickUntil = Date.now() + 500;
      physicalPage(dx < 0 ? 1 : -1);
    }
  }, { passive: true });
  $('#reader-viewport').addEventListener('touchcancel', () => { touchStart = null; }, { passive: true });

  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
    const editable = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(event.target.tagName);
    if (reader.open) {
      if (editable || !reader.book || reader.endOverlay) return;
      if (event.key === 'ArrowLeft') { event.preventDefault(); if (isPhoto()) physicalPage(-1); else moveChapter(state.prefs.direction === 'rtl' ? 1 : -1); }
      else if (event.key === 'ArrowRight') { event.preventDefault(); if (isPhoto()) physicalPage(1); else moveChapter(state.prefs.direction === 'rtl' ? -1 : 1); }
      else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); const delta = event.key === 'ArrowDown' ? 1 : -1; if (state.prefs.mode === 'webtoon') scrollReader(delta, !event.repeat); else movePage(delta); }
      else if (event.key === 'Enter') { event.preventDefault(); toggleFullscreen(); }
      else if (event.key === 'Backspace') { event.preventDefault(); closeReader(); }
      else if (event.key === '?') { event.preventDefault(); toggleHelp(); }
      else if (event.key.toLowerCase() === 'm') { event.preventDefault(); setReaderChrome($('#reader').dataset.controls !== 'visible'); }
      else if (event.key === ' ' || event.key === 'PageDown') { event.preventDefault(); const delta = event.shiftKey ? -1 : 1; if (state.prefs.mode === 'webtoon') scrollReader(delta, !event.repeat); else movePage(delta); }
      else if (event.key === 'PageUp') { event.preventDefault(); if (state.prefs.mode === 'webtoon') scrollReader(-1); else movePage(-1); }
      else if (event.key === 'Home') { event.preventDefault(); setReaderPage(0); }
      else if (event.key === 'End') { event.preventDefault(); setReaderPage(reader.book.page_count - 1); }
    } else if (event.key === '/' && !editable && !$('dialog[open]') && $('#login-screen').hidden) { event.preventDefault(); navigate('search'); }
  });

  function refreshOnReturn() {
    if (document.visibilityState === 'visible' && state.session?.authenticated) {
      if (!reader.open) {
        const writes = [...reader.saveChains.values()];
        Promise.allSettled(writes).then(() => { if (!reader.open) refreshLibrary(); });
      }
      else if (reader.loadedPage !== null) {
        // A tab hidden while an image was loading gets recorded when it is visible again.
        if (state.prefs.mode === 'webtoon') scheduleVisibleScroll();
        else markVisiblePage(reader.loadedPage);
      }
    }
  }
  window.addEventListener('focus', refreshOnReturn);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushProgress(true); else refreshOnReturn(); });
  window.addEventListener('pagehide', () => flushProgress(true));
  window.addEventListener('hashchange', restoreLocation);

  let performanceResizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(performanceResizeTimer);
    performanceResizeTimer = setTimeout(() => {
      if (!state.session?.authenticated) return;
      if (!reader.open && !Performance.get().bookPageSize) renderLibrary();
      if (reader.open && !Performance.get().chapterPageSize) renderChapters();
    }, 180);
  });
  window.addEventListener('sumi-performance-change', () => {
    catalogPaging.signature = ''; chapterPaging.signature = '';
    if (state.session?.authenticated && state.view !== 'settings') renderLibrary();
    if (reader.open && reader.book) {
      renderChapters();
      if (state.prefs.mode === 'webtoon') loadScrollBuffer(reader.index);
      else preloadNeighbors(reader.index);
    } else reader.pageCache.setLimit(Performance.get().imageCacheLimit);
  });

  setupReaderChoices();
  (async function init() {
    try {
      state.session = await api('/api/session');
      if (!state.session.authenticated) { showLogin(); return; }
      showLibrary();
      await refreshLibrary();
      await restoreLocation();
    } catch (error) {
      if (error.status === 401) return;
      state.loading = false;
      state.libraryError = error.message;
      showLogin();
      $('#login-error').textContent = error.message;
      $('#login-error').hidden = false;
    }
  })();
})();

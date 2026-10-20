'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Transcoding = require('../static/transcoding-ui.js');
const Cache = require('../static/cache-management-ui.js');
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  class Element {
    constructor(tag, document) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.style = {}; this.value = ''; this.className = ''; this.open = false; this.classList = { add() {}, toggle() {} }; }
    append(...items) { items.forEach(item => { item.remove(); item.parent = this; this.children.push(item); }); }
    replaceChildren(...items) { this.children.forEach(item => { item.parent = null; }); this.children = []; this.append(...items); }
    insertBefore(item, next) { item.remove(); const position = next ? this.children.indexOf(next) : this.children.length; item.parent = this; this.children.splice(position, 0, item); }
    after(item) { this.parent.insertBefore(item, this.parent.children[this.parent.children.indexOf(this) + 1]); }
    remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    dispatch(type) { return Promise.all((this.listeners[type] || []).map(handler => handler({ target: this, preventDefault() {} }))); }
    querySelectorAll(selector) { const match = item => item.tagName === selector.split('[')[0] && (!selector.includes('[open]') || item.open) && (!selector.includes('[data-transcode-id]') || item.dataset.transcodeId !== undefined); return this.children.flatMap(item => [...(match(item) ? [item] : []), ...item.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    get elements() { return ['input', 'select', 'button'].flatMap(tag => this.querySelectorAll(tag)); }
    reportValidity() { return true; }
    close() { this.open = false; this.dispatch('close'); }
    showModal() { this.open = true; }
  }
  const document = { hidden: false, activeElement: null, createElement: tag => new Element(tag, document) };
  document.body = new Element('body', document); const host = new Element('section', document); document.body.append(host);
  function find(root, predicate) { if (predicate(root)) return root; for (const child of root.children) { const item = find(child, predicate); if (item) return item; } return null; }
  const byText = text => find(document.body, item => item.tagName === 'button' && item.textContent === text);
  return { document, host, find, byText };
}

test('historical output parameters and completion dates are shown as unrecorded, not current defaults', () => {
  for (const value of [null, undefined, '', {}, { output_format: 'webp', quality: 88 }]) assert.equal(Transcoding.formatParameters(value), '未记录');
  assert.equal(Transcoding.formatParameters({ output_format: 'jpeg', quality: 71, max_output_side: 2048 }), 'JPEG · 质量 71 · 最长边 2048px');
  assert.equal(Transcoding.formatTime(null), '未记录'); assert.equal(Transcoding.formatTime(0), '未记录'); assert.equal(Transcoding.formatTime(Infinity), '未记录');
  assert.notEqual(Transcoding.formatTime(1792080000), '未记录');
});

test('pending progress changes rendered rows but a completion heartbeat does not', () => {
  const book = { id: 'a', pending_count: 8, running_count: 1 };
  assert.notEqual(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, pending_count: 7 }));
  assert.equal(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, updated_at: 5, completed_at: 6 }));
});

const optimization = { rules: Transcoding.defaultRules, books: [{ id: 'a', title: '测试', status: 'ready', matched_count: 2, pending_count: 0 }] };
function detailPage(page) {
  return { book: { id: 'a', title: '测试', status: 'ready', pending_count: 0, matched_count: 2 }, current_rule_revision: 2, target_rule_revision: 2,
    target_parameters: { output_format: 'webp', quality: 88, max_output_side: 3840 }, active_profiles: [{ profile: 'old-profile', parameters: null, count: 2, bytes: 512 }],
    pages: [{ page_index: page - 1, status: 'success', source_bytes: 2000, original_path: '/library/private-' + page + '.png', active_path: '/data/transcoded/' + page + '.webp', active_bytes: 256, active_parameters: null }],
    pagination: { page, pages: 2, total: 2, page_size: 1 } };
}

test('optimization detail requests one page at a time and never loads image thumbnails', async () => {
  const f = fixture(); const urls = [];
  const ui = Transcoding.create({ host: f.host, api: async url => { urls.push(url); return url.includes('/details?') ? detailPage(url.includes('page=2') ? 2 : 1) : optimization; }, post: async () => ({}), toast() {} });
  ui.show(); await flush(); await f.byText('优化详情').dispatch('click'); await flush();
  const dialog = f.find(f.document.body, item => item.className.includes('transcode-details-dialog'));
  assert.equal(dialog.open, true); assert.equal(urls.filter(url => url.includes('/details?')).length, 1);
  await f.byText('图片明细').dispatch('click');
  assert.equal(dialog.querySelectorAll('img').length, 0);
  await f.find(dialog, item => item.tagName === 'button' && item.textContent === '下一页').dispatch('click'); await flush();
  assert.ok(urls.at(-1).includes('page=2&page_size=40'));
  const path = f.find(dialog, item => item.tagName === 'code' && item.textContent === '/library/private-2.png'); assert.ok(path);
  ui.reset(); ui.hide();
});

test('late detail response cannot restore private paths after an account reset', async () => {
  const f = fixture(); let resolveDetails;
  const ui = Transcoding.create({ host: f.host, api: url => url.includes('/details?') ? new Promise(resolve => { resolveDetails = resolve; }) : Promise.resolve(optimization), post: async () => ({}), toast() {} });
  ui.show(); await flush(); await f.byText('优化详情').dispatch('click'); ui.reset(); resolveDetails(detailPage(1)); await flush();
  assert.equal(f.find(f.document.body, item => item.tagName === 'code' && String(item.textContent).includes('/library/private-')), null);
  ui.hide();
});

function cacheSnapshot(revision = 1, cover_limit_mb = 512) {
  return { settings: { revision, cover_limit_mb, pdf_limit_mb: 1024 }, summary: { bytes: 1024 }, categories: [
    { kind: 'covers', bytes: 512, files: 1, can_clear: true, protected_files: 1 },
    { kind: 'library_covers', bytes: 512, files: 1, can_clear: true },
    { kind: 'pdf', retention: 'disposable', bytes: 2048, files: 2, can_clear: true },
    { kind: 'optimized', bytes: 0, files: 2, can_clear: true, clearable_files: 2, reclaimable_files: 0 }
  ] };
}

test('cache clear requires a separate explicit confirmation describing original fallback', async () => {
  const f = fixture(); const posts = []; let catalogChanges = 0;
  const ui = Cache.create({ host: f.host, api: async () => cacheSnapshot(), post: async (url, body) => { posts.push({ url, body }); return cacheSnapshot(); }, onCatalogChange: () => { catalogChanges += 1; } });
  ui.show(); await flush(); await f.byText('清除在用优化图').dispatch('click');
  const policy = f.find(f.host, item => item.className === 'cache-policy' && item.textContent.includes('在用'));
  assert.ok(policy.textContent.includes('保留副本')); assert.equal(policy.textContent.includes('等待发布'), false);
  assert.equal(posts.length, 0); const description = Cache.clearDescription('optimized'); assert.ok(description.includes('切回原图')); assert.ok(description.includes('不会删除原始文件')); assert.ok(description.includes('等待发布'));
  await f.byText('确认清除').dispatch('click'); await flush();
  assert.deepEqual(posts, [{ url: '/api/cache/clear', body: { kind: 'optimized', confirm: true } }]); assert.equal(catalogChanges, 1);
  ui.reset(); ui.hide();
});

test('cache polling preserves a dirty value and its original revision instead of silently overwriting a new admin setting', async () => {
  const f = fixture(); let current = cacheSnapshot(); const posts = [];
  const ui = Cache.create({ host: f.host, api: async () => current, post: async (url, body) => { posts.push(body); if (body.expected_revision === 1) throw Object.assign(new Error('设置已变化'), { status: 409 }); return cacheSnapshot(3, body.cover_limit_mb); } });
  ui.show(); await flush(); const input = f.find(f.host, item => item.tagName === 'input'); input.value = '64'; await input.dispatch('input');
  current = cacheSnapshot(2, 128); await ui.refresh(); assert.equal(input.value, '64');
  const form = f.find(f.host, item => item.tagName === 'form'); await form.dispatch('submit'); assert.equal(posts[0].expected_revision, 1);
  await f.byText('放弃修改').dispatch('click'); assert.equal(input.value, 128);
  input.value = '32'; await input.dispatch('input'); await form.dispatch('submit'); assert.equal(posts[1].expected_revision, 2);
  ui.reset(); ui.hide();
});

test('late cache response is ignored after reset and destructive actions stay disabled', async () => {
  const f = fixture(); let resolve;
  const ui = Cache.create({ host: f.host, api: () => new Promise(done => { resolve = done; }), post: async () => cacheSnapshot() });
  ui.show(); ui.reset(); resolve(cacheSnapshot()); await flush();
  assert.equal(f.find(f.host, item => item.tagName === 'input').value, ''); assert.equal(f.byText('清除在用优化图').disabled, true); ui.hide();
});

test('no cache snapshot or unsupported kind can be accepted as a destructive-operation description', () => {
  assert.equal(Cache.validSnapshot(null), false); assert.equal(Cache.validSnapshot({ settings: { revision: 1, cover_limit_mb: true }, categories: [] }), false);
  assert.equal(Cache.validSnapshot(cacheSnapshot()), true); assert.equal(Cache.clearDescription('unknown'), '');
  assert.equal(Cache.validSnapshot({ ...cacheSnapshot(), settings: { revision: 1, cover_limit_mb: 1, pdf_limit_mb: true } }), false);
  assert.ok(Cache.clearDescription('covers').includes('手工上传')); assert.ok(Cache.clearDescription('library_covers').includes('保留已选择'));
});

test('PDF cache clearing is separately confirmed and explicitly preserves the PDF and reading records', async () => {
  const f = fixture(); const posts = [];
  const ui = Cache.create({ host: f.host, api: async () => cacheSnapshot(), post: async (url, body) => { posts.push({ url, body }); return cacheSnapshot(); } });
  ui.show(); await flush();
  const card = f.find(f.host, item => item.className.includes('cache-category') && Boolean(f.find(item, child => child.tagName === 'h2' && child.textContent === 'PDF 页面缓存')));
  assert.ok(card); assert.equal(f.find(card, item => item.className === 'cache-size').textContent, '2 KiB');
  await f.find(card, item => item.tagName === 'button' && item.textContent === '清除缓存').dispatch('click');
  assert.equal(posts.length, 0); assert.ok(Cache.clearDescription('pdf').includes('不会删除原始 PDF')); assert.ok(Cache.clearDescription('pdf').includes('阅读记录'));
  await f.byText('确认清除').dispatch('click');
  assert.deepEqual(posts, [{ url: '/api/cache/clear', body: { kind: 'pdf', confirm: true } }]);
  ui.reset(); ui.hide();
});

test('PDF cache capacity can disable persistence, preserves a dirty form during polling and rejects empty values', async () => {
  const f = fixture(); let current = cacheSnapshot(); const posts = [];
  const ui = Cache.create({ host: f.host, api: async () => current, post: async (url, body) => { posts.push({ url, body }); return { ...cacheSnapshot(3), settings: { revision: 3, cover_limit_mb: body.cover_limit_mb, pdf_limit_mb: body.pdf_limit_mb } }; } });
  ui.show(); await flush();
  const pdf = f.find(f.host, item => item.tagName === 'input' && item.attributes['aria-label']?.startsWith('PDF'));
  const cover = f.find(f.host, item => item.tagName === 'input' && item !== pdf); const form = f.find(f.host, item => item.tagName === 'form');
  assert.equal(pdf.value, 1024); pdf.value = '0'; await pdf.dispatch('input');
  current = cacheSnapshot(2, 128); current.settings.pdf_limit_mb = 4096; await ui.refresh();
  assert.equal(pdf.value, '0'); assert.equal(cover.value, 512);
  cover.value = ''; await form.dispatch('submit'); assert.equal(posts.length, 0);
  cover.value = '512'; await form.dispatch('submit');
  assert.deepEqual(posts[0], { url: '/api/cache/settings', body: { expected_revision: 1, cover_limit_mb: 512, pdf_limit_mb: 0 } });
  ui.reset(); assert.equal(pdf.value, ''); assert.equal(pdf.disabled, true); ui.hide();
});

test('a bounded inventory explicitly marks incomplete totals and clears the notice after a complete poll', async () => {
  const f = fixture(); let current = cacheSnapshot(); current.summary.truncated = true;
  const ui = Cache.create({ host: f.host, api: async () => current, post: async () => cacheSnapshot() });
  ui.show(); await flush(); const total = f.find(f.host, item => item.className === 'cache-total'); assert.ok(total.textContent.includes('本次统计不完整'));
  current = cacheSnapshot(); await ui.refresh(); assert.equal(total.textContent.includes('统计不完整'), false);
  ui.reset(); ui.hide();
});

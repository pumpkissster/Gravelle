'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Application section is available: ${start}`);
  return source.slice(first, last);
}
const avatarFunctions = section('  function clearAvatarEdit(', '  function showProfileForm(');
const modelFormFunctions = section('  function setModelMutationBusy(', '  function showBookmarkForm(');
const saveHandler = section("  $('#model-form').addEventListener('submit'", "  $('#reader-bookmark').addEventListener");
const closeHandler = section("  $('#model-dialog').addEventListener('close', () => clearAvatarEdit('model'));", "  $('#model-avatar-regenerate').addEventListener");
const jpeg = 'data:image/jpeg;base64,/9j/AA==';
const uploaded = 'data:image/webp;base64,UklGRg==';
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
class Element {
  constructor() { this.value = ''; this.hidden = false; this.disabled = false; this.open = false; this.src = ''; this.textContent = ''; this.handlers = new Map(); this.label = { textContent: '' }; }
  addEventListener(type, handler) { this.handlers.set(type, handler); }
  emit(type, data = {}) { return this.handlers.get(type)?.({ preventDefault() {}, ...data }); }
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; this.emit('close'); } }
  focus() {}
}
function fixture() {
  const elements = new Map(), requests = [], reads = [], messages = [];
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const model = { id: 'model / 1', name: 'Original model', avatar_url: '/api/avatars/model/one?v=1', cover_book_id: 'previous-cover', book_count: 4 };
  const scope = {
    AbortController, authGeneration: 1, modelMutationBusy: false, avatarEdits: {},
    allowed: true, state: { editingModel: null, models: [model] },
    $(selector, parent) { return parent && selector === 'span' ? parent.label : element(selector); },
    requireAccess(permission) { assert.equal(permission, 'manage_models'); return scope.allowed; },
    bookUrl(id) { return '/api/books/' + encodeURIComponent(id); },
    Avatars: {
      source(entity, fallback) { return entity?.avatar_url || fallback; },
      readFile(file, options) { const result = deferred(); reads.push({ file, options, ...result }); return result.promise; }
    },
    post(url, payload, options) { const result = deferred(); requests.push({ url, payload, options, ...result }); return result.promise; },
    async refreshLibrary() { scope.refreshes += 1; },
    refreshes: 0, toast(message) { messages.push(message); }
  };
  vm.createContext(scope);
  vm.runInContext(avatarFunctions + modelFormFunctions + closeHandler + saveHandler, scope, { filename: 'app.js:model-avatar' });
  scope.showModelForm(model);
  return { scope, model, requests, reads, messages, get: id => element('#' + id), payload: () => JSON.parse(JSON.stringify(scope.avatarPayload('model'))) };
}

test('generated model avatars stay in the editor until Save submits the ordinary model update', async () => {
  const f = fixture(), originalModel = JSON.stringify(f.model), oldPreview = f.get('model-avatar-preview').src;
  const operation = f.scope.regenerateModelAvatar();
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].url, '/api/models/model%20%2F%201/avatar/regenerate');
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0].payload)), { exclude_book_id: 'previous-cover' });
  assert.equal(f.get('model-avatar-preview').src, oldPreview);
  assert.equal(f.get('model-save').disabled, true);
  assert.equal(f.get('model-avatar-regenerate').disabled, true);
  assert.throws(f.payload, /正在读取头像/);
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'next-cover' }); await operation;
  assert.equal(f.get('model-avatar-preview').src, jpeg);
  assert.equal(f.get('model-save').disabled, false);
  assert.deepEqual(f.payload(), { avatar_upload: jpeg });
  assert.equal(JSON.stringify(f.model), originalModel);
  assert.equal(f.requests.length, 1);
  assert.equal(f.scope.refreshes, 0);

  f.get('model-name').value = '  Updated name  ';
  const saving = f.get('model-form').emit('submit');
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[1].url, '/api/models/model%20%2F%201');
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[1].payload)), { name: 'Updated name', avatar_upload: jpeg });
  f.requests[1].resolve({}); await saving;
  assert.equal(f.get('model-dialog').open, false);
  assert.equal(f.scope.refreshes, 1);
  assert.deepEqual(f.messages, ['模特资料已保存。']);
});

test('a saved custom avatar does not force every new generation to exclude the same default work', async () => {
  const f = fixture();
  f.model.avatar_url = '/api/avatars/models/one?v=2';
  f.scope.showModelForm(f.model);
  const operation = f.scope.regenerateModelAvatar();
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0].payload)), {});
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'next-cover' });
  await operation;
  assert.equal(f.get('model-avatar-preview').src, jpeg);
});

test('face-aware previews explain their crop and clear the message when the draft is discarded', async () => {
  for (const method of ['face', 'center']) {
    const f = fixture();
    const operation = f.scope.regenerateModelAvatar();
    assert.match(f.get('model-avatar-status').textContent, /正在查找/);
    f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'next-cover', crop_method: method });
    await operation;
    assert.equal(f.get('model-avatar-status').hidden, false);
    assert.match(f.get('model-avatar-status').textContent, method === 'face' ? /已围绕人脸裁切/ : /未找到合适的人脸/);
    f.get('model-dialog').close();
    assert.equal(f.get('model-avatar-status').hidden, true);
    assert.equal(f.get('model-avatar-status').textContent, '');
  }
});

test('successive preview generations exclude the last chosen work without saving either preview', async () => {
  const f = fixture();
  let operation = f.scope.regenerateModelAvatar();
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'next-cover' }); await operation;
  operation = f.scope.regenerateModelAvatar();
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[1].payload)), { exclude_book_id: 'next-cover' });
  f.requests[1].resolve({ avatar_upload: 'data:image/jpeg;base64,AA==', cover_book_id: 'third-cover' }); await operation;
  assert.equal(f.requests.every(request => request.url.endsWith('/avatar/regenerate')), true);
  assert.equal(f.model.cover_book_id, 'previous-cover');
  assert.equal(f.scope.refreshes, 0);
});

test('closing and reopening a model aborts its pending avatar and ignores a late response for the same model', async () => {
  const f = fixture(), operation = f.scope.regenerateModelAvatar();
  f.get('model-dialog').close();
  assert.equal(f.requests[0].options.signal.aborted, true);
  f.scope.showModelForm(f.model);
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'late-cover' }); await operation;
  assert.equal(f.get('model-avatar-preview').src, f.model.avatar_url);
  assert.deepEqual(f.payload(), {});
  assert.equal(f.get('model-error').hidden, true);
  assert.equal(f.requests.length, 1);
});

test('uploading an avatar supersedes generation and a late generated preview cannot replace the upload', async () => {
  const f = fixture(), generating = f.scope.regenerateModelAvatar();
  const reading = f.scope.readAvatarEdit('model', { name: 'portrait.webp' });
  assert.equal(f.requests[0].options.signal.aborted, true);
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'late-cover' }); await generating;
  assert.equal(f.get('model-save').disabled, true);
  assert.equal(f.get('model-avatar-preview').src, f.model.avatar_url);
  f.reads[0].resolve(uploaded); await reading;
  assert.equal(f.get('model-avatar-preview').src, uploaded);
  assert.deepEqual(f.payload(), { avatar_upload: uploaded });
  assert.equal(f.get('model-save').disabled, false);
  assert.equal(f.requests.length, 1);
});

test('restoring the default avatar supersedes generation without accepting its result', async () => {
  const f = fixture(), generating = f.scope.regenerateModelAvatar();
  f.scope.resetAvatarEdit('model');
  assert.equal(f.requests[0].options.signal.aborted, true);
  const fallback = f.get('model-avatar-preview').src;
  assert.equal(fallback, '/api/books/previous-cover/cover');
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'late-cover' }); await generating;
  assert.equal(f.get('model-avatar-preview').src, fallback);
  assert.deepEqual(f.payload(), { avatar_reset: true });
  assert.equal(f.get('model-save').disabled, false);
});

test('an avatar response cannot cross an account generation or a changed editing model', async () => {
  for (const change of [scope => { scope.authGeneration += 1; }, scope => { scope.state.editingModel = 'another-model'; }]) {
    const f = fixture(), generating = f.scope.regenerateModelAvatar();
    change(f.scope);
    f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'late-cover' }); await generating;
    assert.equal(f.get('model-avatar-preview').src, f.model.avatar_url);
    assert.deepEqual(f.payload(), {});
  }
  const f = fixture(), generating = f.scope.regenerateModelAvatar();
  f.scope.authGeneration += 1;
  f.requests[0].reject(new Error('Previous account error')); await generating;
  assert.equal(f.get('model-error').hidden, true);
});

test('failed or malformed generation keeps a valid uploaded draft and reports a recoverable error', async () => {
  for (const failure of ['network', 'invalid-image']) {
    const f = fixture(), reading = f.scope.readAvatarEdit('model', { name: 'portrait.webp' });
    f.reads[0].resolve(uploaded); await reading;
    const generating = f.scope.regenerateModelAvatar();
    if (failure === 'network') f.requests[0].reject(new Error('Cannot read associated work'));
    else f.requests[0].resolve({ avatar_upload: 'https://external.example/portrait.jpg' });
    await generating;
    assert.equal(f.get('model-avatar-preview').src, uploaded);
    assert.deepEqual(f.payload(), { avatar_upload: uploaded });
    assert.equal(f.get('model-save').disabled, false);
    assert.equal(f.get('model-error').hidden, false);
    assert.equal(f.requests.length, 1);
  }
});

test('a failed regeneration after an invalid upload keeps Save disabled until the avatar is fixed', async () => {
  const f = fixture(), reading = f.scope.readAvatarEdit('model', { name: 'invalid.webp' });
  f.reads[0].reject(new Error('Invalid image')); await reading;
  const generating = f.scope.regenerateModelAvatar();
  f.requests[0].reject(new Error('Cannot generate')); await generating;
  assert.equal(f.get('model-save').disabled, true);
  assert.throws(f.payload, /有效头像/);
  f.scope.resetAvatarEdit('model');
  assert.equal(f.get('model-save').disabled, false);
  assert.deepEqual(f.payload(), { avatar_reset: true });
});

test('cancelling the file picker preserves an invalid-avatar error and does not enable Save', async () => {
  const f = fixture(), reading = f.scope.readAvatarEdit('model', { name: 'invalid.webp' });
  f.reads[0].reject(new Error('Invalid image')); await reading;
  await f.scope.readAvatarEdit('model', undefined);
  assert.equal(f.get('model-save').disabled, true);
  assert.throws(f.payload, /有效头像/);
});

test('discarding a pending generated avatar makes an AbortError silent and leaves the original model unchanged', async () => {
  const f = fixture(), original = JSON.stringify(f.model), generating = f.scope.regenerateModelAvatar();
  f.get('model-dialog').close();
  const error = new Error('Aborted'); error.name = 'AbortError';
  f.requests[0].reject(error); await generating;
  assert.equal(JSON.stringify(f.model), original);
  assert.equal(f.get('model-error').hidden, true);
  assert.deepEqual(f.payload(), {});
  assert.equal(f.requests.length, 1);
});

test('generation cannot run without model access, during saving, for a new model or on a duplicate click', async () => {
  for (const change of [scope => { scope.allowed = false; }, scope => { scope.modelMutationBusy = true; }, scope => { scope.state.editingModel = null; }]) {
    const f = fixture(); change(f.scope); await f.scope.regenerateModelAvatar();
    assert.equal(f.requests.length, 0);
  }
  const f = fixture(), generating = f.scope.regenerateModelAvatar();
  await f.scope.regenerateModelAvatar();
  assert.equal(f.requests.length, 1);
  f.requests[0].resolve({ avatar_upload: jpeg, cover_book_id: 'next-cover' }); await generating;
  f.scope.showModelForm({ ...f.model, book_count: 0 });
  f.scope.state.models[0].book_count = 0; f.scope.syncModelAvatarRegenerate();
  assert.equal(f.get('model-avatar-regenerate').disabled, true);
});

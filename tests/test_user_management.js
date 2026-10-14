'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Users = require('../static/user-management.js');

test('all shared administration stays exclusive to enabled administrators, even with malformed child permission fields', () => {
  for (const user of [null, {}, { role: 'admin', enabled: false }, { role: 'user', enabled: false, permissions: { edit_metadata: true } }, { role: 'unexpected', permissions: { edit_metadata: true } }]) {
    assert.equal(Users.can(user, 'edit_metadata'), false);
  }
  assert.equal(Users.can({ role: 'admin', enabled: true }, 'edit_metadata'), true);
  const child = { role: 'user', enabled: true, permissions: { edit_metadata: true, scan_libraries: false, manage_models: 'true' } };
  assert.equal(Users.can(child, 'edit_metadata'), false);
  assert.equal(Users.can(child, 'scan_libraries'), false);
  assert.equal(Users.can(child, 'manage_models'), false);
  assert.equal(Users.can(child, 'remove_books'), false);
});

test('the administrator and protected first account cannot appear as manageable child accounts', () => {
  assert.equal(Users.canManage({ id: 1, role: 'admin' }), false);
  assert.equal(Users.canManage({ id: 1, role: 'user' }), false);
  assert.equal(Users.canManage({ id: 2, role: 'admin', enabled: false }), false);
  assert.equal(Users.canManage({ id: 2, role: 'user', enabled: false }), true);
});

test('account saves validate new passwords and never submit an empty password to reset an existing account', () => {
  const libraries = [{ id: 4 }, { id: 7 }];
  const values = { username: '  visitor  ', password: '', enabled: false, library_ids: ['7', '7', 'removed', '4'], permissions: { edit_metadata: true, remove_books: 'true', admin: true } };
  const payload = Users.payload(values, { libraries });
  assert.equal(payload.username, 'visitor'); assert.equal(payload.enabled, false);
  assert.deepEqual(payload.library_ids, [7, 4]); assert.equal(Object.hasOwn(payload, 'password'), false);
  assert.equal(Object.hasOwn(payload, 'permissions'), false);
  assert.throws(() => Users.payload(values, { creating: true, libraries }), /设置密码/);
  assert.throws(() => Users.payload({ ...values, password: 'short', password_confirm: 'short' }), /8–256/);
  assert.throws(() => Users.payload({ ...values, password: 'password123', password_confirm: 'different' }), /不一致/);
  assert.throws(() => Users.payload({ ...values, username: 'x' }), /2–40/);
  assert.throws(() => Users.payload({ ...values, password_confirm: 'onlyconfirmation' }), /先输入新密码/);
  assert.equal(Users.payload({ ...values, password: 'password123', password_confirm: 'password123' }, { creating: true }).password, 'password123');
});

test('personal library visibility permits hiding everything and never submits unauthorized library ids', () => {
  const authorized = [{ id: 4, visible: true }, { id: 7, visible: false }];
  assert.deepEqual(Users.visibilityPayload([], authorized), { visible_library_ids: [] });
  assert.deepEqual(Users.visibilityPayload(['7', '7', '999', '4'], authorized), { visible_library_ids: [7, 4] });
  assert.deepEqual(Users.visibilityPayload(['999'], authorized), { visible_library_ids: [] });
  assert.deepEqual(Users.visibilityPayload(['4']), { visible_library_ids: [] });
});

test('child account validation identifies the basic-tab field to reveal when submitting permissions', () => {
  const cases = [
    [{ username: '' }, {}, 'username'],
    [{ username: 'reader' }, { creating: true }, 'password'],
    [{ username: 'reader', password_confirm: 'confirmation-only' }, {}, 'password'],
    [{ username: 'reader', password: 'short' }, {}, 'password'],
    [{ username: 'reader', password: 'new-password', password_confirm: 'different' }, {}, 'password-confirm']
  ];
  for (const [values, options, field] of cases) {
    assert.throws(() => Users.payload(values, options), error => error.field === field);
  }
});

test('administrators can choose all libraries while child choices stay within the authorized library list', () => {
  const allLibraries = [{ id: 1, visible: false }, { id: 2, visible: true }, { id: 3, visible: false }];
  const childLibraries = [allLibraries[1]];
  assert.deepEqual(Users.visibilityPayload(['1', '3'], allLibraries), { visible_library_ids: [1, 3] });
  assert.deepEqual(Users.visibilityPayload([], allLibraries), { visible_library_ids: [] });
  assert.deepEqual(Users.visibilityPayload(['1', '2', '3'], childLibraries), { visible_library_ids: [2] });
  assert.deepEqual(Users.visibilityPayload(['3'], childLibraries), { visible_library_ids: [] });
});

test('device preference storage remains independent of usernames, logout and other accounts', () => {
  const admin = { id: 1, username: 'admin' }; const child = { id: 2, username: 'admin' };
  assert.notEqual(Users.storageKey('preferences', admin), Users.storageKey('preferences', child));
  assert.equal(Users.storageKey('preferences', admin), Users.storageKey('preferences', { ...admin, username: 'renamed' }));
  assert.notEqual(Users.storageKey('preferences', child), Users.storageKey('preferences', null));
  assert.notEqual(Users.storageKey('preferences', child), Users.storageKey('performance', child));
});

test('reader direction honors the current user preference and effective book direction before library defaults', () => {
  assert.equal(Users.readingDirection({ reading_preferences: { reading_direction: 'rtl' }, reading_direction: 'ltr' }, { reading_direction: 'ltr' }), 'rtl');
  assert.equal(Users.readingDirection({ reading_preferences: { reading_direction: null }, reading_direction: 'rtl' }, { reading_direction: 'ltr' }), 'rtl');
  assert.equal(Users.readingDirection({ reading_direction: 'ltr' }, { reading_direction: 'rtl' }), 'ltr');
  assert.equal(Users.readingDirection({ reading_direction: 'ttb' }, { reading_direction: 'rtl' }), 'ttb');
});

test('reader recovers the stored direction when a hidden library is absent from the catalog', () => {
  const book = { reading_mode: 'double', reading_direction: 'ltr', reading_direction_stored: 'ttb', reading_direction_override: null };
  assert.equal(Users.readingDirection(book, undefined), 'ttb');
  assert.equal(Users.readingDirection({ ...book, reading_preferences: { reading_direction: 'rtl' } }), 'rtl');
  assert.equal(Users.readingDirection({ reading_direction_override: 'ttb', reading_direction: 'ltr' }, { reading_direction: 'rtl' }), 'ttb');
  assert.equal(Users.readingDirection({ reading_direction_override: null, reading_direction: 'ltr' }, { reading_direction: 'ttb' }), 'ttb');
  assert.equal(Users.readingDirection({ reading_mode: 'webtoon', reading_direction: 'ttb' }, { reading_direction: 'rtl' }), 'rtl');
});

test('personal profile validation ignores empty password fields and returns focusable fields for hidden-tab errors', () => {
  assert.deepEqual(Users.profilePayload({ username: '  reader  ' }), { username: 'reader' });
  assert.deepEqual(Users.profilePayload({ username: 'reader' }, { avatar_reset: true }), { username: 'reader', avatar_reset: true });
  const cases = [
    [{ username: '' }, 'username'],
    [{ username: 'reader', current_password: 'old-password' }, 'password'],
    [{ username: 'reader', password: 'short' }, 'password'],
    [{ username: 'reader', password: 'new-password' }, 'current-password'],
    [{ username: 'reader', password: 'new-password', current_password: 'old-password', password_confirm: 'different' }, 'password-confirm']
  ];
  for (const [values, field] of cases) assert.throws(() => Users.profilePayload(values), error => error.field === field);
  assert.deepEqual(Users.profilePayload({ username: 'reader', password: 'new-password', current_password: 'old-password', password_confirm: 'new-password' }), { username: 'reader', password: 'new-password', current_password: 'old-password' });
});

test('combined profile saving preserves committed profile state if library visibility fails and permits a visibility-only retry', async () => {
  const calls = []; let clearedPassword = false;
  await assert.rejects(Users.saveChanges({ profile: { username: 'renamed', password: 'new-password' }, visibility: { visible_library_ids: [] }, saveProfile: async () => { calls.push('profile'); return { user: { username: 'renamed' } }; }, onProfileSaved: () => { clearedPassword = true; }, saveVisibility: async () => { assert.equal(clearedPassword, true); calls.push('visibility'); throw new Error('temporary failure'); } }), error => error.profileSaved === true && error.visibilitySaved === false);
  assert.deepEqual(calls, ['profile', 'visibility']);
  const result = await Users.saveChanges({ profile: null, visibility: { visible_library_ids: [] }, saveProfile: async () => { throw new Error('must not resubmit changed password'); }, saveVisibility: async () => ({ libraries: [] }) });
  assert.deepEqual(result, { profileSaved: false, visibilitySaved: true });
});

test('a failed personal profile save never applies pending library visibility or calls successful-save callbacks', async () => {
  let visibilityCalled = false; let profileCallbackCalled = false;
  await assert.rejects(Users.saveChanges({ profile: { username: 'reader' }, visibility: { visible_library_ids: [] }, saveProfile: async () => { throw new Error('invalid current password'); }, onProfileSaved: () => { profileCallbackCalled = true; }, saveVisibility: async () => { visibilityCalled = true; } }), error => error.profileSaved === false && error.visibilitySaved === false);
  assert.equal(visibilityCalled, false); assert.equal(profileCallbackCalled, false);
});

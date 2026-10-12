/* Shared account rules for the settings UI. The server enforces all access. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleUsers = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const isAdmin = user => user?.role === 'admin' && user.enabled !== false;
  const can = user => isAdmin(user);
  const storageKey = (name, user) => `gravelle-user-${encodeURIComponent(String(user?.id ?? 'signed-out'))}-${name}`;
  const canManage = user => Boolean(user && user.role === 'user' && Number(user.id) !== 1);
  function payload(values, { creating = false, libraries = [] } = {}) {
    const fail = (message, field) => { const error = new Error(message); error.field = field; throw error; };
    const username = String(values.username || '').trim();
    if (username.length < 2 || username.length > 40) fail('用户名需要 2–40 个字符。', 'username');
    const password = String(values.password || '');
    if (!password && values.password_confirm) fail('请先输入新密码。', 'password');
    if (creating && !password) fail('请为新用户设置密码。', 'password');
    if (password && (password.length < 8 || password.length > 256)) fail('密码需要 8–256 个字符。', 'password');
    if (password && password !== String(values.password_confirm || '')) fail('两次输入的密码不一致。', 'password-confirm');
    const knownIds = new Map(libraries.map(library => [String(library.id), library.id]));
    const library_ids = [...new Set((values.library_ids || []).map(String))].filter(id => knownIds.has(id)).map(id => knownIds.get(id));
    return { username, enabled: values.enabled !== false, library_ids, ...(password ? { password } : {}) };
  }
  function visibilityPayload(selected, libraries = []) {
    const knownIds = new Map(libraries.map(library => [String(library.id), library.id]));
    return { visible_library_ids: [...new Set((selected || []).map(String))].filter(id => knownIds.has(id)).map(id => knownIds.get(id)) };
  }
  function readingDirection(book, library) {
    const value = book?.reading_preferences?.reading_direction || book?.reading_direction;
    return value === 'rtl' ? 'rtl' : value === 'ltr' ? 'ltr' : library?.reading_direction === 'rtl' ? 'rtl' : 'ltr';
  }
  function profilePayload(values, avatar = {}) {
    const fail = (message, field) => { const error = new Error(message); error.field = field; throw error; };
    const username = String(values.username || '').trim();
    if (username.length < 2 || username.length > 40) fail('用户名需要 2–40 个字符。', 'username');
    const password = String(values.password || ''); const current = String(values.current_password || ''); const confirm = String(values.password_confirm || '');
    if (!password && (current || confirm)) fail('请先输入新密码。', 'password');
    if (password && (password.length < 8 || password.length > 256)) fail('密码需要 8–256 个字符。', 'password');
    if (password && !current) fail('修改密码时，请输入当前密码。', 'current-password');
    if (password && password !== confirm) fail('两次输入的新密码不一致。', 'password-confirm');
    return { username, ...avatar, ...(password ? { password, current_password: current } : {}) };
  }
  async function saveChanges({ profile, visibility, saveProfile, saveVisibility, onProfileSaved = () => {}, onVisibilitySaved = () => {} }) {
    let profileSaved = false; let visibilitySaved = false;
    try {
      if (profile) { const result = await saveProfile(profile); await onProfileSaved(result); profileSaved = true; }
      if (visibility) { const result = await saveVisibility(visibility); await onVisibilitySaved(result); visibilitySaved = true; }
      return { profileSaved, visibilitySaved };
    } catch (error) { error.profileSaved = profileSaved; error.visibilitySaved = visibilitySaved; throw error; }
  }
  return { isAdmin, can, storageKey, canManage, payload, visibilityPayload, readingDirection, profilePayload, saveChanges };
});

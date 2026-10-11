/* Shared avatar selection helpers. Files are persisted by the profile/model APIs. */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleAvatars = api;
})(typeof window === 'undefined' ? globalThis : window, () => {
  'use strict';
  const MAX_FILE_SIZE = 6 * 1024 * 1024;
  const ACCEPT = 'image/jpeg,image/png,image/webp';
  const DEFAULT_AVATAR = '/default-avatar.png';
  const TYPES = new Set(ACCEPT.split(','));

  function validateFile(file) {
    if (!file || typeof file !== 'object') return { valid: false, error: '请选择头像图片。' };
    if (!TYPES.has(String(file.type || '').toLowerCase())) return { valid: false, error: '头像仅支持 JPG、PNG 或 WebP 图片。' };
    if (!Number.isFinite(file.size) || file.size <= 0) return { valid: false, error: '图片文件为空或无法读取。' };
    if (file.size > MAX_FILE_SIZE) return { valid: false, error: '头像图片不能超过 6 MiB。' };
    return { valid: true, error: '' };
  }

  const aborted = () => new DOMException('已取消选择头像。', 'AbortError');
  function readFile(file, { signal } = {}) {
    const validation = validateFile(file);
    if (!validation.valid) return Promise.reject(new Error(validation.error));
    if (signal?.aborted) return Promise.reject(aborted());
    if (typeof FileReader === 'undefined') return Promise.reject(new Error('当前浏览器不支持读取本地图片。'));
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      let settled = false;
      const finish = (error, result) => {
        if (settled) return;
        settled = true;
        signal?.removeEventListener('abort', cancel);
        reader.onload = reader.onerror = reader.onabort = null;
        if (error) reject(error); else resolve(result);
      };
      const cancel = () => {
        finish(aborted());
        if (reader.readyState === 1) reader.abort();
      };
      reader.onload = () => {
        const value = reader.result;
        if (typeof value !== 'string' || !/^data:image\/(?:jpeg|png|webp);base64,/i.test(value)) {
          finish(new Error('无法读取头像图片，请重新选择。'));
        } else finish(null, value);
      };
      reader.onerror = () => finish(new Error('无法读取头像图片，请重新选择。'));
      reader.onabort = () => finish(aborted());
      signal?.addEventListener('abort', cancel, { once: true });
      try { reader.readAsDataURL(file); } catch (_) { finish(new Error('无法读取头像图片，请重新选择。')); }
    });
  }

  // Avatar URLs supplied by the server are relative paths; local previews use data URLs.
  function imagePath(value) {
    if (typeof value !== 'string') return '';
    if (/^\/(?!\/)/.test(value) || /^data:image\/(?:jpeg|png|webp);base64,/i.test(value)) return value;
    return '';
  }
  function source(entity, fallback = DEFAULT_AVATAR) {
    const candidates = typeof entity === 'string' ? [entity] : [entity?.avatar_url, entity?.cover_url];
    return candidates.map(imagePath).find(Boolean) || imagePath(fallback) || DEFAULT_AVATAR;
  }
  function createImage(entity, { className = 'avatar-image', alt = '', fallback = DEFAULT_AVATAR, lazy = true } = {}) {
    const image = document.createElement('img');
    image.className = className;
    image.alt = alt;
    image.decoding = 'async';
    image.loading = lazy ? 'lazy' : 'eager';
    image.src = source(entity, fallback);
    image.addEventListener('error', () => {
      const replacement = source(fallback);
      if (image.getAttribute('src') !== replacement) image.src = replacement;
    }, { once: true });
    return image;
  }

  return { MAX_FILE_SIZE, ACCEPT, DEFAULT_AVATAR, validateFile, readFile, source, createImage };
});

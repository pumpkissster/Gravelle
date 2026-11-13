/* Group current reminders and remember their read status on this device. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GravelleMessageCenter = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const maxReadMessages = 256;
  const maxSavedOptimizationRecords = 16384;
  const signaturePattern = /^[0-9a-f]{8}:[0-9a-f]{8}$/;

  function fingerprint(value) {
    // Keep storage bounded without persisting the paths or warning text.
    let first = 2166136261, second = 5381;
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index);
      first = Math.imul(first ^ code, 16777619);
      second = Math.imul(second, 33) ^ code;
    }
    return [first, second].map(part => (part >>> 0).toString(16).padStart(8, '0')).join(':');
  }

  function identifier(value) {
    return typeof value === 'string' || Number.isSafeInteger(value) ? String(value) : '';
  }

  function scanMessages(errors, libraries) {
    if (!Array.isArray(errors) || !errors.length) return [];
    const names = new Map();
    for (const library of Array.isArray(libraries) ? libraries : []) {
      const id = identifier(library?.id);
      if (id) names.set(id, typeof library.name === 'string' && library.name ? library.name : '媒体库');
    }

    const groups = new Map();
    for (const error of errors) {
      if (!error || !['string', 'object'].includes(typeof error)) continue;
      const libraryId = identifier(error.library_id);
      if (error.library_id != null && error.library_id !== '' && !names.has(libraryId)) continue;
      const path = typeof error.path === 'string' ? error.path : '';
      const detail = typeof error === 'string' ? error : error.message;
      const message = typeof detail === 'string' && detail ? detail : '扫描失败';
      if (!groups.has(libraryId)) groups.set(libraryId, []);
      groups.get(libraryId).push({ path, message });
    }

    return Array.from(groups, ([libraryId, entries]) => ({
      id: 'scan:' + libraryId,
      kind: 'scan',
      libraryId,
      libraryName: names.get(libraryId) || '媒体库',
      errors: entries,
      count: entries.length,
      signature: fingerprint(JSON.stringify(entries.map(error => JSON.stringify([error.path, error.message])).sort()))
    })).sort((a, b) => Number(!a.libraryId) - Number(!b.libraryId)
      || a.libraryName.localeCompare(b.libraryName, 'zh-CN') || a.id.localeCompare(b.id));
  }

  function optimizationMessage(payload) {
    const pending = new Map();
    if (Array.isArray(payload?.pending_book_ids)) {
      for (const id of payload.pending_book_ids) {
        const value = identifier(id);
        if (value) pending.set(value, [value]);
      }
    }
    if (Array.isArray(payload?.pending_records)) {
      for (const record of payload.pending_records) {
        const id = identifier(record?.id);
        if (id) pending.set(id, [id, identifier(record.generation), identifier(record.rule_revision)]);
      }
    }
    const total = payload?.summary?.pending_decision_books ?? payload?.summary?.pending_books;
    const count = Number.isSafeInteger(total) && total >= 0 ? total : pending.size;
    if (!count) return null;
    const records = Array.from(pending.values(), record => fingerprint(JSON.stringify(record))).sort();
    return {
      id: 'optimization',
      kind: 'optimization',
      count,
      signature: fingerprint(JSON.stringify([count, records])),
      // A count-only or incomplete snapshot cannot establish individual identities.
      records: count === records.length ? records : null,
    };
  }

  function messageKind(id) {
    if (id === 'optimization') return id;
    if (typeof id === 'string' && id.length <= 200 && id.startsWith('scan:')) return 'scan';
    return null;
  }

  function createInbox({ storage, key } = {}) {
    const readSignatures = new Map();
    let readOptimization = new Set();
    if (key && storage) {
      try {
        const raw = storage.getItem(key);
        const saved = typeof raw === 'string' && raw.length <= 500000 ? JSON.parse(raw) : null;
        const entries = Array.isArray(saved) ? saved : saved?.entries;
        if (Array.isArray(entries)) {
          for (const entry of entries.slice(-maxReadMessages)) {
            if (!Array.isArray(entry)) continue;
            const [id, signature] = entry;
            if (messageKind(id) && typeof signature === 'string' && signaturePattern.test(signature)) readSignatures.set(id, signature);
          }
        }
        if (Array.isArray(saved?.optimization)) {
          readOptimization = new Set(saved.optimization.slice(0, maxSavedOptimizationRecords)
            .filter(record => typeof record === 'string' && signaturePattern.test(record)));
        }
      } catch (_) { /* An unreadable preference must not block the inbox. */ }
    }

    function persist() {
      if (!key || !storage) return;
      try {
        storage.setItem(key, JSON.stringify({
          entries: Array.from(readSignatures),
          optimization: Array.from(readOptimization).slice(0, maxSavedOptimizationRecords),
        }));
      } catch (_) { /* Keep the in-memory read status when storage is unavailable. */ }
    }

    function isRead(message) {
      if (!message) return false;
      if (readSignatures.has(message.id) && readSignatures.get(message.id) === message.signature) return true;
      return message.kind === 'optimization' && Boolean(message.records?.length)
        && message.records.every(record => readOptimization.has(record));
    }

    return {
      isRead,
      markRead(message) {
        const kind = messageKind(message?.id);
        if (!kind || kind !== message.kind || typeof message.signature !== 'string' || !signaturePattern.test(message.signature)) return;
        if (kind === 'optimization' && message.records) readOptimization = new Set(message.records);
        readSignatures.delete(message.id);
        readSignatures.set(message.id, message.signature);
        if (readSignatures.size > maxReadMessages) readSignatures.delete(readSignatures.keys().next().value);
        persist();
      },
      reconcile(messages, kind, visibleLibraryIds) {
        const current = new Map(messages.filter(message => message?.kind === kind).map(message => [message.id, message]));
        const visibleLibraries = Array.isArray(visibleLibraryIds) ? new Set(visibleLibraryIds.map(String)) : null;
        let changed = false;
        for (const [id, signature] of readSignatures) {
          if (messageKind(id) !== kind) continue;
          if (kind === 'scan' && visibleLibraries && id !== 'scan:' && !visibleLibraries.has(id.slice(5))) continue;
          if (current.get(id)?.signature !== signature) {
            readSignatures.delete(id);
            changed = true;
          }
        }
        if (kind === 'optimization') {
          const message = current.get('optimization');
          const records = message ? message.records : [];
          if (records && readOptimization.size) {
            const active = new Set(records);
            for (const record of readOptimization) {
              if (active.has(record)) continue;
              readOptimization.delete(record);
              changed = true;
            }
          }
        }
        if (changed) persist();
      }
    };
  }

  return { createInbox, scanMessages, optimizationMessage };
});

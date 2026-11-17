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
  const scanStages = {
    idle: '暂无扫描任务', queued: '等待扫描', enumerating: '正在查找作品',
    inspecting: '正在检查作品', saving: '正在保存扫描结果',
  };
  const indexStages = {
    idle: '暂无索引任务', queued: '等待处理', starting: '正在启动', reconciling: '正在核对图片',
    waiting: '正在等待后台任务名额', reading: '正在读取图片', decoding: '正在建立图片特征',
    paused: '已暂停', disabled: '未启用', stopping: '正在停止', stopped: '任务已停止',
    error: '暂时中断，正在重试',
  };
  const runOutcomes = {
    completed: '已完成', warnings: '已完成，有异常', failed: '失败', paused: '已暂停',
    interrupted: '已中断', superseded: '库设置已变更，等待重扫',
  };
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;

  function taskWork(work) {
    return work?.title ? work.title + (Number.isSafeInteger(work.page) && work.page >= 0 ? `（第 ${work.page + 1} 页）` : '') : '';
  }

  function taskFailure(failure) {
    if (!failure?.reason) return '';
    return [taskWork(failure.work), failure.reason].filter(Boolean).join(' · ');
  }

  function runDuration(entry) {
    if (!Number.isFinite(entry.startedAt) || !Number.isFinite(entry.endedAt)) return '';
    const elapsed = Math.max(0, entry.endedAt - entry.startedAt);
    if (elapsed < 1) {
      const milliseconds = Math.round(elapsed * 1000);
      return milliseconds ? `耗时 ${milliseconds} 毫秒` : '耗时不到 1 毫秒';
    }
    const seconds = Math.floor(elapsed);
    const minutes = Math.floor(seconds / 60);
    return minutes ? `耗时 ${minutes} 分 ${seconds % 60} 秒` : `耗时 ${seconds} 秒`;
  }

  function taskCard(kind, data) {
    const scan = kind === 'scan';
    const stages = scan ? scanStages : indexStages;
    const current = data?.current;
    const stage = Object.hasOwn(stages, data?.stage) ? stages[data.stage] : '暂未读取';
    const target = [current?.libraryName, taskWork(current?.work) || (current?.directory === '.' ? '' : current?.directory)].filter(Boolean).join(' · ');
    const counts = [];
    if (scan) {
      if (current) {
        counts.push(current.mode === 'incremental' ? '增量扫描' : '全量扫描');
        counts.push(`已遍历 ${count(current.directories)} 个目录`);
        if (Number.isSafeInteger(current.total)) counts.push(`已检查 ${count(current.done)} / ${count(current.total)} 本`);
        if (current.warnings) counts.push(`异常 ${count(current.warnings)} 项`);
      }
      if (data?.queued) counts.push(`${count(data.queued)} 个库排队中`);
    } else if (current) {
      if (Number.isSafeInteger(current.totalToCheck)) counts.push(`已核对 ${count(current.checked)} / ${count(current.totalToCheck)} 张`);
      counts.push(`本次已处理 ${count(current.done)} 张 · 失败 ${count(current.failed)} 张`);
    }
    const recent = Array.isArray(data?.recent) ? data.recent.slice(0, 10).map(entry => ({
      label: [scan ? (entry.mode === 'incremental' ? '增量扫描' : '全量扫描') : '',
        Object.hasOwn(runOutcomes, entry.outcome) ? runOutcomes[entry.outcome] : '已结束',
        scan ? entry.libraryName : ''].filter(Boolean).join(' · '),
      detail: [scan ? `遍历 ${count(entry.directories)} 个目录 · 检查 ${count(entry.done)} 本 · 异常 ${count(entry.warnings)} 项`
        : `核对 ${count(entry.checked)} 张 · 处理 ${count(entry.done)} 张 · 失败 ${count(entry.failed)} 张`, runDuration(entry)].filter(Boolean).join(' · '),
      failure: taskFailure(entry.lastFailure), endedAt: entry.endedAt,
    })) : [];
    return { stage, target, counts: counts.join(' · '), lastProgressAt: current?.lastProgressAt,
      failure: current?.error || taskFailure(current?.lastFailure), libraryId: current?.libraryId || data?.recent?.[0]?.libraryId, recent };
  }

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

  return { createInbox, scanMessages, optimizationMessage, taskCard };
});

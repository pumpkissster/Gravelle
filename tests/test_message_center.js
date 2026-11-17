'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { scanMessages, optimizationMessage, createInbox, taskCard } = require('../static/message-center.js');

const libraries = [{ id: 'first', name: 'A' }, { id: 'second', name: 'B' }];
const unreadable = { library_id: 'first', path: 'one', message: '无法读取此目录' };
const unavailable = { library_id: 'first', path: 'two', message: '文件不可用' };
const oneOptimization = {
  summary: { pending_books: 1 },
  pending_book_ids: ['first-book'],
  pending_records: [{ id: 'first-book', generation: 2, rule_revision: 1 }],
};

function memoryStorage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

const unreadMessages = (inbox, messages) => messages.filter(message => !inbox.isRead(message));

test('scan messages group by accessible library and include readable warning details', () => {
  const output = scanMessages([
    { library_id: 'second', library_name: 'Outdated B', path: 'three', message: '无法打开' },
    { library_id: 'private', library_name: 'Private', path: 'secret', message: '不可访问' },
    unreadable,
    unavailable,
    '扫描失败',
  ], libraries);
  assert.deepEqual(output.map(({ id, kind, libraryName, count }) => ({ id, kind, libraryName, count })), [
    { id: 'scan:first', kind: 'scan', libraryName: 'A', count: 2 },
    { id: 'scan:second', kind: 'scan', libraryName: 'B', count: 1 },
    { id: 'scan:', kind: 'scan', libraryName: '媒体库', count: 1 },
  ]);
  assert.deepEqual(output[0].errors, [
    { path: 'one', message: '无法读取此目录' },
    { path: 'two', message: '文件不可用' },
  ]);
});

test('scan messages stay read after reruns with the same warning details', () => {
  const storage = memoryStorage();
  const messages = scanMessages([unreadable, unavailable], libraries);
  createInbox({ storage, key: 'one' }).markRead(messages[0]);

  const reordered = scanMessages([unavailable, { ...unreadable, last_scan: 12345 }], libraries);
  assert.equal(reordered[0].signature, messages[0].signature);
  assert.equal(createInbox({ storage, key: 'one' }).isRead(reordered[0]), true);
  assert.equal(createInbox({ storage, key: 'two' }).isRead(reordered[0]), false);
  const changed = scanMessages([unavailable, { ...unreadable, path: 'new' }], libraries);
  assert.equal(createInbox({ storage, key: 'one' }).isRead(changed[0]), false);
  assert.ok(!storage.getItem('one').includes(unreadable.message));
});

test('optimization read status preserves order independence and resurfaces requeued or different books', () => {
  const pending = {
    summary: { pending_decision_books: 2, pending_books: 2 },
    pending_book_ids: ['first-book', 'second-book'],
    pending_records: [{ id: 'first-book', generation: 1 }, { id: 'second-book', generation: 1 }],
  };
  const message = optimizationMessage(pending);
  const inbox = createInbox();
  inbox.markRead(message);
  assert.equal(message.count, 2);
  assert.equal(message.kind, 'optimization');
  assert.equal(inbox.isRead(optimizationMessage({
    ...pending,
    pending_book_ids: [...pending.pending_book_ids].reverse(),
    pending_records: [...pending.pending_records].reverse(),
    last_scan: 12345,
  })), true);
  assert.equal(inbox.isRead(optimizationMessage({
    ...pending,
    pending_records: [{ id: 'first-book', generation: 2 }, pending.pending_records[1]],
  })), false);
  assert.equal(optimizationMessage({ summary: { pending_books: 0, processing_books: 3 } }), null);
  assert.equal(optimizationMessage({ pending_book_ids: ['a', 'a', 'b'] }).count, 2);
});

test('resolving some optimization work keeps the remaining records read across reloads', () => {
  const storage = memoryStorage();
  const key = 'account';
  const first = { id: 'first-book', generation: 1, rule_revision: 1 };
  const second = { id: 'second-book', generation: 1, rule_revision: 1 };
  const third = { id: 'third-book', generation: 1, rule_revision: 1 };
  const message = (...pending_records) => optimizationMessage({
    pending_records,
    summary: { pending_books: pending_records.length },
  });
  createInbox({ storage, key }).markRead(message(first, second));

  const inbox = createInbox({ storage, key });
  const subset = message(second);
  inbox.reconcile([subset], 'optimization');
  assert.equal(inbox.isRead(subset), true);
  assert.equal(createInbox({ storage, key }).isRead(subset), true);
  assert.ok(!storage.getItem(key).includes(second.id));

  const added = message(second, third);
  inbox.reconcile([added], 'optimization');
  assert.equal(inbox.isRead(added), false);
  const revised = message({ ...second, rule_revision: 2 });
  assert.equal(inbox.isRead(revised), false);
  inbox.reconcile([subset], 'optimization');
  assert.equal(inbox.isRead(subset), true);

  inbox.reconcile([], 'optimization');
  assert.equal(inbox.isRead(subset), false);
});

test('reconciling a confirmed empty category keeps other read messages intact', () => {
  const scan = scanMessages([unreadable], libraries)[0];
  const optimization = optimizationMessage(oneOptimization);
  const inbox = createInbox();
  inbox.markRead(scan);
  inbox.markRead(optimization);
  inbox.reconcile([], 'scan');
  assert.deepEqual(unreadMessages(inbox, [scan, optimization]), [scan]);
  inbox.reconcile([optimization], 'optimization');
  assert.deepEqual(unreadMessages(inbox, [scan, optimization]), [scan]);
});

test('scan reconciliation clears resolved warnings while retaining reads for hidden libraries', () => {
  const second = { ...unavailable, library_id: 'second' };
  const messages = scanMessages([unreadable, second, '一般提醒'], libraries);
  const inbox = createInbox();
  messages.forEach(message => inbox.markRead(message));
  inbox.reconcile([], 'scan', ['first']);
  assert.deepEqual(unreadMessages(inbox, messages).map(message => message.id), ['scan:first', 'scan:']);
  inbox.reconcile([], 'scan', ['second']);
  assert.deepEqual(unreadMessages(inbox, messages), messages);
});

test('previously ignored messages become read while retaining their details', () => {
  const storage = memoryStorage();
  const scan = scanMessages([unreadable], libraries)[0];
  const optimization = optimizationMessage(oneOptimization);
  storage.setItem('account', JSON.stringify({
    entries: [[scan.id, scan.signature], [optimization.id, optimization.signature]],
    optimization: optimization.records,
  }));
  const inbox = createInbox({ storage, key: 'account' });
  const messages = [scan, optimization];
  assert.equal(unreadMessages(inbox, messages).length, 0);
  assert.equal(messages.length, 2);
  assert.deepEqual(messages[0].errors, [{ path: unreadable.path, message: unreadable.message }]);
  assert.equal(messages[1].count, 1);
});

test('invalid or unavailable storage does not block the inbox, and saved reads are bounded', () => {
  const message = optimizationMessage(oneOptimization);
  const storage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('full'); } };
  const inbox = createInbox({ storage, key: 'blocked' });
  inbox.markRead(message);
  assert.equal(inbox.isRead(message), true);

  const bounded = memoryStorage();
  bounded.setItem('account', '{invalid');
  const many = createInbox({ storage: bounded, key: 'account' });
  for (let index = 0; index < 300; index += 1) {
    const id = String(index);
    const [scan] = scanMessages([{ ...unreadable, library_id: id }], [{ id }]);
    many.markRead(scan);
  }
  const saved = JSON.parse(bounded.getItem('account')).entries;
  assert.equal(saved.length, 256);
  assert.equal(saved.at(-1)[0], 'scan:299');
});
test('run details show the waiting work and preserve the latest per-page failure', () => {
  const work = { title: '示例作品', page: 0 };
  const failure = { work, reason: '图片无法解码。' };
  const current = taskCard('index', { stage: 'waiting', current: {
    work, lastProgressAt: 123, done: 3, failed: 1, lastFailure: failure,
  } });
  assert.equal(current.stage, '正在等待后台任务名额');
  assert.equal(current.target, '示例作品（第 1 页）');
  assert.equal(current.lastProgressAt, 123);
  assert.match(current.failure, /示例作品.*图片无法解码/);
  const finished = taskCard('index', { stage: 'idle', recent: [
    { outcome: 'warnings', done: 3, failed: 1, endedAt: 456, lastFailure: failure },
  ] });
  assert.equal(finished.stage, '暂无索引任务');
  assert.equal(finished.target, '');
  assert.equal(finished.recent[0].label, '已完成，有异常');
  assert.equal(finished.recent[0].failure, current.failure);
});

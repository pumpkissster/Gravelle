'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Routes = require('../static/page-router.js');
const Transcoding = require('../static/transcoding-ui.js');

test('settings sections and transcode rules have distinct refreshable addresses', () => {
  for (const section of ['interface', 'performance', 'reading', 'transcoding']) {
    const route = { view: 'settings', section };
    assert.deepEqual(Routes.parse(Routes.hash(route)), route);
  }
  const rules = { view: 'settings', section: 'transcoding', tab: 'rules' };
  assert.equal(Routes.hash(rules), '#/settings/transcoding?tab=rules');
  assert.deepEqual(Routes.parse(Routes.hash(rules)), rules);
  assert.deepEqual(Routes.base({ view: 'book', id: 'b', from: rules }), rules);
});

test('settings routes discard unknown tabs and unrelated or sensitive query parameters', () => {
  assert.deepEqual(Routes.parse('#/settings/transcoding?tab=raw&original=1&password=secret'), { view: 'settings', section: 'transcoding' });
  assert.deepEqual(Routes.parse('#/settings/users?tab=rules'), { view: 'settings' });
  for (const route of ['#/settings/unknown', '#/settings/%2Fetc', '#/settings/transcoding/extra']) {
    assert.deepEqual(Routes.parse(route), { view: 'home' });
  }
});

test('legacy service and user links return to their panels on settings home', () => {
  for (const section of ['service', 'users']) {
    assert.deepEqual(Routes.parse('#/settings/' + section), { view: 'settings' });
    assert.deepEqual(Routes.normalize({ view: 'settings', section }), { view: 'settings' });
    assert.equal(Routes.hash({ view: 'settings', section }), '#/settings');
  }
});

test('displayed progress counts successfully processed matched images only', () => {
  assert.equal(Transcoding.progress({ matched_count: 12, success_count: 10, page_count: 100 }), 83);
  assert.equal(Transcoding.progress({ matched_count: 0, success_count: 0, page_count: 100 }), 0);
  assert.equal(Transcoding.progress({ matched_count: 2, success_count: 9 }), 100);
  assert.equal(Transcoding.progress({ matched_count: 2, success_count: -1 }), 0);
});

test('bulk task actions are limited to compatible current states', () => {
  for (const status of ['queued', 'running']) assert.equal(Transcoding.eligible({ status }, 'pause'), true);
  for (const status of ['pending', 'original', 'ready', 'failed']) assert.equal(Transcoding.eligible({ status }, 'pause'), false);
  assert.equal(Transcoding.eligible({ status: 'paused' }, 'resume'), true);
  assert.equal(Transcoding.eligible({ status: 'running' }, 'resume'), false);
  assert.equal(Transcoding.eligible({ status: 'failed' }, 'retry'), true);
  assert.equal(Transcoding.eligible({ status: 'cancelled' }, 'retry'), true);
  assert.equal(Transcoding.eligible({ status: 'original', matched_count: 12 }, 'transcode'), true);
  assert.equal(Transcoding.eligible({ status: 'queued', matched_count: 12 }, 'transcode'), false);
  assert.equal(Transcoding.eligible({ status: 'pending', matched_count: 0 }, 'transcode'), false);
  assert.equal(Transcoding.eligible({ status: 'pending' }, 'unsafe'), false);
});

test('actual derivative storage is formatted as bytes without estimating original savings', () => {
  assert.equal(Transcoding.bytes(0), '0 B');
  assert.equal(Transcoding.bytes(1024), '1 KiB');
  assert.equal(Transcoding.bytes(2 * 1024 * 1024), '2 MiB');
  assert.equal(Transcoding.defaultRules.enabled, false);
  assert.equal(Transcoding.defaultRules.workers, 1);
});

test('historical pending works on login only establish a reminder baseline', () => {
  const tracker = Transcoding.createPendingTracker();
  assert.equal(tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1 }), null);
  assert.equal(tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1 }), null);
  tracker.reset();
  assert.equal(tracker.observe({ pending_book_ids: ['another-user'], scanning: false, last_scan: 8 }), null);
});

test('a fast completed import reports only new pending works once', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1, libraries: [{ id: 'a', last_scan: 1 }] });
  tracker.expectScan();
  const completed = { pending_book_ids: ['old', 'new'], scanning: false, last_scan: 2, libraries: [{ id: 'a', last_scan: 1 }, { id: 'b', last_scan: 2 }] };
  assert.deepEqual(tracker.observe(completed), { ids: ['new'], count: 1 });
  assert.equal(tracker.observe(completed), null);
  assert.equal(tracker.observe({ ...completed, last_scan: 3 }), null);
});

test('partial scan results wait until completion and are summarized together', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: ['old'], scanning: false, last_scan: 1 });
  assert.equal(tracker.observe({ pending_book_ids: ['old', 'first'], scanning: true, last_scan: 1 }), null);
  assert.equal(tracker.observe({ pending_book_ids: ['old', 'first', 'second'], scanning: true, last_scan: 2 }), null);
  assert.deepEqual(tracker.observe({ pending_book_ids: ['old', 'first', 'second'], scanning: false, last_scan: 3 }), { ids: ['first', 'second'], count: 2 });
});

test('rules and task changes update the baseline without inventing a scan popup', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: [], scanning: false, last_scan: 1 });
  assert.equal(tracker.observe({ pending_book_ids: ['rule-change'], scanning: false, last_scan: 1 }), null);
  assert.equal(tracker.observe({ pending_book_ids: ['rule-change'], scanning: false, last_scan: 2 }), null);
  tracker.observe({ pending_book_ids: [], scanning: false, last_scan: 2 });
  assert.deepEqual(tracker.observe({ pending_book_ids: ['rule-change'], scanning: false, last_scan: 3 }), { ids: ['rule-change'], count: 1 });
});

test('login during an existing scan stays quiet unless the administrator starts a new scan', () => {
  const tracker = Transcoding.createPendingTracker();
  tracker.observe({ pending_book_ids: ['partial'], scanning: true, last_scan: 1 });
  assert.equal(tracker.observe({ pending_book_ids: ['partial', 'rest'], scanning: false, last_scan: 2 }), null);
  tracker.reset();
  tracker.observe({ pending_book_ids: ['partial'], scanning: true, last_scan: 1 });
  tracker.expectScan();
  assert.deepEqual(tracker.observe({ pending_book_ids: ['partial', 'new'], scanning: false, last_scan: 2 }), { ids: ['new'], count: 1 });
});

test('heartbeat timestamps do not replace displayed rows, but progress and errors do', () => {
  const book = { id: 'a', title: '测试作品', status: 'running', matched_count: 10, success_count: 2, updated_at: 1 };
  assert.equal(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, updated_at: 2, rule_revision: 9 }));
  assert.notEqual(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, success_count: 3 }));
  assert.notEqual(Transcoding.bookDisplaySignature(book), Transcoding.bookDisplaySignature({ ...book, error: '处理失败' }));
  assert.equal(Transcoding.statusLabels.running, '优化中');
});

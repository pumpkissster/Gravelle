'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { isISODate, addDays, shiftMonth, calendarGrid, today } = require('../static/release-date-model.js');

test('validates the same ISO date range and Gregorian leap days as publication metadata', () => {
  for (const value of ['0001-01-01', '0099-12-31', '2000-02-29', '2024-02-29', '9999-12-31']) {
    assert.equal(isISODate(value), true, value);
  }
  for (const value of ['', null, 20241111, '0000-12-31', '1900-02-29', '2026-02-29', '2026-04-31', '2026-01-00', '2026-00-01', '2026-13-01', '2026-1-01', '2026-01-01\n', '10000-01-01']) {
    assert.equal(isISODate(value), false, String(value));
  }
});

test('steps across Gregorian dates without remapping early years or crossing supported bounds', () => {
  assert.equal(addDays('2024-02-28', 1), '2024-02-29');
  assert.equal(addDays('2024-03-01', -2), '2024-02-28');
  assert.equal(addDays('0099-12-31', 1), '0100-01-01');
  assert.equal(addDays('0001-01-01', -1), null);
  assert.equal(addDays('9999-12-31', 1), null);
  assert.equal(addDays('2024-01-01', 0.5), null);
});

test('month movement retains the day where possible and clamps it in shorter months', () => {
  assert.equal(shiftMonth('2024-01-31', 1), '2024-02-29');
  assert.equal(shiftMonth('2024-02-29', 12), '2025-02-28');
  assert.equal(shiftMonth('2024-01-15', -1), '2023-12-15');
  assert.equal(shiftMonth('0099-12-31', 1), '0100-01-31');
  assert.equal(shiftMonth('0001-01-01', -1), null);
  assert.equal(shiftMonth('9999-12-31', 1), null);
});

test('builds six Monday-first weeks and leaves out-of-range dates unavailable', () => {
  const november = calendarGrid('2026-11-11');
  assert.equal(november.length, 42);
  assert.deepEqual(november.slice(0, 7), [
    '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-10-31', '2026-11-01',
  ]);
  assert.equal(november.at(-1), '2026-12-06');
  assert.equal(calendarGrid('0001-01-01')[0], '0001-01-01');
  const lastMonth = calendarGrid('9999-12-31');
  assert.equal(lastMonth.filter(Boolean).at(-1), '9999-12-31');
  assert.deepEqual(lastMonth.slice(-9), Array(9).fill(null));
  assert.deepEqual(calendarGrid(''), []);
});

test('today uses the browser’s local calendar day', () => {
  assert.equal(today(new Date(2026, 10, 11, 0, 1)), '2026-11-11');
  assert.equal(today(new Date(NaN)), null);
});

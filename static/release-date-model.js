/* Gregorian dates for publication metadata, stored as YYYY-MM-DD strings. */
(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  else root.GravelleReleaseDateModel = model;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const monthLengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

  function daysInMonth(year, month) {
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return month === 2 && leap ? 29 : monthLengths[month - 1];
  }

  function parse(value) {
    if (typeof value !== 'string' || value.length !== 10) return null;
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!match) return null;
    const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null;
    return { year, month, day };
  }

  function format(year, month, day) {
    if (!(year >= 1 && year <= 9999)) return null;
    return [String(year).padStart(4, '0'), String(month).padStart(2, '0'), String(day).padStart(2, '0')].join('-');
  }

  function utcDate(year, month, day) {
    const date = new Date(0);
    // setUTCFullYear treats 0001–0099 as literal years.
    date.setUTCFullYear(year, month - 1, day);
    return date;
  }

  function formatUTC(date) {
    return format(date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate());
  }

  function isISODate(value) {
    return parse(value) !== null;
  }

  function addDays(value, amount) {
    const parts = parse(value);
    if (!parts || !Number.isSafeInteger(amount)) return null;
    const date = utcDate(parts.year, parts.month, parts.day);
    date.setUTCDate(parts.day + amount);
    return formatUTC(date);
  }

  function shiftMonth(value, amount) {
    const parts = parse(value);
    if (!parts || !Number.isSafeInteger(amount)) return null;
    const index = (parts.year - 1) * 12 + parts.month - 1 + amount;
    if (index < 0 || index >= 9999 * 12) return null;
    const year = Math.floor(index / 12) + 1;
    const month = index % 12 + 1;
    return format(year, month, Math.min(parts.day, daysInMonth(year, month)));
  }

  function calendarGrid(value) {
    const parts = parse(value);
    if (!parts) return [];
    const date = utcDate(parts.year, parts.month, 1);
    const precedingDays = (date.getUTCDay() + 6) % 7;
    date.setUTCDate(1 - precedingDays);
    const dates = [];
    for (let cell = 0; cell < 42; cell += 1) {
      dates.push(formatUTC(date));
      date.setUTCDate(date.getUTCDate() + 1);
    }
    return dates;
  }

  function today(date = new Date()) {
    return format(date.getFullYear(), date.getMonth() + 1, date.getDate());
  }

  return { isISODate, addDays, shiftMonth, calendarGrid, today };
}));

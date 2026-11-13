(() => {
  'use strict';
  const model = window.GravelleReleaseDateModel;

  function enhance(native) {
    const dialog = native.closest('dialog');
    const field = dialog.querySelector('#release-date-field');
    const input = field.querySelector('input');
    const trigger = field.querySelector('button');
    const panel = dialog.querySelector('#release-date-calendar');
    if (!model || typeof panel.showPopover !== 'function') return null;

    const label = native.labels[0];
    const heading = panel.querySelector('#release-date-month');
    const grid = panel.querySelector('#release-date-days');
    const previous = panel.querySelector('#release-date-previous');
    const next = panel.querySelector('#release-date-next');
    const clear = panel.querySelector('#release-date-clear');
    const todayButton = panel.querySelector('#release-date-today');
    const viewport = window.visualViewport;
    let cursor;
    let shownMonth;
    let today;
    const opened = () => panel.matches(':popover-open');

    function validate(showError = false) {
      const value = input.value.trim();
      const invalid = Boolean(value && !model.isISODate(value));
      input.setCustomValidity(invalid ? '请输入有效日期，格式为 YYYY-MM-DD。' : '');
      if (invalid && (showError || value.length === 10)) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
      if (!invalid) native.value = value;
      clear.disabled = !input.value;
    }

    function position() {
      if (!opened()) return;
      const anchor = field.getBoundingClientRect();
      const edge = 12;
      const gap = 7;
      const leftEdge = (viewport?.offsetLeft ?? 0) + edge;
      const topEdge = (viewport?.offsetTop ?? 0) + edge;
      const rightEdge = leftEdge + (viewport?.width ?? window.innerWidth) - edge * 2;
      const bottomEdge = topEdge + (viewport?.height ?? window.innerHeight) - edge * 2;
      panel.style.maxWidth = Math.max(0, rightEdge - leftEdge) + 'px';
      panel.style.maxHeight = Math.max(0, bottomEdge - topEdge) + 'px';
      const { width, height } = panel.getBoundingClientRect();
      const left = Math.max(leftEdge, Math.min(anchor.right - width, rightEdge - width));
      let top = anchor.bottom + gap;
      if (top + height > bottomEdge && anchor.top - gap - height >= topEdge) {
        top = anchor.top - gap - height;
      }
      panel.style.left = left + 'px';
      panel.style.top = Math.max(topEdge, Math.min(top, bottomEdge - height)) + 'px';
      panel.dataset.positioned = 'true';
    }

    function close(restoreFocus = false) {
      if (!opened()) return;
      panel.hidePopover();
      if (restoreFocus) trigger.focus({ preventScroll: true });
    }

    function choose(value) {
      input.value = value;
      validate();
      close(true);
    }

    function draw(value, focus = false) {
      cursor = value;
      shownMonth = value.slice(0, 7);
      const [year, month] = shownMonth.split('-').map(Number);
      const monthLabel = `${year}年${month}月`;
      if (heading.textContent !== monthLabel) heading.textContent = monthLabel;
      previous.disabled = !model.shiftMonth(value, -1);
      next.disabled = !model.shiftMonth(value, 1);
      const rows = [];
      let focusedButton;
      model.calendarGrid(value).forEach((date, index) => {
        if (index % 7 === 0) rows.push(document.createElement('tr'));
        const cell = document.createElement('td');
        rows[rows.length - 1].append(cell);
        if (!date) return;
        cell.setAttribute('aria-selected', String(date === native.value));
        const day = document.createElement('button');
        day.type = 'button';
        day.className = 'release-date-day';
        day.dataset.date = date;
        day.tabIndex = date === value ? 0 : -1;
        const [dayYear, dayMonth, dayNumber] = date.split('-').map(Number);
        day.textContent = String(dayNumber);
        day.setAttribute('aria-label', `${dayYear}年${dayMonth}月${dayNumber}日`);
        if (!date.startsWith(shownMonth)) day.dataset.outside = 'true';
        if (date === today) day.setAttribute('aria-current', 'date');
        if (date === value) focusedButton = day;
        cell.append(day);
      });
      grid.replaceChildren(...rows);
      if (focus) focusedButton?.focus({ preventScroll: true });
    }

    function moveMonth(amount) {
      const target = model.shiftMonth(cursor, amount);
      if (target) draw(target, document.activeElement?.disabled === true);
    }

    panel.addEventListener('beforetoggle', event => {
      if (event.newState !== 'open') return;
      today = model.today();
      draw(native.value || today);
      clear.disabled = !input.value;
    });
    panel.addEventListener('toggle', () => {
      const visible = opened();
      trigger.setAttribute('aria-expanded', String(visible));
      if (visible) {
        position();
        grid.querySelector('[tabindex="0"]')?.focus({ preventScroll: true });
      } else delete panel.dataset.positioned;
    });
    grid.addEventListener('click', event => {
      const day = event.target.closest('button[data-date]');
      if (day) choose(day.dataset.date);
    });
    panel.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close(true);
        return;
      }
      const day = event.target.closest('button[data-date]');
      if (!day || event.ctrlKey || event.altKey || event.metaKey) return;
      const date = day.dataset.date;
      const column = day.parentElement.cellIndex;
      const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -column, End: 6 - column };
      let target;
      if (Object.hasOwn(offsets, event.key)) target = model.addDays(date, offsets[event.key]);
      else if (event.key === 'PageUp' || event.key === 'PageDown') {
        target = model.shiftMonth(date, (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1));
      } else return;
      event.preventDefault();
      if (target) draw(target, true);
    });
    previous.addEventListener('click', () => moveMonth(-1));
    next.addEventListener('click', () => moveMonth(1));
    clear.addEventListener('click', () => choose(''));
    todayButton.addEventListener('click', () => choose(model.today()));
    input.addEventListener('input', () => validate());
    input.addEventListener('blur', () => { input.value = input.value.trim(); validate(true); });
    input.addEventListener('invalid', () => validate(true));
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' && event.altKey && !opened()) {
        event.preventDefault();
        trigger.click();
      }
    });
    dialog.addEventListener('cancel', event => {
      if (!opened()) return;
      event.preventDefault();
      close(true);
    });
    dialog.addEventListener('close', () => close());
    dialog.addEventListener('scroll', position, { capture: true, passive: true });
    window.addEventListener('resize', position);
    viewport?.addEventListener('resize', position);
    viewport?.addEventListener('scroll', position);

    native.hidden = true;
    field.hidden = panel.hidden = false;
    label.htmlFor = input.id;
    return {
      sync() {
        close();
        input.value = native.value;
        validate();
      },
    };
  }

  window.GravelleReleaseDatePicker = { enhance };
})();

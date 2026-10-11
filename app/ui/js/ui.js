// Toasts, dialogs and popovers.

import { h } from './util.js';
import { icon } from './icons.js';

const toastRoot = () => document.getElementById('toasts');

/** toast('Sent in 3.2 s', {type: 'ok'|'error'|'info', action: {label, fn}, timeout}) */
export function toast(message, { type = 'info', action = null, timeout = null, key = null } = {}) {
  const root = toastRoot();
  if (key) {
    const old = root.querySelector(`[data-key="${key}"]`);
    if (old) old.remove();
  }
  const ic = type === 'ok' ? 'check' : type === 'error' ? 'close' : 'sparkles';
  const el = h('div.toast', { role: 'status', 'data-type': type, 'data-key': key || '' });
  el.append(h('span.toast-ic', { html: icon(ic) }), h('span.toast-msg', message));
  if (action) {
    el.append(
      h('button.btn.sm', {
        onclick: () => {
          close();
          action.fn();
        },
      }, action.label),
    );
  }
  el.append(h('button.toast-x', { 'aria-label': 'Dismiss', html: icon('close'), onclick: () => close() }));
  root.append(el);
  requestAnimationFrame(() => el.classList.add('in'));
  const ms = timeout ?? (type === 'error' ? 8000 : 3500);
  let timer = setTimeout(close, ms);
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', () => (timer = setTimeout(close, 2000)));
  function close() {
    clearTimeout(timer);
    el.classList.remove('in');
    setTimeout(() => el.remove(), 200);
  }
  return close;
}

let openModal = null;

export function modal({ title, body, actions = [], wide = false, onClose = null }) {
  closeModal();
  const root = document.getElementById('modalRoot');
  const box = h('div.modal' + (wide ? '.wide' : ''), { role: 'dialog', 'aria-modal': 'true', 'aria-label': title });
  const head = h('div.modal-head', h('h2', title), h('button.icon-btn.ghost', { 'aria-label': 'Close', html: icon('close'), onclick: () => closeModal() }));
  const content = h('div.modal-body');
  if (typeof body === 'string') content.innerHTML = body;
  else if (body) content.append(body);
  box.append(head, content);
  if (actions.length) {
    const foot = h('div.modal-foot');
    for (const a of actions) {
      foot.append(
        h('button.btn' + (a.primary ? '.primary' : '') + (a.danger ? '.danger' : ''), {
          onclick: async () => {
            const keep = await a.fn?.();
            if (keep !== true) closeModal();
          },
        }, a.label),
      );
    }
    box.append(foot);
  }
  const backdrop = h('div.backdrop', { onmousedown: (e) => e.target === backdrop && closeModal() }, box);
  root.append(backdrop);
  openModal = { el: backdrop, onClose };
  requestAnimationFrame(() => backdrop.classList.add('in'));
  const focusable = box.querySelector('.modal-foot .btn.primary, input, button');
  focusable?.focus();
  return box;
}

export function closeModal() {
  if (!openModal) return false;
  const m = openModal;
  openModal = null;
  m.el.remove();
  m.onClose?.();
  return true;
}

export const isModalOpen = () => !!openModal;

export function confirmDialog(title, message, okLabel = 'OK', { danger = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    modal({
      title,
      body: h('p', message),
      actions: [
        { label: 'Cancel', fn: () => { done = true; resolve(false); } },
        { label: okLabel, primary: !danger, danger, fn: () => { done = true; resolve(true); } },
      ],
      onClose: () => !done && resolve(false),
    });
  });
}

// ---------------------------------------------------------------- popovers

let openPop = null;

/** Show `content` in a floating panel anchored to `anchor`. Returns a close function. */
export function popover(anchor, content, { align = 'start', onClose = null, className = '' } = {}) {
  closePopover();
  const el = h('div.popover' + (className ? '.' + className : ''), content);
  document.body.append(el);
  const place = () => {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth;
    const hh = el.offsetHeight;
    let left = align === 'end' ? r.right - w : r.left;
    left = Math.max(8, Math.min(window.innerWidth - w - 8, left));
    let top = r.bottom + 6;
    if (top + hh > window.innerHeight - 8) top = Math.max(8, r.top - hh - 6);
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  };
  place();
  const onDown = (e) => {
    if (!el.contains(e.target) && !anchor.contains(e.target)) closePopover();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      closePopover();
    }
  };
  setTimeout(() => document.addEventListener('pointerdown', onDown, true));
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', place);
  openPop = {
    el,
    anchor,
    close() {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', place);
      el.remove();
      onClose?.();
    },
  };
  anchor.setAttribute('aria-expanded', 'true');
  return closePopover;
}

export function closePopover() {
  if (!openPop) return false;
  const p = openPop;
  openPop = null;
  p.anchor.setAttribute('aria-expanded', 'false');
  p.close();
  return true;
}

export const isPopoverOpen = () => !!openPop;

// ---------------------------------------------------------------- small form widgets

/** Segmented control. options = [[value, label, title?]] */
export function segmented(options, value, onChange, { small = false } = {}) {
  const el = h('div.seg' + (small ? '.sm' : ''), { role: 'radiogroup' });
  const set = (v) => {
    value = v;
    for (const b of el.children) b.setAttribute('aria-checked', String(b.dataset.value === String(v)));
  };
  for (const [v, label, title] of options) {
    el.append(
      h('button', {
        type: 'button',
        role: 'radio',
        'data-value': v,
        title: title || null,
        onclick: () => {
          if (String(value) === String(v)) return;
          set(v);
          onChange(v);
        },
      }, label),
    );
  }
  set(value);
  el.setValue = set;
  return el;
}

/** Labeled slider with a live value readout. */
export function slider({ label, min, max, step = 1, value, fmt = (v) => v, onInput, title = null }) {
  const out = h('output', fmt(value));
  const input = h('input.range', { type: 'range', min, max, step, value, 'aria-label': label });
  const paint = () => {
    const p = ((input.value - min) / (max - min)) * 100;
    input.style.setProperty('--p', `${p}%`);
  };
  input.addEventListener('input', () => {
    out.textContent = fmt(Number(input.value));
    paint();
    onInput(Number(input.value));
  });
  paint();
  const el = h('label.field.slider-field', { title }, h('span.field-label', label, out), input);
  el.setValue = (v) => {
    input.value = v;
    out.textContent = fmt(Number(v));
    paint();
  };
  el.input = input;
  return el;
}

export function paintRange(input) {
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  input.style.setProperty('--p', `${((input.value - min) / (max - min)) * 100}%`);
}

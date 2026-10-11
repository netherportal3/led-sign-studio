// Drawing color, palette, recent colors and the color picker popover.

import { h, hsv, rgbToHsv, rgbToHex, hexToRgb, sameRgb, clamp } from './util.js';
import { popover, closePopover } from './ui.js';

export const PALETTE = [
  '#ff0000', '#ff4000', '#ff8000', '#ffc000', '#ffff00', '#80ff00', '#00ff00', '#00ff80',
  '#00ffff', '#0080ff', '#0000ff', '#8000ff', '#ff00ff', '#ff0080', '#ffffff', '#ffb070',
].map(hexToRgb);

const listeners = new Set();
export const colors = {
  current: [255, 0, 64],
  recent: [],
  onChange(fn) {
    listeners.add(fn);
  },
  set(rgb) {
    if (sameRgb(rgb, this.current)) return;
    this.current = rgb.slice();
    for (const fn of listeners) fn();
  },
  /** Remember a color after it was used to draw. */
  use(rgb) {
    if (!(rgb[0] | rgb[1] | rgb[2])) return;
    const hex = rgbToHex(rgb);
    this.recent = [rgb.slice(), ...this.recent.filter((c) => rgbToHex(c) !== hex)].slice(0, 10);
    try {
      localStorage.setItem('ledsign.recent', JSON.stringify(this.recent));
    } catch {}
    for (const fn of listeners) fn();
  },
  load() {
    try {
      const r = JSON.parse(localStorage.getItem('ledsign.recent') || '[]');
      if (Array.isArray(r)) this.recent = r.filter((c) => Array.isArray(c) && c.length === 3).slice(0, 10);
    } catch {}
  },
};

/**
 * Open the HSV picker next to `anchor`. onPick(rgb) fires live while dragging.
 */
export function openPicker(anchor, rgb, onPick) {
  let [hue, sat, val] = rgbToHsv(...rgb);
  const sv = h('canvas.picker-sv', { width: 232, height: 132 });
  const hueBar = h('canvas.picker-hue', { width: 232, height: 14 });
  const svKnob = h('div.picker-knob');
  const hueKnob = h('div.picker-hknob');
  const hex = h('input.input.mono.picker-hex', { value: rgbToHex(rgb), maxlength: 7, spellcheck: false, 'aria-label': 'Hex color' });
  const preview = h('div.picker-preview');
  const swatches = h('div.picker-swatches');
  for (const c of PALETTE) {
    swatches.append(
      h('button.swatch.sm', {
        style: { '--c': rgbToHex(c) },
        title: rgbToHex(c),
        onclick: () => {
          [hue, sat, val] = rgbToHsv(...c);
          update(true);
        },
      }),
    );
  }

  const content = h(
    'div.picker',
    h('div.picker-svwrap', sv, svKnob),
    h('div.picker-huewrap', hueBar, hueKnob),
    h('div.picker-row', preview, hex),
    swatches,
  );

  function drawSv() {
    const ctx = sv.getContext('2d');
    const w = sv.width;
    const hh = sv.height;
    ctx.fillStyle = `rgb(${hsv(hue, 1, 1)})`;
    ctx.fillRect(0, 0, w, hh);
    const gx = ctx.createLinearGradient(0, 0, w, 0);
    gx.addColorStop(0, '#fff');
    gx.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gx;
    ctx.fillRect(0, 0, w, hh);
    const gy = ctx.createLinearGradient(0, 0, 0, hh);
    gy.addColorStop(0, 'rgba(0,0,0,0)');
    gy.addColorStop(1, '#000');
    ctx.fillStyle = gy;
    ctx.fillRect(0, 0, w, hh);
  }
  function drawHue() {
    const ctx = hueBar.getContext('2d');
    const g = ctx.createLinearGradient(0, 0, hueBar.width, 0);
    for (let i = 0; i <= 6; i++) g.addColorStop(i / 6, `rgb(${hsv(i * 60, 1, 1)})`);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, hueBar.width, hueBar.height);
  }
  function update(fire, fromHex = false) {
    const c = hsv(hue, sat, val);
    drawSv();
    svKnob.style.left = `${sat * 100}%`;
    svKnob.style.top = `${(1 - val) * 100}%`;
    hueKnob.style.left = `${(hue / 360) * 100}%`;
    preview.style.background = rgbToHex(c);
    if (!fromHex) hex.value = rgbToHex(c);
    if (fire) onPick(c);
  }
  function drag(el, fn) {
    el.addEventListener('pointerdown', (e) => {
      try {
        el.setPointerCapture(e.pointerId);
      } catch {}
      const move = (ev) => {
        const r = el.getBoundingClientRect();
        fn(clamp((ev.clientX - r.left) / r.width, 0, 1), clamp((ev.clientY - r.top) / r.height, 0, 1));
        update(true);
      };
      move(e);
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerup', () => el.removeEventListener('pointermove', move), { once: true });
    });
  }
  drag(sv.parentElement, (x, y) => {
    sat = x;
    val = 1 - y;
  });
  drag(hueBar.parentElement, (x) => {
    hue = Math.min(359.9, x * 360);
  });
  hex.addEventListener('input', () => {
    const c = hexToRgb(hex.value);
    if (!c) return;
    [hue, sat, val] = rgbToHsv(...c);
    update(true, true);
  });
  hex.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') closePopover();
  });
  drawHue();
  update(false);
  popover(anchor, content, { className: 'picker-pop' });
  hex.focus();
  hex.select();
}

/** A swatch button that opens the picker. The element has .rgb and .setValue(). */
export function colorButton(rgb, onChange, { label = null } = {}) {
  const btn = h('button.color-btn', { type: 'button', 'aria-label': label || 'Pick a color' });
  const chip = h('span.chip');
  const text = h('span.mono', '');
  btn.append(chip, text);
  btn.rgb = rgb.slice();
  const paint = () => {
    chip.style.background = rgbToHex(btn.rgb);
    text.textContent = rgbToHex(btn.rgb).toUpperCase();
  };
  btn.setValue = (c) => {
    btn.rgb = c.slice();
    paint();
  };
  btn.addEventListener('click', () =>
    openPicker(btn, btn.rgb, (c) => {
      btn.rgb = c;
      paint();
      onChange(c);
    }),
  );
  paint();
  return btn;
}

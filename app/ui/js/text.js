// Text tab: text in the chosen font (pixels from the server), colored and animated here.

import { api } from './api.js';
import { store } from './state.js';
import { OutputBox } from './output.js';
import { colorButton } from './colors.js';
import { h, $, hsv, rgbToHsv, lerp, fromB64, debounce, clamp } from './util.js';
import { segmented, slider } from './ui.js';

const MODES = [
  ['solid', 'Solid'],
  ['rainbow', 'Rainbow'],
  ['wave', 'Rainbow wave', 'Moving rainbow'],
  ['gradient', 'Gradient'],
  ['cycle', 'Color cycle', 'All letters change color together'],
  ['blink', 'Blink'],
];
const ANIMATED = new Set(['wave', 'cycle', 'blink']);

const opts = {
  text: 'Hello!',
  font: null,
  size: null,
  voff: 0,
  mode: 'solid',
  color: [255, 40, 120],
  colorA: [255, 0, 80],
  colorB: [0, 160, 255],
  bg: [0, 0, 0],
  layout: 'static',
  step: 1,
  ms: 30,
};

const maskCache = new Map();

export const textPanel = {
  fonts: [],

  init() {
    const root = $('#tab-text');
    this.out = new OutputBox({ label: 'Text' });

    this.input = h('textarea.input.text-input', { rows: 2, spellcheck: false, 'aria-label': 'Text to show', placeholder: 'Type something' });
    this.input.value = opts.text;
    this.input.addEventListener('input', () => {
      opts.text = this.input.value;
      this.refreshMask();
    });

    this.fontSel = h('select.input', { 'aria-label': 'Font' });
    this.fontSel.addEventListener('change', () => {
      const f = this.fonts.find((x) => x.id === this.fontSel.value);
      opts.font = this.fontSel.value;
      if (f) {
        opts.size = f.default_size;
        this.sizeIn.value = f.default_size;
      }
      this._syncSizeLabel();
      this.refreshMask();
    });
    this.sizeIn = h('input.input.num', { type: 'number', min: 1, max: 64, 'aria-label': 'Size' });
    this.sizeIn.addEventListener('change', () => {
      opts.size = clamp(Number(this.sizeIn.value) || 1, Number(this.sizeIn.min) || 1, Number(this.sizeIn.max) || 64);
      this.sizeIn.value = opts.size;
      this.refreshMask();
    });
    this.sizeLabel = h('span.field-label', 'Size');

    const voff = slider({ label: 'Move up or down', min: -8, max: 8, value: 0, fmt: (v) => (v > 0 ? `${v} down` : v < 0 ? `${-v} up` : 'center'), onInput: (v) => { opts.voff = v; this.rebuild(); } });

    this.modeSeg = segmented(MODES, opts.mode, (v) => { opts.mode = v; this._syncColorFields(); this.rebuild(); }, { small: true });
    this.modeSeg.classList.add('wrap');

    this.colorBtn = colorButton(opts.color, (c) => { opts.color = c; this.rebuild(); }, { label: 'Text color' });
    this.colorABtn = colorButton(opts.colorA, (c) => { opts.colorA = c; this.rebuild(); }, { label: 'Gradient start' });
    this.colorBBtn = colorButton(opts.colorB, (c) => { opts.colorB = c; this.rebuild(); }, { label: 'Gradient end' });
    this.bgBtn = colorButton(opts.bg, (c) => { opts.bg = c; this.rebuild(); }, { label: 'Background color' });
    this.colorField = h('div.field', h('span.field-label', 'Text color'), this.colorBtn);
    this.gradField = h('div.field', h('span.field-label', 'Gradient'), h('div.row-tight', this.colorABtn, this.colorBBtn));

    this.layoutSeg = segmented([['static', 'Still'], ['left', 'Scroll left'], ['right', 'Scroll right']], opts.layout, (v) => {
      opts.layout = v;
      this._syncSpeed();
      this.rebuild();
    }, { small: true });

    this.stepSeg = segmented([['1', '1 px'], ['2', '2 px'], ['3', '3 px']], String(opts.step), (v) => { opts.step = Number(v); this.rebuild(); }, { small: true });
    this.stepField = h('div.field', h('span.field-label', 'Pixels per step'), this.stepSeg);
    this.msSlider = slider({ label: 'Time per step', min: 30, max: 100, step: 5, value: opts.ms, fmt: (v) => `${v} ms`, onInput: (v) => { opts.ms = v; this.rebuild(); } });

    root.append(
      h('div.section',
        h('label.field', h('span.field-label', 'Text'), this.input),
        this.out.el),
      h('div.section',
        h('h3.section-title', 'Font'),
        h('div.grid2',
          h('label.field', h('span.field-label', 'Font'), this.fontSel),
          h('label.field', this.sizeLabel, this.sizeIn)),
        voff),
      h('div.section',
        h('h3.section-title', 'Color'),
        this.modeSeg,
        h('div.grid2', this.colorField, this.gradField, h('div.field', h('span.field-label', 'Background'), this.bgBtn))),
      h('div.section',
        h('h3.section-title', 'Motion'),
        this.layoutSeg,
        h('div.grid2.speed-grid', this.stepField, this.msSlider)),
    );
    this._syncColorFields();
    this._syncSpeed();
    this.loadFonts();
    store.on('change', () => {
      if (this.mask && (this.out.W !== store.width || this.out.H !== store.height)) this.rebuild();
    });
  },

  _syncColorFields() {
    this.colorField.hidden = !(opts.mode === 'solid' || opts.mode === 'blink' || opts.mode === 'cycle');
    this.colorField.querySelector('.field-label').textContent = opts.mode === 'cycle' ? 'Start color' : 'Text color';
    this.gradField.hidden = opts.mode !== 'gradient';
  },

  _syncSpeed() {
    this.stepField.hidden = opts.layout === 'static';
  },

  _syncSizeLabel() {
    const f = this.fonts.find((x) => x.id === opts.font);
    const pixel = f && f.kind === 'pixel';
    this.sizeLabel.textContent = pixel ? 'Scale' : 'Size';
    this.sizeIn.min = f?.min_size ?? 1;
    this.sizeIn.max = f?.max_size ?? (pixel ? 4 : 64);
  },

  async loadFonts() {
    try {
      this.fonts = (await api.fonts()) || [];
    } catch (e) {
      this.out.note('Could not load fonts. Is the app still running?', { label: 'Try again', fn: () => this.loadFonts() }, 'error');
      return;
    }
    this.fontSel.replaceChildren();
    const groups = [['pixel', 'Pixel fonts'], ['ttf', 'PC fonts']];
    for (const [kind, label] of groups) {
      const list = this.fonts.filter((f) => f.kind === kind);
      if (!list.length) continue;
      const g = h('optgroup', { label });
      for (const f of list) g.append(h('option', { value: f.id }, f.label));
      this.fontSel.append(g);
    }
    const first = this.fonts[0];
    if (first && !this.fonts.some((f) => f.id === opts.font)) {
      opts.font = first.id;
      opts.size = first.default_size;
    }
    this.fontSel.value = opts.font;
    this.sizeIn.value = opts.size ?? 1;
    this._syncSizeLabel();
    this.refreshMask();
  },

  refreshMask: debounce(function () {
    textPanel._fetchMask();
  }, 180),

  async _fetchMask() {
    if (!opts.font) return;
    const text = opts.text.replace(/\r/g, '');
    if (!text.trim()) {
      this.mask = null;
      this.out.note('Type some text to see it here.', null, 'info');
      this.out.set([new Uint8ClampedArray(store.width * store.height * 3)], [1000]);
      return;
    }
    const key = `${opts.font}|${opts.size}|${text}`;
    let m = maskCache.get(key);
    if (!m) {
      const seq = (this._seq = (this._seq || 0) + 1);
      try {
        const r = await api.textMask(text, opts.font, opts.size);
        m = { width: r.width, height: r.height, mask: fromB64(r.mask), cropped: !!r.cropped };
        maskCache.set(key, m);
        if (maskCache.size > 60) maskCache.delete(maskCache.keys().next().value);
      } catch (e) {
        if (seq === this._seq) this.out.note(e.message || 'Could not draw that text.', null, 'error');
        return;
      }
      if (seq !== this._seq) return;
    }
    this.mask = m;
    this.rebuild();
  },

  rebuild() {
    if (!this.mask) return;
    const r = buildTextFrames(this.mask, store.width, store.height, opts);
    if (r.tooLong) {
      this.out.note("It's too long to fit. Scroll it instead?", {
        label: 'Scroll left',
        fn: () => {
          opts.layout = 'left';
          this.layoutSeg.setValue('left');
          this._syncSpeed();
          this.rebuild();
        },
      });
    } else if (r.tooTall || this.mask.cropped) {
      this.out.note('The text is taller than the sign, so part of it is cut off. Try a smaller size.', null, 'warn');
    } else {
      this.out.note(null);
    }
    this.out.label = `Text "${opts.text.trim().slice(0, 18)}${opts.text.trim().length > 18 ? '...' : ''}"`;
    this.out.set(r.frames, r.durations);
  },
};

/** Color and animate a text mask. Returns {frames, durations, tooLong, tooTall}. */
export function buildTextFrames(m, W, H, o) {
  const mw = m.width;
  const mh = m.height;
  const mask = m.mask;
  const top = Math.floor((H - mh) / 2) + o.voff;
  const lit = (sx, y) => {
    const r = y - top;
    if (r < 0 || r >= mh || sx < 0 || sx >= mw) return false;
    return mask[r * mw + sx] > 127;
  };
  // Ink extents, so centering and scrolling use the visible width.
  let minX = mw;
  let maxX = -1;
  let minY = mh;
  let maxY = -1;
  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      if (mask[y * mw + x] > 127) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  const empty = maxX < 0;
  const inkW = empty ? 0 : maxX - minX + 1;
  const animated = ANIMATED.has(o.mode);
  const scroll = o.layout !== 'static';
  const tooLong = !scroll && inkW > W;
  const tooTall = !empty && (maxY - minY + 1 > H || top + minY < 0 || top + maxY >= H);

  let N;
  let ms;
  const step = clamp(o.step || 1, 1, 3);
  let P = 0;
  if (scroll) {
    // The strip is the text plus one panel width of gap, so the text leaves before it comes back.
    P = Math.ceil((inkW + W) / step) * step;
    N = P / step;
    ms = o.ms;
  } else if (!animated) {
    N = 1;
    ms = 1000;
  } else if (o.mode === 'blink') {
    N = 2;
    ms = 500;
  } else {
    N = o.mode === 'wave' ? 36 : 60;
    ms = o.ms;
  }
  const cyclesOver = (targetMs) => (scroll ? Math.max(1, Math.round((N * ms) / targetMs)) : 1);
  const kWave = cyclesOver(1600);
  const kCycle = cyclesOver(3000);
  const kBlink = cyclesOver(1000);
  const span = clamp(inkW, 16, 96);
  const startHue = rgbToHsv(...o.color)[0];
  const bg = o.bg;
  const x0 = Math.floor((W - inkW) / 2);

  const frames = [];
  for (let i = 0; i < N; i++) {
    const out = new Uint8ClampedArray(W * H * 3);
    const ph = i / N;
    let offset;
    if (!scroll) offset = minX - x0;
    else if (o.layout === 'left') offset = i * step;
    else offset = inkW - W - i * step;
    const blinkOff = o.mode === 'blink' && (scroll ? (kBlink * ph) % 1 >= 0.5 : i === 1);
    const cycleColor = o.mode === 'cycle' ? hsv(startHue + 360 * kCycle * ph, 1, Math.max(...o.color) / 255 || 1) : null;
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        let sx;
        if (scroll) {
          const p = (((offset + x) % P) + P) % P;
          sx = p < inkW ? p + minX : -1;
        } else sx = x + offset;
        const k = (y * W + x) * 3;
        let c = bg;
        if (sx >= 0 && !blinkOff && lit(sx, y)) {
          const tx = sx - minX;
          switch (o.mode) {
            case 'rainbow':
              c = hsv((tx / span) * 360, 1, 1);
              break;
            case 'wave':
              c = hsv((x / W) * 360 + y * 5 - 360 * kWave * ph, 1, 1);
              break;
            case 'gradient': {
              const t = inkW > 1 ? tx / (inkW - 1) : 0;
              c = [lerp(o.colorA[0], o.colorB[0], t), lerp(o.colorA[1], o.colorB[1], t), lerp(o.colorA[2], o.colorB[2], t)];
              break;
            }
            case 'cycle':
              c = cycleColor;
              break;
            default:
              c = o.color;
          }
        }
        out[k] = c[0];
        out[k + 1] = c[1];
        out[k + 2] = c[2];
      }
    }
    frames.push(out);
  }
  return { frames, durations: frames.map(() => ms), tooLong, tooTall };
}

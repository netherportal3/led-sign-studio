// The LED canvas editor: drawing tools, toolbar, tool options and the color bar.

import { store } from './state.js';
import { LedView } from './render.js';
import { colors, PALETTE, openPicker } from './colors.js';
import { h, $, hsv, rgbToHex, clamp } from './util.js';
import { icon } from './icons.js';
import { segmented, toast } from './ui.js';
import * as T from './tools.js';

const SHAPES = new Set(['line', 'rect', 'ellipse']);
const STROKES = new Set(['pencil', 'eraser', 'rainbow']);

export const editor = {
  tool: 'pencil',
  prevTool: 'pencil',
  mirror: false,
  onion: false,
  shapeFill: false,
  rainbowHue: 0,
  drag: null,
  hover: null,
  playIndex: null,
  view: null,
  _listeners: new Set(),
  onState(fn) {
    this._listeners.add(fn);
  },
  _notify() {
    for (const fn of this._listeners) fn();
  },

  init() {
    this.stage = $('#stage');
    this.canvas = $('#ledCanvas');
    this.overlay = $('#overlayCanvas');
    this.view = new LedView(this.canvas, { glow: true });
    this.octx = this.overlay.getContext('2d');
    this._buildToolbar();
    this._buildToolOpts();
    this._buildColorbar();
    this._bindPointer();
    new ResizeObserver(() => this.layout()).observe(this.stage);
    window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener?.('change', () => this.layout());
    store.on('change', () => {
      if (store.width !== this.view.W || store.height !== this.view.H) this.layout();
      else this.render();
      this._syncButtons();
    });
    this.layout();
  },

  layout() {
    const W = store.width;
    const H = store.height;
    const cs = getComputedStyle(this.stage);
    const availW = this.stage.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const availH = this.stage.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const cell = clamp(Math.floor(Math.min(availW / W, availH / H)), 3, 48);
    this.view.resize(W, H, cell);
    const dpr = window.devicePixelRatio || 1;
    this.overlay.width = Math.round(W * cell * dpr);
    this.overlay.height = Math.round(H * cell * dpr);
    this.overlay.style.width = `${W * cell}px`;
    this.overlay.style.height = `${H * cell}px`;
    // Resizing clears the canvas, so repaint right away instead of on the next frame.
    this._drawNow();
  },

  /** What the canvas currently shows. */
  shownPixels() {
    if (this.playIndex != null && store.frames[this.playIndex]) return store.frames[this.playIndex].pixels;
    if (this.drag && this.drag.buf) return this.drag.buf;
    return store.frame.pixels;
  },

  render() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this._drawNow();
    });
  },

  _drawNow() {
    const px = this.shownPixels();
    let under = null;
    if (this.onion && this.playIndex == null && store.frames.length > 1) {
      const prev = (store.current - 1 + store.frames.length) % store.frames.length;
      under = store.frames[prev].pixels;
    }
    this.view.draw(px, under, 0.25);
    this._drawOverlay();
  },

  _drawOverlay() {
    const ctx = this.octx;
    const dpr = window.devicePixelRatio || 1;
    const cell = this.view.cssCell * dpr;
    ctx.clearRect(0, 0, this.overlay.width, this.overlay.height);
    if (this.playIndex != null) return;
    const W = store.width;
    if (this.mirror) {
      ctx.save();
      ctx.strokeStyle = 'rgba(61, 232, 255, 0.55)';
      ctx.lineWidth = Math.max(1, dpr);
      ctx.setLineDash([4 * dpr, 4 * dpr]);
      const x = (W / 2) * cell;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, this.overlay.height);
      ctx.stroke();
      ctx.restore();
    }
    if (this.hover) {
      const cells = [[this.hover.x, this.hover.y]];
      if (this.mirror) cells.push([W - 1 - this.hover.x, this.hover.y]);
      ctx.save();
      ctx.lineWidth = Math.max(1.5, 1.5 * dpr);
      ctx.strokeStyle = 'rgba(255,255,255,0.85)';
      for (const [x, y] of cells) {
        ctx.beginPath();
        ctx.arc((x + 0.5) * cell, (y + 0.5) * cell, cell * 0.46, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }
  },

  // ------------------------------------------------------------ tools

  setTool(t) {
    if (!T.TOOL_INFO[t]) return;
    if (t !== this.tool) {
      if (t === 'picker') this.prevTool = this.tool;
      this.tool = t;
    }
    this._syncButtons();
    this._syncToolOpts();
  },
  toggleMirror() {
    this.mirror = !this.mirror;
    this._syncButtons();
    this.render();
  },
  toggleOnion() {
    this.onion = !this.onion;
    this._syncButtons();
    this.render();
  },

  // ------------------------------------------------------------ whole-frame operations

  _op(label, fn) {
    this.onPlaybackInterrupt?.();
    const i = store.current;
    store.setPixels(i, fn(store.frame.pixels, store.width, store.height), label);
  },
  shift(dx, dy) {
    this.onPlaybackInterrupt?.();
    const i = store.current;
    const out = T.shift(store.frame.pixels, store.width, store.height, dx, dy);
    store.commit('Shift', (s) => (s.frames[i] = { pixels: out, ms: s.frames[i].ms }), { coalesce: true });
  },
  flipH() {
    this._op('Flip horizontal', T.flipH);
  },
  flipV() {
    this._op('Flip vertical', T.flipV);
  },
  clear() {
    this._op('Clear', (px) => new Uint8ClampedArray(px.length));
  },

  // ------------------------------------------------------------ pointer

  _cellAt(e, clampToGrid = false) {
    const r = this.canvas.getBoundingClientRect();
    let x = Math.floor(((e.clientX - r.left) / r.width) * store.width);
    let y = Math.floor(((e.clientY - r.top) / r.height) * store.height);
    if (clampToGrid) {
      x = clamp(x, 0, store.width - 1);
      y = clamp(y, 0, store.height - 1);
    }
    return { x, y };
  },

  _bindPointer() {
    const c = this.overlay;
    c.addEventListener('contextmenu', (e) => e.preventDefault());
    c.addEventListener('pointerdown', (e) => this._down(e));
    c.addEventListener('pointermove', (e) => this._move(e));
    c.addEventListener('pointerup', (e) => this._up(e));
    c.addEventListener('pointercancel', (e) => this._up(e));
    c.addEventListener('pointerleave', () => {
      this.hover = null;
      this._posText('');
      this.render();
    });
  },

  _plot(buf, x, y, c) {
    const W = store.width;
    const H = store.height;
    let changed = T.plot(buf, W, H, x, y, c);
    if (this.mirror) changed = T.plot(buf, W, H, W - 1 - x, y, c) || changed;
    return changed;
  },

  _down(e) {
    if (e.button !== 0 && e.button !== 2) return;
    e.preventDefault();
    if (this.drag) return;
    this.onPlaybackInterrupt?.();
    try {
      this.overlay.setPointerCapture(e.pointerId);
    } catch {}
    const { x, y } = this._cellAt(e);
    const W = store.width;
    const H = store.height;
    const erase = e.button === 2 || this.tool === 'eraser';
    const tool = this.tool;
    if (!erase && (tool === 'picker' || e.altKey)) {
      this.drag = { mode: 'pick', fromTool: tool === 'picker' };
      this._pick(x, y);
      return;
    }
    const color = erase ? T.BLACK : colors.current;
    const base = store.frame.pixels;
    const index = store.current;
    if (tool === 'fill') {
      const buf = base.slice();
      let changed = T.floodFill(buf, W, H, x, y, color);
      if (this.mirror) changed = T.floodFill(buf, W, H, W - 1 - x, y, color) || changed;
      if (changed) {
        store.setPixels(index, buf, 'Fill');
        if (!erase) colors.use(color);
      }
      return;
    }
    this.drag = {
      mode: SHAPES.has(tool) ? 'shape' : 'stroke',
      tool,
      index,
      base,
      buf: base.slice(),
      x0: x,
      y0: y,
      lx: x,
      ly: y,
      color,
      erase,
      changed: false,
    };
    if (this.drag.mode === 'stroke') this._paint(x, y);
    else this._shape(x, y, e.shiftKey);
    this.render();
  },

  _paint(x, y) {
    const d = this.drag;
    let c = d.color;
    if (d.tool === 'rainbow' && !d.erase) {
      if (d.lastCell !== `${x},${y}`) this.rainbowHue = (this.rainbowHue + 9) % 360;
      c = hsv(this.rainbowHue, 1, 1);
    }
    d.lastCell = `${x},${y}`;
    if (this._plot(d.buf, x, y, c)) d.changed = true;
  },

  _shape(x, y, shiftKey) {
    const d = this.drag;
    let [ex, ey] = [x, y];
    if (shiftKey) [ex, ey] = d.tool === 'line' ? T.constrainLine(d.x0, d.y0, x, y) : T.constrainSquare(d.x0, d.y0, x, y);
    const cells =
      d.tool === 'line'
        ? T.lineCells(d.x0, d.y0, ex, ey)
        : d.tool === 'rect'
          ? T.rectCells(d.x0, d.y0, ex, ey, this.shapeFill)
          : T.ellipseCells(d.x0, d.y0, ex, ey, this.shapeFill);
    d.buf = d.base.slice();
    d.changed = false;
    for (const [cx, cy] of cells) if (this._plot(d.buf, cx, cy, d.color)) d.changed = true;
  },

  _pick(x, y) {
    if (x < 0 || y < 0 || x >= store.width || y >= store.height) return;
    const c = T.getPx(this.shownPixels(), store.width, x, y);
    if (c[0] | c[1] | c[2]) colors.set(c);
  },

  _move(e) {
    const { x, y } = this._cellAt(e);
    const inside = x >= 0 && y >= 0 && x < store.width && y < store.height;
    const hv = inside ? { x, y } : null;
    if (!this.hover || !hv || hv.x !== this.hover.x || hv.y !== this.hover.y) {
      this.hover = hv;
      this._posText(inside ? `x ${x}  y ${y}` : '');
      this.render();
    }
    const d = this.drag;
    if (!d) return;
    if (d.mode === 'pick') {
      this._pick(x, y);
      return;
    }
    if (d.mode === 'stroke') {
      const pts = T.lineCells(d.lx, d.ly, x, y);
      for (let i = 1; i < pts.length; i++) this._paint(pts[i][0], pts[i][1]);
      d.lx = x;
      d.ly = y;
    } else {
      this._shape(x, y, e.shiftKey);
    }
    this.render();
  },

  _up() {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    if (d.mode === 'pick') {
      // Back to the tool used before, but a picked color is for drawing, not erasing.
      if (d.fromTool) this.setTool(this.prevTool === 'picker' || this.prevTool === 'eraser' ? 'pencil' : this.prevTool);
      return;
    }
    if (d.changed && store.frames[d.index]) {
      const label = d.erase ? 'Erase' : T.TOOL_INFO[d.tool].label;
      store.setPixels(d.index, d.buf, label);
      if (!d.erase && d.tool !== 'rainbow') colors.use(d.color);
    } else {
      this.render();
    }
  },

  _posText(t) {
    if (this.posEl) this.posEl.textContent = t;
  },

  // ------------------------------------------------------------ UI

  _buildToolbar() {
    const bar = $('#toolbar');
    const btn = (id, ic, title, fn, cls = '') =>
      h('button.tool' + cls, { type: 'button', 'data-id': id, title, 'aria-label': title, html: icon(ic), onclick: fn });
    for (const [id, info] of Object.entries(T.TOOL_INFO)) {
      bar.append(btn(id, info.icon, `${info.label} (${info.key})`, () => this.setTool(id)));
    }
    bar.append(h('div.tool-sep'));
    bar.append(btn('mirror', 'mirror', 'Mirror drawing (M)', () => this.toggleMirror(), '.toggle'));
    bar.append(btn('onion', 'onion', 'Onion skin: show the previous frame faintly (K)', () => this.toggleOnion(), '.toggle'));
    bar.append(h('div.tool-sep'));
    bar.append(btn('flipH', 'flipH', 'Flip left to right (H)', () => this.flipH()));
    bar.append(btn('flipV', 'flipV', 'Flip upside down (V)', () => this.flipV()));
    bar.append(btn('clear', 'clear', 'Clear this frame (Delete)', () => this.clear()));
    bar.append(h('div.tool-grow'));
    bar.append(btn('undo', 'undo', 'Undo (Ctrl+Z)', () => this.undo()));
    bar.append(btn('redo', 'redo', 'Redo (Ctrl+Y)', () => this.redo()));
    this.toolbar = bar;
    this._syncButtons();
  },

  undo() {
    const label = store.undo();
    if (!label) toast('Nothing to undo', { key: 'undo', timeout: 1200 });
  },
  redo() {
    const label = store.redo();
    if (!label) toast('Nothing to redo', { key: 'undo', timeout: 1200 });
  },

  _syncButtons() {
    if (!this.toolbar) return;
    for (const b of this.toolbar.querySelectorAll('.tool')) {
      const id = b.dataset.id;
      if (T.TOOL_INFO[id]) b.setAttribute('aria-pressed', String(id === this.tool));
      if (id === 'mirror') b.setAttribute('aria-pressed', String(this.mirror));
      if (id === 'onion') {
        b.setAttribute('aria-pressed', String(this.onion));
        b.disabled = store.frames.length < 2;
      }
      if (id === 'undo') b.disabled = !store.canUndo;
      if (id === 'redo') b.disabled = !store.canRedo;
    }
    this.overlay.dataset.tool = this.tool;
    this._notify();
  },

  _buildToolOpts() {
    const el = $('#toolOpts');
    this.toolNameEl = h('span.toolopt-name');
    this.toolHintEl = h('span.toolopt-hint');
    this.fillSeg = segmented(
      [['outline', 'Outline'], ['filled', 'Filled']],
      'outline',
      (v) => {
        this.shapeFill = v === 'filled';
      },
      { small: true },
    );
    this.posEl = h('span.toolopt-pos.mono');
    el.append(h('span.toolopt-ic'), this.toolNameEl, this.fillSeg, this.toolHintEl, h('span.grow'), this.posEl);
    this._syncToolOpts();
  },

  _syncToolOpts() {
    if (!this.toolNameEl) return;
    const info = T.TOOL_INFO[this.tool];
    $('#toolOpts .toolopt-ic').innerHTML = icon(info.icon);
    this.toolNameEl.textContent = info.label;
    this.toolHintEl.textContent = info.hint;
    this.fillSeg.hidden = !(this.tool === 'rect' || this.tool === 'ellipse');
  },

  _buildColorbar() {
    const bar = $('#colorbar');
    const cur = h('button.current-color', { type: 'button', title: 'Pick any color', 'aria-label': 'Drawing color' },
      h('span.chip'), h('span.mono.hex'));
    cur.addEventListener('click', () => openPicker(cur, colors.current, (c) => colors.set(c)));
    const pal = h('div.swatches', { role: 'list', 'aria-label': 'Palette' });
    for (const c of PALETTE) {
      pal.append(h('button.swatch', { type: 'button', title: rgbToHex(c), style: { '--c': rgbToHex(c) }, 'data-hex': rgbToHex(c), onclick: () => colors.set(c) }));
    }
    const recentWrap = h('div.recent', h('span.label-sm', 'Recent'));
    const recent = h('div.swatches.recent-list');
    recentWrap.append(recent);
    bar.append(cur, pal, recentWrap);
    const sync = () => {
      const hex = rgbToHex(colors.current);
      cur.querySelector('.chip').style.background = hex;
      cur.querySelector('.hex').textContent = hex.toUpperCase();
      for (const s of pal.children) s.setAttribute('aria-pressed', String(s.dataset.hex === hex));
      recent.replaceChildren(
        ...colors.recent.map((c) =>
          h('button.swatch.sm', { type: 'button', title: rgbToHex(c), style: { '--c': rgbToHex(c) }, 'aria-pressed': String(rgbToHex(c) === hex), onclick: () => colors.set(c) }),
        ),
      );
      if (!colors.recent.length) recent.append(h('span.empty-sm', 'Colors you draw with show here'));
      if (this.tool === 'eraser') this.setTool('pencil');
    };
    colors.onChange(sync);
    sync();
  },
};

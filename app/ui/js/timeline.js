// Frames timeline: thumbnails, frame operations, durations, playback and the upload estimate.

import { store, clampMs, DEFAULT_MS, MIN_MS, BIG_UPLOAD_BYTES } from './state.js';
import { drawDots } from './render.js';
import { editor } from './editor.js';
import { api } from './api.js';
import { h, $, debounce, fmtBytes, fmtSeconds, toB64 } from './util.js';
import { icon } from './icons.js';
import { segmented } from './ui.js';

const thumbCache = new WeakMap(); // pixel buffer (never changed once committed) -> canvas

export const timeline = {
  playing: false,
  timer: 0,
  dragFrom: null,

  init() {
    this.root = $('#timeline');
    this._buildControls();
    this.strip = h('div.strip', { role: 'listbox', 'aria-label': 'Frames' });
    this.root.append(this.strip);
    this.strip.addEventListener('wheel', (e) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        this.strip.scrollLeft += e.deltaY;
        e.preventDefault();
      }
    }, { passive: false });
    this._bindDnD();
    store.on('change', (e) => {
      this.render(e.kind === 'select');
      if (e.edit) this.scheduleEstimate();
    });
    editor.onPlaybackInterrupt = () => this.stop();
    this.render();
    this.scheduleEstimate();
  },

  _buildControls() {
    const b = (ic, label, title, fn, cls = '') =>
      h('button.btn.sm' + cls, { type: 'button', title, onclick: fn, html: icon(ic) + (label ? `<span>${label}</span>` : '') });
    this.playBtn = b('play', 'Play', 'Play the animation (Space)', () => this.toggle(), '.play-btn');
    const add = b('plus', 'Add', 'Add a blank frame (N)', () => this.add());
    const dup = b('copy', 'Duplicate', 'Duplicate this frame (Ctrl+D)', () => this.duplicate());
    this.delBtn = b('trash', '', 'Delete this frame (Ctrl+Delete)', () => this.remove(), '.icon-only');
    this.leftBtn = b('left', '', 'Move frame earlier (Alt+Left)', () => this.move(-1), '.icon-only');
    this.rightBtn = b('right', '', 'Move frame later (Alt+Right)', () => this.move(1), '.icon-only');
    this.countEl = h('span.tl-count');
    this.allInput = h('input.input.num', { type: 'number', min: 1, max: 10000, value: DEFAULT_MS, 'aria-label': 'Duration for all frames' });
    this.unit = 'ms';
    const unitSeg = segmented([['ms', 'ms'], ['fps', 'fps']], 'ms', (v) => {
      const val = Number(this.allInput.value) || DEFAULT_MS;
      this.unit = v;
      this.allInput.value = v === 'fps' ? Math.round(1000 / Math.max(MIN_MS, val)) : Math.round(1000 / Math.max(1, val));
    }, { small: true });
    const apply = h('button.btn.sm', { type: 'button', onclick: () => this.setAll() }, 'Set all');
    this.allInput.addEventListener('keydown', (e) => e.key === 'Enter' && this.setAll());
    this.estEl = h('span.tl-est', { title: 'How much has to be uploaded to the sign' });
    this.root.append(
      h('div.tl-controls',
        this.playBtn, h('span.vsep'), add, dup, this.leftBtn, this.rightBtn, this.delBtn, h('span.vsep'), this.countEl,
        h('span.grow'),
        h('span.tl-all', h('span.label-sm', 'Every frame'), this.allInput, unitSeg, apply),
        h('span.vsep'), this.estEl),
    );
  },

  render(selectOnly = false) {
    const n = store.frames.length;
    const total = store.totalMs;
    this.countEl.textContent = n === 1 ? '1 frame' : `${n} frames, ${fmtSeconds(total / 1000)} loop`;
    this.delBtn.disabled = n < 2;
    this.leftBtn.disabled = store.current === 0;
    this.rightBtn.disabled = store.current === n - 1;
    const items = this.strip.children;
    if (!selectOnly) {
      while (items.length > n) items[items.length - 1].remove();
      for (let i = 0; i < n; i++) {
        let it = items[i];
        if (!it) {
          it = this._item();
          this.strip.append(it);
        }
        this._fill(it, i);
      }
    }
    for (let i = 0; i < items.length; i++) {
      const on = i === (this.playing ? this.playIndex : store.current);
      items[i].classList.toggle('selected', i === store.current);
      items[i].classList.toggle('playing', this.playing && on);
      items[i].setAttribute('aria-selected', String(i === store.current));
    }
    const sel = items[store.current];
    if (sel && !this.playing) this._reveal(sel);
  },

  _reveal(el) {
    const s = this.strip;
    const l = el.offsetLeft - s.offsetLeft;
    if (l < s.scrollLeft) s.scrollLeft = l - 8;
    else if (l + el.offsetWidth > s.scrollLeft + s.clientWidth) s.scrollLeft = l + el.offsetWidth - s.clientWidth + 8;
  },

  _item() {
    const it = h('div.frame', { role: 'option', draggable: 'true' });
    const thumbWrap = h('div.frame-thumb');
    const num = h('span.frame-num');
    const ms = h('input.frame-ms', { type: 'number', min: MIN_MS, max: 10000, step: 10, title: 'How long this frame shows, in milliseconds', 'aria-label': 'Frame duration in ms' });
    it.append(thumbWrap, h('div.frame-meta', num, h('label.frame-ms-wrap', ms, h('span', 'ms'))));
    it.addEventListener('click', (e) => {
      if (e.target === ms) return;
      this.stop();
      store.select(Number(it.dataset.index));
    });
    ms.addEventListener('change', () => {
      const i = Number(it.dataset.index);
      const v = clampMs(ms.value);
      ms.value = v;
      if (store.frames[i] && store.frames[i].ms !== v) {
        store.commit('Frame duration', (s) => (s.frames[i] = { pixels: s.frames[i].pixels, ms: v }));
      }
    });
    ms.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') ms.blur();
      e.stopPropagation();
    });
    return it;
  },

  _fill(it, i) {
    const f = store.frames[i];
    it.dataset.index = i;
    it.querySelector('.frame-num').textContent = i + 1;
    const ms = it.querySelector('.frame-ms');
    if (document.activeElement !== ms) ms.value = f.ms;
    if (it._px !== f.pixels) {
      it._px = f.pixels;
      let c = thumbCache.get(f.pixels);
      if (!c || c._w !== store.width) {
        c = document.createElement('canvas');
        drawDots(c, f.pixels, store.width, store.height, 3);
        c._w = store.width;
        thumbCache.set(f.pixels, c);
      }
      const wrap = it.querySelector('.frame-thumb');
      // A canvas can only live in one place; clone when the same frame object shows twice.
      const node = c.isConnected && c.parentElement !== wrap ? cloneCanvas(c) : c;
      if (wrap.firstChild !== node) wrap.replaceChildren(node);
    }
  },

  // ------------------------------------------------------------ operations

  add() {
    this.stop();
    const ms = store.frame.ms;
    store.commit('Add frame', (s) => {
      s.frames.splice(s.current + 1, 0, { pixels: s.blankPixels(), ms });
      s.current += 1;
    });
  },
  duplicate() {
    this.stop();
    store.commit('Duplicate frame', (s) => {
      const f = s.frames[s.current];
      s.frames.splice(s.current + 1, 0, { pixels: f.pixels.slice(), ms: f.ms });
      s.current += 1;
    });
  },
  remove() {
    if (store.frames.length < 2) return;
    this.stop();
    store.commit('Delete frame', (s) => {
      s.frames.splice(s.current, 1);
      s.current = Math.min(s.current, s.frames.length - 1);
    });
  },
  move(dir) {
    const from = store.current;
    const to = from + dir;
    if (to < 0 || to >= store.frames.length) return;
    this.moveTo(from, to);
  },
  moveTo(from, to) {
    if (from === to) return;
    this.stop();
    store.commit('Move frame', (s) => {
      const [f] = s.frames.splice(from, 1);
      s.frames.splice(to, 0, f);
      s.current = to;
    });
  },
  setAll() {
    let v = Number(this.allInput.value);
    if (!v || v <= 0) return;
    const ms = clampMs(this.unit === 'fps' ? 1000 / v : v);
    if (store.frames.every((f) => f.ms === ms)) return;
    store.commit('Set all durations', (s) => {
      s.frames = s.frames.map((f) => ({ pixels: f.pixels, ms }));
    });
  },
  prev() {
    this.stop();
    store.select((store.current - 1 + store.frames.length) % store.frames.length);
  },
  next() {
    this.stop();
    store.select((store.current + 1) % store.frames.length);
  },

  // ------------------------------------------------------------ playback

  toggle() {
    if (this.playing) this.stop();
    else this.play();
  },
  play() {
    if (store.frames.length < 2 || this.playing) return;
    this.playing = true;
    this.playIndex = store.current;
    this.playBtn.innerHTML = icon('stop') + '<span>Stop</span>';
    this.playBtn.classList.add('active');
    const step = () => {
      if (!this.playing) return;
      editor.playIndex = this.playIndex;
      editor.render();
      this.render(true);
      const ms = clampMs(store.frames[this.playIndex]?.ms ?? DEFAULT_MS);
      this.timer = setTimeout(() => {
        this.playIndex = (this.playIndex + 1) % store.frames.length;
        step();
      }, ms);
    };
    step();
  },
  stop() {
    if (!this.playing) return;
    this.playing = false;
    clearTimeout(this.timer);
    editor.playIndex = null;
    editor.render();
    this.playBtn.innerHTML = icon('play') + '<span>Play</span>';
    this.playBtn.classList.remove('active');
    this.render(true);
  },

  // ------------------------------------------------------------ drag to reorder

  _bindDnD() {
    const s = this.strip;
    let marker = null;
    const clearMarker = () => {
      marker?.classList.remove('drop-before', 'drop-after');
      marker = null;
    };
    s.addEventListener('dragstart', (e) => {
      const it = e.target.closest('.frame');
      if (!it) return;
      this.dragFrom = Number(it.dataset.index);
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/x-frame', String(this.dragFrom));
      it.classList.add('dragging');
    });
    s.addEventListener('dragend', (e) => {
      e.target.closest('.frame')?.classList.remove('dragging');
      this.dragFrom = null;
      clearMarker();
    });
    s.addEventListener('dragover', (e) => {
      if (this.dragFrom == null) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const it = e.target.closest('.frame');
      clearMarker();
      if (!it) return;
      const r = it.getBoundingClientRect();
      marker = it;
      it.classList.add(e.clientX < r.left + r.width / 2 ? 'drop-before' : 'drop-after');
    });
    s.addEventListener('drop', (e) => {
      if (this.dragFrom == null) return;
      e.preventDefault();
      e.stopPropagation();
      const it = e.target.closest('.frame');
      let to = store.frames.length - 1;
      if (it) {
        const r = it.getBoundingClientRect();
        to = Number(it.dataset.index) + (e.clientX < r.left + r.width / 2 ? 0 : 1);
      }
      const from = this.dragFrom;
      if (to > from) to -= 1;
      clearMarker();
      this.dragFrom = null;
      this.moveTo(from, Math.max(0, Math.min(store.frames.length - 1, to)));
    });
  },

  // ------------------------------------------------------------ estimate

  scheduleEstimate: debounce(function () {
    timeline._estimate();
  }, 900),

  async _estimate() {
    const seq = (this._seq = (this._seq || 0) + 1);
    const frames = store.frames.slice();
    if (frames.length === 1) {
      this.estEl.textContent = 'Still picture, about 1 s to send';
      this.estEl.classList.remove('warn');
      return;
    }
    this.estEl.textContent = 'Working out size...';
    try {
      const r = await api.estimate(frames.map((f) => toB64(f.pixels)), frames.map((f) => clampMs(f.ms)));
      if (seq !== this._seq) return;
      this.estEl.textContent = `About ${fmtBytes(r.bytes)}, ${fmtSeconds(r.seconds)} to send`;
      this.estEl.classList.toggle('warn', r.bytes > BIG_UPLOAD_BYTES);
      this.estEl.title = r.bytes > BIG_UPLOAD_BYTES ? 'A big upload. Fewer frames send faster.' : 'How much has to be uploaded to the sign';
    } catch {
      if (seq === this._seq) this.estEl.textContent = '';
    }
  },
};

function cloneCanvas(c) {
  const n = document.createElement('canvas');
  n.width = c.width;
  n.height = c.height;
  n.getContext('2d').drawImage(c, 0, 0);
  return n;
}

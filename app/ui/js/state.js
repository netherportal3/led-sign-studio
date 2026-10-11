// Project model and undo history.
//
// Frames are immutable once committed: {pixels: Uint8ClampedArray(W*H*3), ms}. An edit always
// builds a new frame object, so a history entry is just a shallow copy of the frames array.

import { fromB64, toB64, clamp } from './util.js';

export const MIN_MS = 30;
export const MAX_MS = 10000;
export const DEFAULT_MS = 100;
// Above this many bytes (about 25 s at the sign's upload speed) Send asks first.
export const BIG_UPLOAD_BYTES = 150000;
const MAX_HISTORY = 400;

export const clampMs = (ms) => clamp(Math.round(Number(ms) || DEFAULT_MS), MIN_MS, MAX_MS);

class Emitter {
  constructor() {
    this._handlers = {};
  }
  on(evt, fn) {
    (this._handlers[evt] ||= []).push(fn);
    return () => (this._handlers[evt] = this._handlers[evt].filter((f) => f !== fn));
  }
  emit(evt, data) {
    for (const fn of this._handlers[evt] || []) {
      try {
        fn(data);
      } catch (e) {
        console.error(e);
      }
    }
  }
}

class Store extends Emitter {
  constructor() {
    super();
    this.width = 64;
    this.height = 16;
    this.name = 'Untitled';
    this.frames = [this.blankFrame()];
    this.current = 0;
    this.undoStack = [];
    this.redoStack = [];
    this.revision = 0;
    this.lastCommit = null;
  }

  blankPixels(w = this.width, h = this.height) {
    return new Uint8ClampedArray(w * h * 3);
  }
  blankFrame(ms = DEFAULT_MS) {
    return { pixels: this.blankPixels(), ms };
  }
  get frame() {
    return this.frames[this.current];
  }
  get totalMs() {
    return this.frames.reduce((a, f) => a + clampMs(f.ms), 0);
  }

  snapshot() {
    return { name: this.name, width: this.width, height: this.height, frames: this.frames.slice(), current: this.current };
  }
  _restore(s) {
    this.name = s.name;
    this.width = s.width;
    this.height = s.height;
    this.frames = s.frames.slice();
    this.current = clamp(s.current, 0, this.frames.length - 1);
  }

  /**
   * Run an editing operation. `fn(store)` mutates the store fields (never pixel arrays already in
   * use). `coalesce` merges quick repeats of the same operation into one undo step.
   */
  commit(label, fn, { coalesce = false } = {}) {
    const before = this.snapshot();
    const now = performance.now();
    const merge =
      coalesce && this.lastCommit && this.lastCommit.label === label && now - this.lastCommit.time < 1500 &&
      this.redoStack.length === 0 && this.undoStack.length > 0;
    fn(this);
    if (!this.frames.length) this.frames = [this.blankFrame()];
    this.current = clamp(this.current, 0, this.frames.length - 1);
    if (!merge) {
      this.undoStack.push({ label, snap: before });
      if (this.undoStack.length > MAX_HISTORY) this.undoStack.shift();
    }
    this.redoStack.length = 0;
    this.lastCommit = { label, time: now };
    this._changed(label, true);
  }

  _changed(kind, edit) {
    this.revision++;
    this.emit('change', { kind, edit });
  }

  get canUndo() {
    return this.undoStack.length > 0;
  }
  get canRedo() {
    return this.redoStack.length > 0;
  }

  undo() {
    const e = this.undoStack.pop();
    if (!e) return null;
    this.redoStack.push({ label: e.label, snap: this.snapshot() });
    this._restore(e.snap);
    this.lastCommit = null;
    this._changed('undo', true);
    return e.label;
  }

  redo() {
    const e = this.redoStack.pop();
    if (!e) return null;
    this.undoStack.push({ label: e.label, snap: this.snapshot() });
    this._restore(e.snap);
    this.lastCommit = null;
    this._changed('redo', true);
    return e.label;
  }

  /** Select a frame. Not an edit, so it is not in the history. */
  select(i) {
    i = clamp(i, 0, this.frames.length - 1);
    if (i === this.current) return;
    this.current = i;
    this.emit('change', { kind: 'select', edit: false });
  }

  rename(name) {
    name = String(name || '').trim().slice(0, 80) || 'Untitled';
    if (name === this.name) return;
    // A rename is not an undo step: carry it into the history of this same project.
    const old = this.name;
    for (const e of [...this.undoStack, ...this.redoStack]) if (e.snap.name === old) e.snap.name = name;
    this.name = name;
    this.revision++;
    this.emit('change', { kind: 'rename', edit: false });
  }

  setPixels(index, pixels, label = 'Draw') {
    this.commit(label, (s) => {
      s.frames[index] = { pixels, ms: s.frames[index].ms };
      s.current = index;
    });
  }

  /** Replace or append generated frames: frames = [Uint8ClampedArray], durations = [ms]. */
  putFrames(frames, durations, mode, label, srcW = this.width, srcH = this.height) {
    const list = frames.map((p, i) => ({
      pixels: this.fit(p.pixels || p, this.width, this.height, srcW, srcH),
      ms: clampMs(durations[i] ?? durations[0]),
    }));
    this.commit(label, (s) => {
      if (mode === 'append') {
        // A single blank frame is just the empty canvas: replace it instead of keeping it.
        const onlyBlank = s.frames.length === 1 && isBlank(s.frames[0].pixels);
        const start = onlyBlank ? 0 : s.frames.length;
        s.frames = onlyBlank ? list : s.frames.concat(list);
        s.current = start;
      } else {
        s.frames = list;
        s.current = 0;
      }
    });
  }

  /** Make a pixel buffer match the canvas size (center crop or pad). */
  fit(pixels, w = this.width, h = this.height, srcW = this.width, srcH = this.height) {
    if (pixels.length === w * h * 3 && srcW === w && srcH === h) return pixels;
    return resizePixels(pixels, srcW, srcH, w, h);
  }

  resize(w, h, label = 'Change canvas size') {
    if (w === this.width && h === this.height) return;
    this.commit(label, (s) => {
      const ow = s.width;
      const oh = s.height;
      s.frames = s.frames.map((f) => ({ pixels: resizePixels(f.pixels, ow, oh, w, h), ms: f.ms }));
      s.width = w;
      s.height = h;
    });
  }

  toJSON() {
    return {
      version: 1,
      name: this.name,
      width: this.width,
      height: this.height,
      frames: this.frames.map((f) => ({ pixels: toB64(f.pixels), ms: clampMs(f.ms) })),
      current: this.current,
    };
  }

  /** Parse a project JSON into a plain object, or null when it is not usable. */
  static parse(json) {
    if (!json || typeof json !== 'object' || !Array.isArray(json.frames) || !json.frames.length) return null;
    const w = clamp(parseInt(json.width, 10) || 64, 1, 512);
    const h = clamp(parseInt(json.height, 10) || 16, 1, 256);
    const frames = [];
    for (const f of json.frames) {
      let px;
      try {
        px = fromB64(f.pixels);
      } catch {
        continue;
      }
      if (px.length !== w * h * 3) {
        const fixed = new Uint8ClampedArray(w * h * 3);
        fixed.set(px.subarray(0, fixed.length));
        px = fixed;
      }
      frames.push({ pixels: px, ms: clampMs(f.ms) });
    }
    if (!frames.length) return null;
    return {
      name: String(json.name || 'Untitled').slice(0, 80),
      width: w,
      height: h,
      frames,
      current: clamp(parseInt(json.current, 10) || 0, 0, frames.length - 1),
    };
  }

  /** Load a project. With `undoable`, it becomes one undo step. */
  load(json, { label = 'Open project', undoable = true } = {}) {
    const p = Store.parse(json);
    if (!p) return false;
    const apply = (s) => {
      s.name = p.name;
      s.width = p.width;
      s.height = p.height;
      s.frames = p.frames;
      s.current = p.current;
    };
    if (undoable) this.commit(label, apply);
    else {
      apply(this);
      this.undoStack = [];
      this.redoStack = [];
      this._changed('load', false);
    }
    return true;
  }
}

export function isBlank(px) {
  for (let i = 0; i < px.length; i++) if (px[i]) return false;
  return true;
}

export function resizePixels(src, ow, oh, w, h) {
  const out = new Uint8ClampedArray(w * h * 3);
  const dx = Math.floor((w - ow) / 2);
  const dy = Math.floor((h - oh) / 2);
  for (let y = 0; y < h; y++) {
    const sy = y - dy;
    if (sy < 0 || sy >= oh) continue;
    for (let x = 0; x < w; x++) {
      const sx = x - dx;
      if (sx < 0 || sx >= ow) continue;
      const si = (sy * ow + sx) * 3;
      const di = (y * w + x) * 3;
      out[di] = src[si];
      out[di + 1] = src[si + 1];
      out[di + 2] = src[si + 2];
    }
  }
  return out;
}

export const store = new Store();
export { Store };

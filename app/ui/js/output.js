// A live LED preview with Send / Put in editor buttons, shared by Text, Effects, Animate and Import.

import { store, clampMs, BIG_UPLOAD_BYTES } from './state.js';
import { LedView } from './render.js';
import { Player, visibleIn } from './anim.js';
import { api } from './api.js';
import { sendFrames, onSendState } from './send.js';
import { h, debounce, fmtBytes, fmtSeconds, toB64, clamp } from './util.js';
import { icon } from './icons.js';
import { toast } from './ui.js';

export class OutputBox {
  /**
   * opts.label: what is being made ("Text", "Fire"), used in undo labels and toasts.
   * opts.sendLabel: text of the send button.
   */
  constructor({ label = 'Content', sendLabel = 'Send to sign', estimate = true } = {}) {
    this.label = label;
    this.frames = [];
    this.durations = [];
    this.W = store.width;
    this.H = store.height;
    this.canvas = h('canvas.out-canvas');
    this.view = new LedView(this.canvas, { glow: true });
    this.info = h('span.out-info');
    this.est = h('span.out-est');
    this.msg = h('div.out-msg', { hidden: true });
    this.sendBtn = h('button.btn.primary', { type: 'button', onclick: () => this.send() }, h('span', { html: icon('send') }), h('span', sendLabel));
    this.replaceBtn = h('button.btn', { type: 'button', title: 'Replace all frames in the editor', onclick: () => this.put('replace') }, 'Replace');
    this.appendBtn = h('button.btn', { type: 'button', title: 'Add these frames after the last frame', onclick: () => this.put('append') }, 'Append');
    this.screen = h('div.out-screen', this.canvas);
    this.el = h('div.out',
      this.screen,
      h('div.out-meta', this.info, this.est),
      this.msg,
      h('div.out-actions',
        this.sendBtn,
        h('div.out-put', h('span.label-sm', 'Put in editor'), this.replaceBtn, this.appendBtn)),
    );
    this.player = new Player((i) => this._draw(i), { isVisible: visibleIn(this.canvas) });
    this.doEstimate = estimate;
    this._est = debounce(() => this._estimate(), 700);
    new ResizeObserver(() => this._layout()).observe(this.screen);
    onSendState((busy) => (this.sendBtn.disabled = busy || !this.frames.length));
  }

  _layout() {
    const w = this.screen.clientWidth;
    if (!w) return;
    const cell = clamp(Math.floor(w / this.W), 2, 12);
    this.view.resize(this.W, this.H, cell);
    this._draw(this.player.index);
  }

  _draw(i) {
    const f = this.frames[i];
    if (f) this.view.draw(f);
  }

  /** Show a message under the preview (null hides it). */
  note(text, action = null, tone = 'warn') {
    if (!text) {
      this.msg.hidden = true;
      return;
    }
    this.msg.hidden = false;
    this.msg.dataset.tone = tone;
    this.msg.replaceChildren(h('span', text));
    if (action) this.msg.append(h('button.btn.sm', { type: 'button', onclick: action.fn }, action.label));
  }

  set(frames, durations) {
    this.frames = frames;
    this.durations = durations.map(clampMs);
    if (this.W !== store.width || this.H !== store.height) {
      this.W = store.width;
      this.H = store.height;
      this._layout();
    }
    const n = frames.length;
    const total = this.durations.reduce((a, b) => a + b, 0);
    this.info.textContent = n === 1 ? 'Still picture' : `${n} frames, ${fmtSeconds(total / 1000)} loop`;
    this.player.set(n, this.durations);
    if (!this.player.running) this.player.start(0);
    else this._draw(this.player.index % Math.max(1, n));
    this.sendBtn.disabled = !n;
    this.replaceBtn.disabled = !n;
    this.appendBtn.disabled = !n;
    if (this.doEstimate) {
      this.est.textContent = '';
      this._est();
    }
  }

  async _estimate() {
    const frames = this.frames;
    if (!frames.length) return;
    if (frames.length === 1) {
      this.est.textContent = 'About 1 s to send';
      return;
    }
    const seq = (this._seq = (this._seq || 0) + 1);
    try {
      const r = await api.estimate(frames.map((f) => toB64(f)), this.durations, { width: this.W, height: this.H });
      if (seq !== this._seq) return;
      this.est.textContent = `About ${fmtBytes(r.bytes)}, ${fmtSeconds(r.seconds)} to send`;
      this.est.classList.toggle('warn', r.bytes > BIG_UPLOAD_BYTES);
    } catch {
      /* the estimate is a nice-to-have */
    }
  }

  send() {
    if (!this.frames.length) return;
    sendFrames(this.frames, this.durations, { what: this.label, size: { width: this.W, height: this.H } });
  }

  put(mode) {
    if (!this.frames.length) return;
    store.putFrames(this.frames, this.durations, mode, `${this.label}: ${mode === 'append' ? 'append' : 'replace'}`, this.W, this.H);
    const n = this.frames.length;
    toast(
      mode === 'append' ? `Added ${n === 1 ? '1 frame' : `${n} frames`} to the editor.` : `The editor now has ${n === 1 ? '1 frame' : `${n} frames`}.`,
      { type: 'ok', key: 'put', action: { label: 'Undo', fn: () => store.undo() } },
    );
  }
}

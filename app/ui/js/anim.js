// One requestAnimationFrame loop drives every looping preview on the page.

import { MIN_MS } from './state.js';

const players = new Set();
let raf = 0;

function tick(ts) {
  raf = 0;
  for (const p of players) p._tick(ts);
  if (players.size) raf = requestAnimationFrame(tick);
}

/**
 * A looping player. draw(frameIndex) is called whenever the shown frame changes.
 * `isVisible()` lets hidden previews skip work.
 */
export class Player {
  constructor(draw, { isVisible = () => true } = {}) {
    this.draw = draw;
    this.isVisible = isVisible;
    this.count = 0;
    this.durations = [];
    this.index = 0;
    this.last = 0;
    this.acc = 0;
    this.running = false;
  }
  set(count, durations) {
    this.count = count;
    this.durations = durations;
    if (this.index >= count) this.index = 0;
    this.acc = 0;
    this.last = 0;
  }
  start(fromIndex = null) {
    if (fromIndex != null) this.index = fromIndex;
    this.running = true;
    this.last = 0;
    this.acc = 0;
    players.add(this);
    if (!raf) raf = requestAnimationFrame(tick);
    this.draw(this.index);
  }
  stop() {
    this.running = false;
    players.delete(this);
  }
  _tick(ts) {
    if (!this.count) return;
    if (!this.last) this.last = ts;
    const dt = Math.min(1000, ts - this.last);
    this.last = ts;
    if (!this.isVisible()) return;
    if (this.count < 2) return;
    this.acc += dt;
    let changed = false;
    for (let guard = 0; guard < 200; guard++) {
      const d = Math.max(MIN_MS, this.durations[this.index] ?? this.durations[0] ?? 100);
      if (this.acc < d) break;
      this.acc -= d;
      this.index = (this.index + 1) % this.count;
      changed = true;
    }
    if (changed) this.draw(this.index);
  }
}

export const visibleIn = (el) => () => el.isConnected && el.offsetParent !== null && !document.hidden;

// Effect generators. Every generator returns a seamless loop: {frames: Uint8ClampedArray[], ms}.
// Loops close exactly because all motion uses whole cycles over the loop length N, or (for
// Game of Life) the end blends into the start.

import { hsv, rng, clamp, lerp, ramp, smooth, TAU } from './util.js';

const sp = (p) => (clamp(p.speed ?? 5, 1, 10) - 1) / 9; // 0 slow .. 1 fast
const byspeed = (p, slow, fast) => Math.round(lerp(slow, fast, sp(p)));

class Buf {
  constructor(W, H) {
    this.W = W;
    this.H = H;
    this.f = new Float32Array(W * H * 3);
  }
  fill(c) {
    for (let i = 0; i < this.f.length; i += 3) {
      this.f[i] = c[0];
      this.f[i + 1] = c[1];
      this.f[i + 2] = c[2];
    }
  }
  set(x, y, c) {
    if (x < 0 || y < 0 || x >= this.W || y >= this.H) return;
    const i = (y * this.W + x) * 3;
    this.f[i] = c[0];
    this.f[i + 1] = c[1];
    this.f[i + 2] = c[2];
  }
  add(x, y, c, a = 1) {
    x = Math.round(x);
    y = Math.round(y);
    if (x < 0 || y < 0 || x >= this.W || y >= this.H) return;
    const i = (y * this.W + x) * 3;
    this.f[i] += c[0] * a;
    this.f[i + 1] += c[1] * a;
    this.f[i + 2] += c[2] * a;
  }
  /** Sub-pixel splat over the 4 nearest LEDs. */
  splat(x, y, c, a = 1) {
    const x0 = Math.floor(x);
    const y0 = Math.floor(y);
    const fx = x - x0;
    const fy = y - y0;
    this._addi(x0, y0, c, a * (1 - fx) * (1 - fy));
    this._addi(x0 + 1, y0, c, a * fx * (1 - fy));
    this._addi(x0, y0 + 1, c, a * (1 - fx) * fy);
    this._addi(x0 + 1, y0 + 1, c, a * fx * fy);
  }
  _addi(x, y, c, a) {
    if (a <= 0 || x < 0 || y < 0 || x >= this.W || y >= this.H) return;
    const i = (y * this.W + x) * 3;
    this.f[i] += c[0] * a;
    this.f[i + 1] += c[1] * a;
    this.f[i + 2] += c[2] * a;
  }
  u8(scale = 1) {
    const out = new Uint8ClampedArray(this.f.length);
    for (let i = 0; i < out.length; i++) out[i] = this.f[i] * scale;
    return out;
  }
}

function each(W, H, fn) {
  const out = new Uint8ClampedArray(W * H * 3);
  let i = 0;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++, i += 3) {
      const c = fn(x, y);
      out[i] = c[0];
      out[i + 1] = c[1];
      out[i + 2] = c[2];
    }
  }
  return out;
}

const scale = (c, v) => [c[0] * v, c[1] * v, c[2] * v];
const hash3 = (a, b, c) => {
  let h = (a * 374761393 + b * 668265263 + c * 2147483647) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
};

function periodicNoise(r, gw, gh) {
  const g = new Float32Array(gw * gh);
  for (let i = 0; i < g.length; i++) g[i] = r();
  return (x, y) => {
    x = ((x % gw) + gw) % gw;
    y = ((y % gh) + gh) % gh;
    const x0 = Math.floor(x);
    const y0 = Math.floor(y);
    const x1 = (x0 + 1) % gw;
    const y1 = (y0 + 1) % gh;
    const fx = smooth(x - x0);
    const fy = smooth(y - y0);
    const a = g[y0 * gw + x0];
    const b = g[y0 * gw + x1];
    const c = g[y1 * gw + x0];
    const d = g[y1 * gw + x1];
    return lerp(lerp(a, b, fx), lerp(c, d, fx), fy);
  };
}

const PALETTES = {
  lava: [[0, [10, 0, 0]], [0.3, [150, 0, 0]], [0.55, [255, 60, 0]], [0.8, [255, 170, 0]], [1, [255, 240, 120]]],
  ocean: [[0, [0, 5, 40]], [0.35, [0, 40, 160]], [0.6, [0, 140, 255]], [0.85, [60, 230, 255]], [1, [200, 255, 255]]],
  neon: [[0, [40, 0, 90]], [0.3, [255, 0, 170]], [0.55, [120, 0, 255]], [0.8, [0, 200, 255]], [1, [180, 255, 255]]],
  forest: [[0, [0, 20, 0]], [0.4, [0, 120, 20]], [0.7, [120, 255, 0]], [1, [255, 255, 120]]],
};

const FIRE = {
  classic: [[0, [0, 0, 0]], [0.18, [30, 0, 0]], [0.4, [170, 8, 0]], [0.6, [255, 70, 0]], [0.8, [255, 160, 10]], [1, [255, 240, 150]]],
  blue: [[0, [0, 0, 0]], [0.2, [0, 0, 40]], [0.45, [0, 30, 220]], [0.7, [0, 140, 255]], [0.88, [80, 220, 255]], [1, [220, 255, 255]]],
  green: [[0, [0, 0, 0]], [0.2, [0, 25, 0]], [0.45, [0, 160, 0]], [0.7, [90, 255, 0]], [0.88, [190, 255, 60]], [1, [240, 255, 200]]],
  purple: [[0, [0, 0, 0]], [0.2, [25, 0, 40]], [0.45, [140, 0, 220]], [0.7, [230, 40, 255]], [0.88, [255, 140, 255]], [1, [255, 230, 255]]],
};

// Parameter helpers for the UI.
const range = (key, label, min, max, def, extra = {}) => ({ key, label, type: 'range', min, max, def, ...extra });
const select = (key, label, options, def, extra = {}) => ({ key, label, type: 'select', options, def, ...extra });
const color = (key, label, def, extra = {}) => ({ key, label, type: 'color', def, ...extra });
const SPEED = range('speed', 'Speed', 1, 10, 5);
const BRIGHT = range('bright', 'Brightness', 10, 100, 100, { unit: '%' });

export const EFFECTS = {
  rainbow: {
    name: 'Rainbow wave',
    params: [
      select('dir', 'Direction', [['left', 'Left'], ['right', 'Right'], ['up', 'Up'], ['down', 'Down'], ['diag', 'Diagonal']], 'left'),
      range('size', 'Stripe size', 1, 8, 6),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const span = [8, 12, 16, 24, 32, 48, 64, 128][p.size - 1] || 48;
      const N = clamp(Math.round(span / 1.4), 16, 64);
      const v = p.bright / 100;
      const frames = [];
      for (let t = 0; t < N; t++) {
        const ph = t / N;
        frames.push(each(W, H, (x, y) => {
          let u;
          switch (p.dir) {
            case 'right': u = x / span - ph; break;
            case 'up': u = (y * 3) / span + ph; break;
            case 'down': u = (y * 3) / span - ph; break;
            case 'diag': u = (x + y * 2) / span + ph; break;
            default: u = x / span + ph;
          }
          return hsv(u * 360, 1, v);
        }));
      }
      return { frames, ms: byspeed(p, 90, 30) };
    },
  },

  cycle: {
    name: 'Color cycle',
    params: [
      select('mode', 'Style', [['cycle', 'Cycle'], ['breathe', 'Breathe'], ['rbreathe', 'Rainbow breathe']], 'cycle'),
      color('color', 'Color', [255, 0, 90], { show: (p) => p.mode === 'breathe' }),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const b = p.bright / 100;
      const frames = [];
      const breath = (u) => 0.03 + 0.97 * Math.pow(0.5 - 0.5 * Math.cos(TAU * u), 1.8);
      if (p.mode === 'breathe') {
        const N = 40;
        for (let t = 0; t < N; t++) {
          const c = scale(p.color, breath(t / N) * b);
          frames.push(each(W, H, () => c));
        }
      } else if (p.mode === 'rbreathe') {
        const N = 64;
        for (let t = 0; t < N; t++) {
          const c = hsv((360 * t) / N, 1, breath((2 * t) / N) * b);
          frames.push(each(W, H, () => c));
        }
      } else {
        const N = 48;
        for (let t = 0; t < N; t++) {
          const c = hsv((360 * t) / N, 1, b);
          frames.push(each(W, H, () => c));
        }
      }
      return { frames, ms: byspeed(p, 110, 30) };
    },
  },

  plasma: {
    name: 'Plasma',
    params: [
      select('palette', 'Colors', [['rainbow', 'Rainbow'], ['lava', 'Lava'], ['ocean', 'Ocean'], ['neon', 'Neon'], ['forest', 'Forest']], 'rainbow'),
      range('size', 'Blob size', 1, 5, 3),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const N = 48;
      const f = [2, 1.5, 1, 0.7, 0.5][p.size - 1] || 1;
      const b = p.bright / 100;
      const frames = [];
      for (let t = 0; t < N; t++) {
        const ph = (TAU * t) / N;
        const cx = W / 2 + W * 0.35 * Math.sin(ph);
        const cy = H / 2 + H * 0.4 * Math.cos(ph);
        frames.push(each(W, H, (x, y) => {
          let v = Math.sin(x * 0.16 * f + ph);
          v += Math.sin(y * 0.33 * f - 2 * ph);
          v += Math.sin((x * 0.1 + y * 0.2) * f + ph);
          v += Math.sin(Math.hypot(x - cx, (y - cy) * 2) * 0.25 * f - ph);
          const u = (v + 4) / 8;
          if (p.palette === 'rainbow') return hsv(u * 400 + (360 * t) / N, 1, b);
          const w = 0.5 + 0.5 * Math.sin(u * TAU * 2 + ph);
          return scale(ramp(PALETTES[p.palette] || PALETTES.lava, w), b);
        }));
      }
      return { frames, ms: byspeed(p, 110, 35) };
    },
  },

  fire: {
    name: 'Fire',
    seeded: true,
    params: [
      select('palette', 'Colors', [['classic', 'Classic'], ['blue', 'Blue'], ['green', 'Green'], ['purple', 'Purple']], 'classic'),
      range('height', 'Flame height', 1, 5, 3),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 40;
      const gw1 = Math.max(4, Math.ceil(W / 3));
      const gh1 = 8;
      const gw2 = Math.max(4, Math.ceil(W / 1.5));
      const gh2 = 16;
      const n1 = periodicNoise(r, gw1, gh1);
      const n2 = periodicNoise(r, gw2, gh2);
      const exp = [2.4, 1.8, 1.3, 0.95, 0.7][p.height - 1] || 1.3;
      const pal = FIRE[p.palette] || FIRE.classic;
      const b = p.bright / 100;
      const frames = [];
      for (let t = 0; t < N; t++) {
        const s = t / N;
        frames.push(each(W, H, (x, y) => {
          const yy = (y / H) * 16; // keep flame shape the same on any panel height
          const a = n1((x * gw1) / W, yy / 3.2 + s * gh1);
          const c = n2((x * gw2) / W, yy / 1.6 + s * gh2 * 2);
          const turb = a * 0.6 + c * 0.4;
          const hgt = (y + 0.5) / H;
          const base = Math.pow(hgt, exp);
          let I = turb * 1.5 * base + base * 0.28 - 0.1;
          if (y === H - 1) I = Math.max(I, 0.62 + 0.3 * c);
          return scale(ramp(pal, clamp(I, 0, 1)), b);
        }));
      }
      return { frames, ms: byspeed(p, 90, 35) };
    },
  },

  sparkle: {
    name: 'Sparkle',
    seeded: true,
    params: [
      range('density', 'Amount', 1, 10, 5),
      select('colors', 'Colors', [['rainbow', 'Rainbow'], ['white', 'White'], ['one', 'One color']], 'rainbow'),
      color('color', 'Color', [255, 200, 60], { show: (p) => p.colors === 'one' }),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 48;
      const life = lerp(16, 6, sp(p));
      const count = Math.round((W * H * p.density) / 70);
      const sparks = [];
      for (let i = 0; i < count; i++) {
        const c = p.colors === 'white' ? [255, 255, 255] : p.colors === 'one' ? p.color : hsv(r() * 360, 0.85, 1);
        sparks.push({ x: Math.floor(r() * W), y: Math.floor(r() * H), t0: r() * N, life: life * (0.7 + r() * 0.6), c });
      }
      const frames = [];
      for (let t = 0; t < N; t++) {
        const buf = new Buf(W, H);
        for (const s of sparks) {
          const loc = (t - s.t0 + N) % N;
          if (loc >= s.life) continue;
          const e = Math.pow(Math.sin((Math.PI * loc) / s.life), 1.5);
          buf.add(s.x, s.y, s.c, e);
          if (e > 0.6) {
            const a = (e - 0.6) * 0.7;
            buf.add(s.x - 1, s.y, s.c, a);
            buf.add(s.x + 1, s.y, s.c, a);
            buf.add(s.x, s.y - 1, s.c, a);
            buf.add(s.x, s.y + 1, s.c, a);
          }
        }
        frames.push(buf.u8(p.bright / 100));
      }
      return { frames, ms: 40 };
    },
  },

  matrix: {
    name: 'Matrix rain',
    seeded: true,
    params: [
      range('density', 'Amount', 1, 10, 6),
      select('colors', 'Colors', [['green', 'Green'], ['cyan', 'Cyan'], ['rainbow', 'Rainbow'], ['one', 'One color']], 'green'),
      color('color', 'Color', [255, 60, 200], { show: (p) => p.colors === 'one' }),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 64;
      const base = lerp(1, 3, sp(p));
      const drops = [];
      for (let x = 0; x < W; x++) {
        let nd = r() < p.density / 12 + 0.12 ? 1 : 0;
        if (p.density > 6 && r() < (p.density - 6) / 8) nd++;
        for (let k = 0; k < nd; k++) {
          const Lt = 4 + Math.floor(r() * 8);
          const Lp = H + Lt + Math.floor(r() * H);
          const cyc = Math.max(1, Math.round(base * (0.6 + r() * 0.8)));
          const c =
            p.colors === 'cyan' ? [0, 210, 255] : p.colors === 'rainbow' ? hsv((x * 360) / W, 1, 1) : p.colors === 'one' ? p.color : [0, 255, 60];
          drops.push({ x, Lt, Lp, v: (Lp * cyc) / N, ph: r() * Lp, c });
        }
      }
      const frames = [];
      for (let t = 0; t < N; t++) {
        const buf = new Buf(W, H);
        for (const d of drops) {
          const yh = Math.floor((d.ph + t * d.v) % d.Lp);
          for (let j = 0; j <= d.Lt; j++) {
            const y = yh - j;
            if (y < 0 || y >= H) continue;
            const flick = 0.6 + 0.4 * hash3(d.x, y, Math.floor(t / 4));
            if (j === 0) buf.add(d.x, y, [d.c[0] * 0.5 + 128, d.c[1] * 0.5 + 128, d.c[2] * 0.5 + 128], 1);
            else buf.add(d.x, y, d.c, Math.pow(1 - j / (d.Lt + 1), 1.6) * flick);
          }
        }
        frames.push(buf.u8(p.bright / 100));
      }
      return { frames, ms: byspeed(p, 80, 40) };
    },
  },

  starfield: {
    name: 'Starfield',
    seeded: true,
    params: [
      select('style', 'Style', [['side', 'Side scroll'], ['warp', 'Warp']], 'side'),
      range('density', 'Amount', 1, 10, 5),
      select('colors', 'Colors', [['white', 'White'], ['color', 'Colorful']], 'white'),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const tint = () => (p.colors === 'color' ? hsv(r() * 360, 0.45, 1) : [255, 255, 255]);
      const frames = [];
      if (p.style === 'warp') {
        const N = 48;
        const stars = [];
        const n = p.density * 7;
        for (let i = 0; i < n; i++) {
          const k = r() < 0.4 ? 2 : 1;
          stars.push({ a: r() * TAU, t0: r() * N, life: N / k, c: tint() });
        }
        const cx = (W - 1) / 2;
        const cy = (H - 1) / 2;
        for (let t = 0; t < N; t++) {
          const buf = new Buf(W, H);
          for (const s of stars) {
            const loc = (((t - s.t0) % s.life) + s.life) % s.life / s.life;
            for (const [lag, a] of [[0, 1], [0.035, 0.35]]) {
              const l = loc - lag;
              if (l <= 0) continue;
              const d = Math.pow(l, 2.2);
              const x = cx + Math.cos(s.a) * d * W * 0.75;
              const y = cy + Math.sin(s.a) * d * H * 1.1;
              buf.splat(x, y, s.c, Math.min(1, l * 2) * a);
            }
          }
          frames.push(buf.u8(p.bright / 100));
        }
        return { frames, ms: byspeed(p, 80, 30) };
      }
      const N = 64;
      const layers = [
        { k: 1, a: 0.4, per: 45 },
        { k: 2, a: 0.7, per: 90 },
        { k: 4, a: 1, per: 180 },
      ];
      const stars = [];
      for (const L of layers) {
        const n = Math.max(1, Math.round(((W * H) / L.per) * (p.density / 5)));
        for (let i = 0; i < n; i++) stars.push({ x: r() * W, y: Math.floor(r() * H), L, c: tint() });
      }
      for (let t = 0; t < N; t++) {
        const buf = new Buf(W, H);
        for (const s of stars) {
          const x = (((s.x - (t * W * s.L.k) / N) % W) + W) % W;
          const x0 = Math.floor(x);
          const fx = x - x0;
          buf.add(x0, s.y, s.c, s.L.a * (1 - fx));
          buf.add((x0 + 1) % W, s.y, s.c, s.L.a * fx);
          if (s.L.k === 4) buf.add((x0 + 2) % W, s.y, s.c, 0.25);
        }
        frames.push(buf.u8(p.bright / 100));
      }
      return { frames, ms: byspeed(p, 100, 30) };
    },
  },

  snow: {
    name: 'Snow',
    seeded: true,
    params: [
      range('density', 'Amount', 1, 10, 5),
      select('wind', 'Wind', [['-1', 'Left'], ['0', 'None'], ['1', 'Right']], '0'),
      color('color', 'Color', [255, 255, 255]),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 60;
      const wind = Number(p.wind) || 0;
      const base = lerp(1, 2.2, sp(p));
      const n = Math.round(((W * H) / 1024) * p.density * 6);
      const Lp = H + 2;
      const flakes = [];
      for (let i = 0; i < n; i++) {
        const z = r();
        flakes.push({
          x0: r() * W,
          y0: r() * Lp,
          z,
          k: Math.max(1, Math.round(base * (0.6 + z * 0.8))),
          ks: 1 + Math.floor(r() * 3),
          ph: r() * TAU,
        });
      }
      const frames = [];
      for (let t = 0; t < N; t++) {
        const buf = new Buf(W, H);
        for (const f of flakes) {
          const y = ((f.y0 + (t * Lp * f.k) / N) % Lp) - 1;
          let x = f.x0 + (wind * t * W) / N + Math.sin((TAU * f.ks * t) / N + f.ph) * 0.8;
          x = ((x % W) + W) % W;
          const yi = Math.floor(y);
          const fy = y - yi;
          const a = 0.5 + 0.5 * f.z;
          const xi = Math.round(x) % W;
          buf.add(xi, yi, p.color, a * (1 - fy));
          buf.add(xi, yi + 1, p.color, a * fy);
        }
        frames.push(buf.u8(p.bright / 100));
      }
      return { frames, ms: byspeed(p, 110, 45) };
    },
  },

  scanner: {
    name: 'Scanner',
    params: [
      color('color', 'Color', [255, 0, 0]),
      range('width', 'Width', 1, 8, 3),
      range('trail', 'Trail', 0, 12, 5),
      select('style', 'Style', [['full', 'Full height'], ['band', 'Middle band']], 'full'),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const w = Math.min(p.width, W - 1);
      const travel = W - w;
      const N = Math.max(4, Math.round(travel));
      const pos = (t) => {
        const u = (((t % N) + N) % N) / N;
        const tri = u < 0.5 ? u * 2 : 2 - u * 2;
        return travel * smooth(tri);
      };
      const rows = [];
      for (let y = 0; y < H; y++) {
        if (p.style === 'band') {
          const d = Math.abs(y + 0.5 - H / 2);
          rows.push(d <= 2 ? 1 : d <= 3 ? 0.25 : 0);
        } else rows.push(1);
      }
      const frames = [];
      for (let t = 0; t < N; t++) {
        const col = new Float32Array(W);
        for (let j = 0; j <= p.trail; j++) {
          const p0 = pos(t - j);
          const a = Math.pow(0.6, j);
          for (let x = 0; x < W; x++) {
            const cover = Math.max(0, Math.min(x + 1, p0 + w) - Math.max(x, p0));
            col[x] = Math.max(col[x], cover * a);
          }
        }
        const b = p.bright / 100;
        frames.push(each(W, H, (x, y) => {
          const v2 = col[x] * rows[y] * b;
          const hot = col[x] > 0.9 ? 0.18 * rows[y] * b : 0;
          return [p.color[0] * v2 + 255 * hot, p.color[1] * v2 + 255 * hot, p.color[2] * v2 + 255 * hot];
        }));
      }
      return { frames, ms: byspeed(p, 75, 30) };
    },
  },

  equalizer: {
    name: 'Equalizer',
    seeded: true,
    params: [
      select('bars', 'Bars', [['8', '8'], ['16', '16'], ['21', '21'], ['32', '32']], '16'),
      select('colors', 'Colors', [['classic', 'Classic'], ['rainbow', 'Rainbow'], ['one', 'One color']], 'classic'),
      color('color', 'Color', [0, 180, 255], { show: (p) => p.colors === 'one' }),
      select('peaks', 'Peak dots', [['on', 'On'], ['off', 'Off']], 'on'),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 64;
      const bars = Math.min(Number(p.bars) || 16, W);
      const bw = Math.max(1, Math.floor(W / bars));
      const gap = bw >= 3 ? 1 : 0;
      const off = Math.floor((W - bars * bw) / 2);
      const kmax = 5;
      const comps = [];
      for (let b = 0; b < bars; b++) {
        const list = [];
        for (let i = 0; i < 3; i++) list.push({ k: 1 + Math.floor(r() * kmax), ph: r() * TAU, a: 0.4 + r() * 0.6 });
        comps.push(list);
      }
      const beat = { k: Math.max(1, Math.round(kmax * 1.5)), ph: r() * TAU };
      const level = (b, t) => {
        let s = 0;
        let sa = 0;
        for (const c of comps[b]) {
          s += c.a * Math.sin((TAU * c.k * t) / N + c.ph);
          sa += c.a;
        }
        const bt = Math.max(0, Math.sin((TAU * beat.k * t) / N + beat.ph)) * 0.35;
        const boost = 1 - (b / bars) * 0.35;
        return clamp((0.1 + 0.75 * (0.5 + (0.5 * s) / sa) + bt) * boost, 0.05, 1);
      };
      const colorAt = (b, y) => {
        if (p.colors === 'rainbow') return hsv((b / bars) * 300, 1, 1);
        if (p.colors === 'one') return p.color;
        const u = 1 - (y + 0.5) / H;
        return u > 0.8 ? [255, 0, 0] : u > 0.55 ? [255, 200, 0] : [0, 255, 40];
      };
      const frames = [];
      for (let t = 0; t < N; t++) {
        const buf = new Buf(W, H);
        for (let b = 0; b < bars; b++) {
          const hgt = level(b, t) * H;
          const full = Math.floor(hgt);
          const frac = hgt - full;
          let peak = 0;
          if (p.peaks === 'on') {
            for (let j = 0; j < 16; j++) peak = Math.max(peak, level(b, t - j) * H - 0.03 * j * j);
          }
          for (let x = off + b * bw; x < off + (b + 1) * bw - gap; x++) {
            for (let k = 0; k < full; k++) buf.set(x, H - 1 - k, colorAt(b, H - 1 - k));
            if (frac > 0.05 && full < H) buf.add(x, H - 1 - full, colorAt(b, H - 1 - full), frac);
            if (p.peaks === 'on') {
              const py = H - 1 - Math.min(H - 1, Math.floor(peak));
              if (py < H - 1 - full) buf.set(x, py, [255, 255, 255]);
            }
          }
        }
        frames.push(buf.u8(p.bright / 100));
      }
      return { frames, ms: byspeed(p, 90, 35) };
    },
  },

  life: {
    name: 'Game of Life',
    seeded: true,
    params: [
      range('density', 'Start amount', 1, 10, 5),
      select('colors', 'Colors', [['age', 'By age'], ['rainbow', 'Rainbow'], ['one', 'One color']], 'age'),
      color('color', 'Color', [0, 255, 120], { show: (p) => p.colors === 'one' }),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 64;
      const F = 10;
      let age = new Int16Array(W * H).fill(-1);
      const fill = 0.12 + p.density * 0.03;
      for (let i = 0; i < age.length; i++) if (r() < fill) age[i] = 0;
      const colorOf = (a, x) => {
        if (p.colors === 'one') return a === 0 ? p.color.map((c) => Math.min(255, c * 0.6 + 100)) : p.color;
        if (p.colors === 'rainbow') return hsv((x * 360) / W, a === 0 ? 0.4 : 1, 1);
        if (a === 0) return [190, 255, 255];
        return hsv(180 + Math.min(a, 24) * 6, 1, 1);
      };
      const render = () => {
        const f = new Float32Array(W * H * 3);
        for (let y = 0; y < H; y++) {
          for (let x = 0; x < W; x++) {
            const a = age[y * W + x];
            if (a < 0) continue;
            const c = colorOf(a, x);
            const i = (y * W + x) * 3;
            f[i] = c[0];
            f[i + 1] = c[1];
            f[i + 2] = c[2];
          }
        }
        return f;
      };
      const gens = [];
      const seen = [];
      for (let g = 0; g < N + F; g++) {
        gens.push(render());
        const next = new Int16Array(W * H).fill(-1);
        let alive = 0;
        for (let y = 0; y < H; y++) {
          for (let x = 0; x < W; x++) {
            let n = 0;
            for (let dy = -1; dy <= 1; dy++) {
              for (let dx = -1; dx <= 1; dx++) {
                if (!dx && !dy) continue;
                if (age[((y + dy + H) % H) * W + ((x + dx + W) % W)] >= 0) n++;
              }
            }
            const i = y * W + x;
            const was = age[i] >= 0;
            if ((was && (n === 2 || n === 3)) || (!was && n === 3)) {
              next[i] = was ? Math.min(age[i] + 1, 1000) : 0;
              alive++;
            }
          }
        }
        // Stuck or nearly empty: drop in a fresh patch so it stays lively.
        const key = Array.from(next, (a) => (a >= 0 ? 1 : 0)).join('');
        if (alive < W * H * 0.06 || seen.slice(-3).includes(key)) {
          const px = Math.floor(r() * W);
          const py = Math.floor(r() * H);
          for (let dy = 0; dy < 7; dy++) {
            for (let dx = 0; dx < 10; dx++) {
              if (r() < 0.45) next[((py + dy) % H) * W + ((px + dx) % W)] = 0;
            }
          }
        }
        seen.push(key);
        age = next;
      }
      const b = p.bright / 100;
      const frames = [];
      for (let i = 0; i < N; i++) {
        const out = new Uint8ClampedArray(W * H * 3);
        const g = gens[i];
        if (i < F) {
          const w = (i + 0.5) / F;
          const e = gens[N + i];
          for (let k = 0; k < out.length; k++) out[k] = (e[k] * (1 - w) + g[k] * w) * b;
        } else {
          for (let k = 0; k < out.length; k++) out[k] = g[k] * b;
        }
        frames.push(out);
      }
      return { frames, ms: byspeed(p, 220, 60) };
    },
  },

  fireworks: {
    name: 'Fireworks',
    seeded: true,
    params: [
      range('count', 'How many', 1, 8, 4),
      select('colors', 'Colors', [['rainbow', 'Mixed'], ['gold', 'Gold'], ['one', 'One color']], 'rainbow'),
      color('color', 'Color', [255, 40, 160], { show: (p) => p.colors === 'one' }),
      SPEED,
      BRIGHT,
    ],
    gen(W, H, p) {
      const r = rng(p.seed);
      const N = 96;
      const fws = [];
      const drag = 0.89;
      for (let i = 0; i < p.count; i++) {
        const baseHue = r() * 360;
        const Tr = Math.round(7 + r() * 4);
        const Te = Math.round(26 + r() * 8);
        const parts = [];
        const np = 14 + Math.floor(r() * 10);
        for (let j = 0; j < np; j++) {
          let c;
          if (p.colors === 'gold') c = hsv(30 + r() * 20, 0.7 + r() * 0.3, 1);
          else if (p.colors === 'one') c = p.color;
          else c = hsv(baseHue + (r() - 0.5) * 60, 0.85, 1);
          parts.push({ a: (TAU * j) / np + (r() - 0.5) * 0.35, v: 0.5 + r() * 0.55, c });
        }
        fws.push({
          t0: ((i + r() * 0.5) * N) / p.count,
          x: 8 + r() * (W - 16),
          ye: 3 + r() * H * 0.3,
          Tr,
          Te: Math.min(Te, N - Tr - 2),
          parts,
        });
      }
      const pos = (f, q, e) => {
        const dist = (q.v * (1 - Math.pow(drag, e))) / (1 - drag);
        return [f.x + Math.cos(q.a) * dist * 1.7, f.ye + Math.sin(q.a) * dist * 0.8 + 0.006 * e * e];
      };
      const frames = [];
      for (let t = 0; t < N; t++) {
        const buf = new Buf(W, H);
        for (const f of fws) {
          const loc = (((t - f.t0) % N) + N) % N;
          if (loc < f.Tr) {
            const u = loc / f.Tr;
            const y = H - 1 - (H - 1 - f.ye) * (1 - (1 - u) * (1 - u));
            buf.add(f.x, y, [255, 210, 140], 1);
            buf.add(f.x, y + 1, [255, 120, 30], 0.45);
            buf.add(f.x, y + 2, [255, 70, 0], 0.18);
          } else if (loc < f.Tr + f.Te) {
            const e = loc - f.Tr;
            const u = e / f.Te;
            if (e < 2) {
              const fl = e ? 0.45 : 1;
              buf.add(f.x, f.ye, [255, 255, 255], fl);
              for (const [dx, dy] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) buf.add(f.x + dx, f.ye + dy, [255, 255, 255], fl * 0.6);
            }
            for (let j = 0; j < f.parts.length; j++) {
              const q = f.parts[j];
              let a = e < 4 ? 1 : Math.pow(1 - u, 1.1);
              if (u > 0.5 && hash3(j, t, 7) < 0.4) a *= 0.2;
              const [x, y] = pos(f, q, e);
              buf.add(x, y, q.c, a);
              // A short fading trail behind each spark.
              if (e >= 1) {
                const [x1, y1] = pos(f, q, e - 1);
                buf.add(x1, y1, q.c, a * 0.3);
              }
            }
          }
        }
        frames.push(buf.u8(p.bright / 100));
      }
      return { frames, ms: byspeed(p, 70, 30) };
    },
  },
};

export function defaults(id) {
  const p = { seed: 1234 };
  for (const d of EFFECTS[id].params) p[d.key] = Array.isArray(d.def) ? d.def.slice() : d.def;
  return p;
}

export function generate(id, W, H, params) {
  const out = EFFECTS[id].gen(W, H, params);
  return { frames: out.frames, durations: out.frames.map(() => out.ms) };
}

// ---------------------------------------------------------------- animate a drawing

export const DRAWING_ANIMS = {
  left: { name: 'Scroll left' },
  right: { name: 'Scroll right' },
  up: { name: 'Scroll up' },
  down: { name: 'Scroll down' },
  rainbow: { name: 'Rainbow' },
  blink: { name: 'Blink' },
  pulse: { name: 'Pulse' },
  sparkle: { name: 'Sparkle' },
};

function shifted(src, W, H, dx, dy) {
  const out = new Uint8ClampedArray(src.length);
  for (let y = 0; y < H; y++) {
    const sy = (((y - dy) % H) + H) % H;
    for (let x = 0; x < W; x++) {
      const sx = (((x - dx) % W) + W) % W;
      const s = (sy * W + sx) * 3;
      const d = (y * W + x) * 3;
      out[d] = src[s];
      out[d + 1] = src[s + 1];
      out[d + 2] = src[s + 2];
    }
  }
  return out;
}

/** speed 1..10. Returns {frames, durations}. */
export function animateDrawing(kind, src, W, H, speed = 5) {
  const t01 = (clamp(speed, 1, 10) - 1) / 9;
  const frames = [];
  let ms = 40;
  if (kind === 'left' || kind === 'right' || kind === 'up' || kind === 'down') {
    const horiz = kind === 'left' || kind === 'right';
    const n = horiz ? W : H;
    const dir = kind === 'left' || kind === 'up' ? -1 : 1;
    for (let i = 0; i < n; i++) frames.push(shifted(src, W, H, horiz ? dir * i : 0, horiz ? 0 : dir * i));
    ms = Math.round(lerp(150, 30, t01));
  } else if (kind === 'rainbow') {
    const N = 36;
    for (let t = 0; t < N; t++) {
      const out = new Uint8ClampedArray(src.length);
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          const i = (y * W + x) * 3;
          const v = Math.max(src[i], src[i + 1], src[i + 2]) / 255;
          if (!v) continue;
          const c = hsv((x / W) * 360 + y * 6 - (360 * t) / N, 1, v);
          out[i] = c[0];
          out[i + 1] = c[1];
          out[i + 2] = c[2];
        }
      }
      frames.push(out);
    }
    ms = Math.round(lerp(110, 30, t01));
  } else if (kind === 'blink') {
    frames.push(src.slice(), new Uint8ClampedArray(src.length));
    ms = Math.round(lerp(1000, 150, t01));
  } else if (kind === 'pulse') {
    const N = 30;
    for (let t = 0; t < N; t++) {
      const v = 0.06 + 0.94 * Math.pow(0.5 - 0.5 * Math.cos((TAU * t) / N), 1.4);
      const out = new Uint8ClampedArray(src.length);
      for (let i = 0; i < src.length; i++) out[i] = src[i] * v;
      frames.push(out);
    }
    ms = Math.round(lerp(110, 30, t01));
  } else if (kind === 'sparkle') {
    const r = rng(77);
    const N = 48;
    const life = lerp(14, 6, t01);
    const sparks = [];
    const n = Math.round((W * H) / 40);
    for (let i = 0; i < n; i++) sparks.push({ x: Math.floor(r() * W), y: Math.floor(r() * H), t0: r() * N, life: life * (0.7 + r() * 0.6) });
    for (let t = 0; t < N; t++) {
      const buf = new Buf(W, H);
      for (let i = 0; i < src.length; i++) buf.f[i] = src[i];
      for (const s of sparks) {
        const loc = (t - s.t0 + N) % N;
        if (loc >= s.life) continue;
        const e = Math.sin((Math.PI * loc) / s.life);
        buf.add(s.x, s.y, [255, 255, 255], e);
      }
      frames.push(buf.u8());
    }
    ms = 40;
  }
  return { frames, durations: frames.map(() => ms) };
}

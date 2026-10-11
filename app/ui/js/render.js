// LED-look rendering: round dots, dark unlit dots, soft glow.

const UNLIT = [24, 26, 33];
const GAP = [7, 8, 11];

/** Big, glowing renderer for the editor and previews. */
export class LedView {
  constructor(canvas, { glow = true, dot = 0.78 } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.glow = glow;
    this.dot = dot;
    this.src = document.createElement('canvas');
    this.srcCtx = this.src.getContext('2d');
    this.layer = document.createElement('canvas');
    this.layerCtx = this.layer.getContext('2d');
    this.glowC = document.createElement('canvas');
    this.glowCtx = this.glowC.getContext('2d');
    this.W = 0;
    this.H = 0;
    this.cell = 0;
  }

  /** cssCell = CSS pixels per LED. */
  resize(W, H, cssCell) {
    const dpr = window.devicePixelRatio || 1;
    const cell = Math.max(1, cssCell * dpr);
    if (W === this.W && H === this.H && cell === this.cell) return;
    this.W = W;
    this.H = H;
    this.cell = cell;
    this.cssCell = cssCell;
    const w = Math.round(W * cell);
    const hh = Math.round(H * cell);
    this.canvas.width = w;
    this.canvas.height = hh;
    this.canvas.style.width = `${W * cssCell}px`;
    this.canvas.style.height = `${H * cssCell}px`;
    this.src.width = W;
    this.src.height = H;
    this.img = this.srcCtx.createImageData(W, H);
    this.layer.width = w;
    this.layer.height = hh;
    const gs = 4;
    this.glowC.width = W * gs;
    this.glowC.height = H * gs;
    this.glowBlur = gs * 0.55;

    // Dot mask (white circles) and the unlit panel background.
    this.mask = document.createElement('canvas');
    this.mask.width = w;
    this.mask.height = hh;
    this.unlit = document.createElement('canvas');
    this.unlit.width = w;
    this.unlit.height = hh;
    const m = this.mask.getContext('2d');
    const u = this.unlit.getContext('2d');
    u.fillStyle = `rgb(${GAP})`;
    u.fillRect(0, 0, w, hh);
    const r = (cell * this.dot) / 2;
    m.fillStyle = '#fff';
    const grad = (cx, cy) => {
      const g = u.createRadialGradient(cx - r * 0.25, cy - r * 0.3, r * 0.1, cx, cy, r);
      g.addColorStop(0, `rgb(${UNLIT.map((c) => c + 10)})`);
      g.addColorStop(1, `rgb(${UNLIT})`);
      return g;
    };
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const cx = (x + 0.5) * cell;
        const cy = (y + 0.5) * cell;
        m.beginPath();
        m.arc(cx, cy, r, 0, Math.PI * 2);
        m.fill();
        u.beginPath();
        u.fillStyle = cell >= 6 ? grad(cx, cy) : `rgb(${UNLIT})`;
        u.arc(cx, cy, r, 0, Math.PI * 2);
        u.fill();
      }
    }
  }

  /** rgb = Uint8ClampedArray(W*H*3). `under` (optional) shows faintly where rgb is dark. */
  draw(rgb, under = null, underAlpha = 0.22) {
    const { W, H, ctx } = this;
    if (!W) return;
    const d = this.img.data;
    for (let i = 0, j = 0, n = W * H; i < n; i++, j += 3) {
      let r = rgb[j], g = rgb[j + 1], b = rgb[j + 2];
      if (under && !(r | g | b)) {
        r = under[j] * underAlpha;
        g = under[j + 1] * underAlpha;
        b = under[j + 2] * underAlpha;
      }
      const k = i * 4;
      d[k] = r;
      d[k + 1] = g;
      d[k + 2] = b;
      d[k + 3] = 255;
    }
    this.srcCtx.putImageData(this.img, 0, 0);
    const w = this.canvas.width;
    const hh = this.canvas.height;
    const l = this.layerCtx;
    l.globalCompositeOperation = 'copy';
    l.imageSmoothingEnabled = false;
    l.drawImage(this.src, 0, 0, w, hh);
    l.globalCompositeOperation = 'destination-in';
    l.drawImage(this.mask, 0, 0);
    l.globalCompositeOperation = 'source-over';

    ctx.globalCompositeOperation = 'copy';
    ctx.drawImage(this.unlit, 0, 0);
    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(this.layer, 0, 0);
    if (this.glow) {
      const g = this.glowCtx;
      g.globalCompositeOperation = 'copy';
      g.imageSmoothingEnabled = true;
      g.filter = `blur(${this.glowBlur}px)`;
      g.drawImage(this.src, 0, 0, this.glowC.width, this.glowC.height);
      g.filter = 'none';
      ctx.imageSmoothingEnabled = true;
      ctx.globalAlpha = 0.6;
      ctx.drawImage(this.glowC, 0, 0, w, hh);
      ctx.globalAlpha = 1;
    }
    ctx.globalCompositeOperation = 'source-over';
  }
}

// ---------------------------------------------------------------- cheap dot images

const maskCache = new Map();
function dotMask(s) {
  let m = maskCache.get(s);
  if (m) return m;
  m = new Float32Array(s * s);
  const r = s * 0.42;
  const c = (s - 1) / 2;
  for (let y = 0; y < s; y++) {
    for (let x = 0; x < s; x++) {
      const d = Math.hypot(x - c, y - c);
      m[y * s + x] = Math.max(0, Math.min(1, r - d + 0.5));
    }
  }
  maskCache.set(s, m);
  return m;
}

/** Draw rgb as a dot image into `canvas` at s device pixels per LED (no glow, fast). */
export function drawDots(canvas, rgb, W, H, s = 3) {
  const w = W * s;
  const hh = H * s;
  if (canvas.width !== w || canvas.height !== hh) {
    canvas.width = w;
    canvas.height = hh;
  }
  const ctx = canvas.getContext('2d');
  const img = ctx.createImageData(w, hh);
  const d = img.data;
  const m = dotMask(s);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const j = (y * W + x) * 3;
      const lit = rgb[j] | rgb[j + 1] | rgb[j + 2];
      const r = lit ? Math.max(rgb[j], UNLIT[0]) : UNLIT[0];
      const g = lit ? Math.max(rgb[j + 1], UNLIT[1]) : UNLIT[1];
      const b = lit ? Math.max(rgb[j + 2], UNLIT[2]) : UNLIT[2];
      for (let py = 0; py < s; py++) {
        let k = ((y * s + py) * w + x * s) * 4;
        for (let px = 0; px < s; px++, k += 4) {
          const a = m[py * s + px];
          d[k] = GAP[0] + (r - GAP[0]) * a;
          d[k + 1] = GAP[1] + (g - GAP[1]) * a;
          d[k + 2] = GAP[2] + (b - GAP[2]) * a;
          d[k + 3] = 255;
        }
      }
    }
  }
  ctx.putImageData(img, 0, 0);
}

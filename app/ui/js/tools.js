// Pixel operations on RGB buffers (Uint8ClampedArray, W*H*3).

export const BLACK = [0, 0, 0];

export const TOOL_INFO = {
  pencil: { label: 'Pencil', key: 'P', icon: 'pencil', hint: 'Right click erases. Alt click picks a color.' },
  eraser: { label: 'Eraser', key: 'E', icon: 'eraser', hint: 'Turns LEDs off.' },
  fill: { label: 'Fill', key: 'F', icon: 'fill', hint: 'Fills the area of one color. Right click fills with black.' },
  line: { label: 'Line', key: 'L', icon: 'line', hint: 'Drag to draw. Hold Shift for straight angles.' },
  rect: { label: 'Rectangle', key: 'R', icon: 'rect', hint: 'Drag to draw. Hold Shift for a square.' },
  ellipse: { label: 'Ellipse', key: 'O', icon: 'ellipse', hint: 'Drag to draw. Hold Shift for a circle.' },
  picker: { label: 'Eyedropper', key: 'I', icon: 'picker', hint: 'Click an LED to use its color.' },
  rainbow: { label: 'Rainbow brush', key: 'B', icon: 'rainbow', hint: 'The color shifts as you draw.' },
};

export function getPx(buf, W, x, y) {
  const i = (y * W + x) * 3;
  return [buf[i], buf[i + 1], buf[i + 2]];
}

export function plot(buf, W, H, x, y, c) {
  if (x < 0 || y < 0 || x >= W || y >= H) return false;
  const i = (y * W + x) * 3;
  if (buf[i] === c[0] && buf[i + 1] === c[1] && buf[i + 2] === c[2]) return false;
  buf[i] = c[0];
  buf[i + 1] = c[1];
  buf[i + 2] = c[2];
  return true;
}

export function lineCells(x0, y0, x1, y1) {
  const out = [];
  const dx = Math.abs(x1 - x0);
  const dy = -Math.abs(y1 - y0);
  const sx = x0 < x1 ? 1 : -1;
  const sy = y0 < y1 ? 1 : -1;
  let err = dx + dy;
  for (let guard = 0; guard < 4096; guard++) {
    out.push([x0, y0]);
    if (x0 === x1 && y0 === y1) break;
    const e2 = 2 * err;
    if (e2 >= dy) {
      err += dy;
      x0 += sx;
    }
    if (e2 <= dx) {
      err += dx;
      y0 += sy;
    }
  }
  return out;
}

/** Snap the end point to 0, 45 or 90 degrees. */
export function constrainLine(x0, y0, x1, y1) {
  const dx = x1 - x0;
  const dy = y1 - y0;
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  if (ax > ay * 2) return [x1, y0];
  if (ay > ax * 2) return [x0, y1];
  const d = Math.max(ax, ay);
  return [x0 + Math.sign(dx) * d, y0 + Math.sign(dy) * d];
}

export function constrainSquare(x0, y0, x1, y1) {
  const d = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0));
  return [x0 + (x1 >= x0 ? d : -d), y0 + (y1 >= y0 ? d : -d)];
}

export function rectCells(x0, y0, x1, y1, filled) {
  const out = [];
  const [ax, bx] = [Math.min(x0, x1), Math.max(x0, x1)];
  const [ay, by] = [Math.min(y0, y1), Math.max(y0, y1)];
  for (let y = ay; y <= by; y++) {
    for (let x = ax; x <= bx; x++) {
      if (filled || y === ay || y === by || x === ax || x === bx) out.push([x, y]);
    }
  }
  return out;
}

export function ellipseCells(x0, y0, x1, y1, filled) {
  const [ax, bx] = [Math.min(x0, x1), Math.max(x0, x1)];
  const [ay, by] = [Math.min(y0, y1), Math.max(y0, y1)];
  const cx = (ax + bx) / 2;
  const cy = (ay + by) / 2;
  const rx = (bx - ax) / 2 + 0.5;
  const ry = (by - ay) / 2 + 0.5;
  const inside = (x, y) => ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0001;
  const out = [];
  for (let y = ay; y <= by; y++) {
    for (let x = ax; x <= bx; x++) {
      if (!inside(x, y)) continue;
      if (filled || !inside(x - 1, y) || !inside(x + 1, y) || !inside(x, y - 1) || !inside(x, y + 1)) out.push([x, y]);
    }
  }
  return out;
}

export function floodFill(buf, W, H, x, y, c) {
  if (x < 0 || y < 0 || x >= W || y >= H) return false;
  const target = getPx(buf, W, x, y);
  if (target[0] === c[0] && target[1] === c[1] && target[2] === c[2]) return false;
  const match = (i) => buf[i] === target[0] && buf[i + 1] === target[1] && buf[i + 2] === target[2];
  const stack = [y * W + x];
  while (stack.length) {
    const p = stack.pop();
    const i = p * 3;
    if (!match(i)) continue;
    buf[i] = c[0];
    buf[i + 1] = c[1];
    buf[i + 2] = c[2];
    const px = p % W;
    const py = (p / W) | 0;
    if (px > 0) stack.push(p - 1);
    if (px < W - 1) stack.push(p + 1);
    if (py > 0) stack.push(p - W);
    if (py < H - 1) stack.push(p + W);
  }
  return true;
}

/** Shift with wrap-around. Returns a new buffer. */
export function shift(buf, W, H, dx, dy) {
  const out = new Uint8ClampedArray(buf.length);
  for (let y = 0; y < H; y++) {
    const ny = (((y + dy) % H) + H) % H;
    for (let x = 0; x < W; x++) {
      const nx = (((x + dx) % W) + W) % W;
      const s = (y * W + x) * 3;
      const d = (ny * W + nx) * 3;
      out[d] = buf[s];
      out[d + 1] = buf[s + 1];
      out[d + 2] = buf[s + 2];
    }
  }
  return out;
}

export function flipH(buf, W, H) {
  const out = new Uint8ClampedArray(buf.length);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const s = (y * W + x) * 3;
      const d = (y * W + (W - 1 - x)) * 3;
      out[d] = buf[s];
      out[d + 1] = buf[s + 1];
      out[d + 2] = buf[s + 2];
    }
  }
  return out;
}

export function flipV(buf, W, H) {
  const out = new Uint8ClampedArray(buf.length);
  for (let y = 0; y < H; y++) out.set(buf.subarray(y * W * 3, (y + 1) * W * 3), (H - 1 - y) * W * 3);
  return out;
}

// Ambient tab: weather, a clock and world news on the sign during the day, plus the header pill.
// The schedule runs in the app's background service; this tab shows its state and edits its settings.

import { api } from './api.js';
import { conn } from './connection.js';
import { onSent } from './send.js';
import { LedView } from './render.js';
import { Player, visibleIn } from './anim.js';
import { h, $, debounce, clamp, fromB64, fmtBytes, fmtSeconds } from './util.js';
import { icon } from './icons.js';
import { toast, segmented, slider } from './ui.js';

const SCREENS = [
  ['clock', 'Clock', 'Time with the high and low'],
  ['now', 'Now', 'Temperature and conditions'],
  ['today', 'Today', 'The next 24 hours'],
  ['week', 'Week', '7-day forecast'],
  ['news', 'News', 'World headlines'],
];
const SCREEN_NAMES = { clock: 'Clock', now: 'Now', today: 'Today', week: 'Week', news: 'News', night: 'Night' };
const PAUSES = [[30, '30 min'], [60, '1 h'], [120, '2 h']];
const MANUAL_PAUSES = [['15', '15 min'], ['30', '30 min'], ['60', '1 h'], ['120', '2 h']];
const NEWS_SPEEDS = [['slow', 'Slow'], ['normal', 'Normal'], ['fast', 'Fast']];
const UNITS = [['F', '°F'], ['C', '°C']];
const CLOCKS = [['12', '12-hour'], ['24', '24-hour']];
const POLL_SHOWN = 5000;
const POLL_HIDDEN = 30000;
const MAX_PREVIEW_FRAMES = 4000;
// Settings that change what the sign shows, so the preview is made again after they are saved.
const VISUAL = ['location', 'screens', 'news_sources', 'day_color_scale', 'units', 'clock_24h', 'news_speed'];

// ---------------------------------------------------------------- formatting

function toMs(v) {
  if (v == null || v === '') return NaN;
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
  return Date.parse(v);
}

const clockText = (t) => new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

/** "9:42 AM", "tomorrow 8:00 AM", or "Oct 12, 9:42 AM". */
function when(v) {
  const t = toMs(v);
  if (!isFinite(t)) return '';
  const d = new Date(t);
  const now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(now)) / 86400000);
  const time = clockText(t);
  if (diff === 0) return time;
  if (diff === 1) return `tomorrow ${time}`;
  if (diff === -1) return `yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

/** "08:00" -> "8:00 AM" */
function hourText(hhmm) {
  const m = /^(\d{1,2}):(\d{2})/.exec(hhmm || '');
  if (!m) return hhmm || '';
  const d = new Date();
  d.setHours(Number(m[1]), Number(m[2]), 0, 0);
  return clockText(d);
}

function minutesText(min) {
  if (min >= 60 && min % 60 === 0) return `${min / 60} h`;
  if (min > 60) return `${Math.floor(min / 60)} h ${min % 60} min`;
  return `${min} min`;
}

const titleCase = (s) => String(s || '').toLowerCase().replace(/(^|[\s/-])([a-z])/g, (_, a, b) => a + b.toUpperCase());

/** The service's error text, with programmer wording turned into plain words. */
function plainError(msg) {
  const s = String(msg || '').trim();
  if (!s) return '';
  const technical = /Error|Exception|errno|Traceback|\[Win|_[a-z]/.test(s);
  if (!technical) return s;
  const rules = [
    [/bluetooth/i, 'Bluetooth is off or not available on this PC.'],
    [/not.?connected|not.?found|no device/i, 'Could not reach the sign. It keeps trying.'],
    [/storage.?full|out of space/i, 'The sign is out of space.'],
    [/no.?confirm/i, "The sign didn't confirm the last update."],
    [/urlopen|getaddrinfo|name resolution|nodename|network|unreachable|connection (refused|reset|aborted)|ssl/i,
      'No internet connection. The sign shows the last saved weather and news.'],
    [/timed? ?out|timeout/i, 'Something took too long to answer. It will try again.'],
  ];
  for (const [re, text] of rules) if (re.test(s)) return text;
  return s.replace(/^[\w.]*(Error|Exception)\s*:\s*/, '') || 'Something went wrong. It will try again.';
}

function placeName(r) {
  const second = r.admin1 && r.admin1 !== r.name ? r.admin1 : r.country;
  return second ? `${r.name}, ${second}` : r.name;
}

function merge(into, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (v && typeof v === 'object' && !Array.isArray(v) && into[k] && typeof into[k] === 'object') merge(into[k], v);
    else into[k] = v && typeof v === 'object' ? structuredClone(v) : v;
  }
  return into;
}

// ---------------------------------------------------------------- small widgets

function toggle(label, onChange, { title = null } = {}) {
  const input = h('input', { type: 'checkbox' });
  input.addEventListener('change', () => onChange(input.checked));
  const el = h('label.switch', { title }, input, h('span.track', h('span.thumb')), h('span.switch-label', label));
  return { el, input };
}

function savedMark() {
  return h('span.amb-saved', { 'aria-live': 'polite' }, h('span', { html: icon('check') }), 'Saved');
}

function head(title, ...extra) {
  return h('div.section-head', h('h3.section-title', title), ...extra);
}

// ---------------------------------------------------------------- GIF frames

/** RGBA to the RGB layout every renderer here uses. */
function toRgb(rgba) {
  const n = rgba.length / 4;
  const out = new Uint8ClampedArray(n * 3);
  for (let i = 0, j = 0, k = 0; i < n; i++, j += 3, k += 4) {
    out[j] = rgba[k];
    out[j + 1] = rgba[k + 1];
    out[j + 2] = rgba[k + 2];
  }
  return out;
}

/** Every frame with its duration, using the browser's ImageDecoder. Null when it is missing or fails. */
async function decodeWithBrowser(bytes) {
  if (typeof ImageDecoder !== 'function') return null;
  let dec = null;
  try {
    if (ImageDecoder.isTypeSupported && !(await ImageDecoder.isTypeSupported('image/gif'))) return null;
    dec = new ImageDecoder({ data: bytes, type: 'image/gif' });
    await dec.tracks.ready;
    await dec.completed;
    const n = Math.min(dec.tracks.selectedTrack.frameCount, MAX_PREVIEW_FRAMES);
    let ctx = null;
    let W = 0;
    let H = 0;
    const frames = [];
    const durations = [];
    for (let i = 0; i < n; i++) {
      const { image } = await dec.decode({ frameIndex: i });
      if (!ctx) {
        W = image.displayWidth;
        H = image.displayHeight;
        const c = document.createElement('canvas');
        c.width = W;
        c.height = H;
        ctx = c.getContext('2d', { willReadFrequently: true });
      }
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(image, 0, 0, W, H);
      durations.push(image.duration ? Math.round(image.duration / 1000) : 100);
      image.close();
      frames.push(toRgb(ctx.getImageData(0, 0, W, H).data));
    }
    return frames.length ? { frames, durations, W, H } : null;
  } catch (e) {
    console.warn('ImageDecoder could not read the preview', e);
    return null;
  } finally {
    try {
      dec?.close();
    } catch {}
  }
}

/** GIF LZW data to color indexes. */
function lzw(minCode, data, count) {
  const out = new Uint8Array(count);
  const clear = 1 << minCode;
  const eoi = clear + 1;
  const prefix = new Int16Array(4096);
  const suffix = new Uint8Array(4096);
  const stack = new Uint8Array(4097);
  for (let i = 0; i < clear; i++) {
    prefix[i] = -1;
    suffix[i] = i;
  }
  let size = minCode + 1;
  let mask = (1 << size) - 1;
  let next = eoi + 1;
  let prev = -1;
  let acc = 0;
  let bits = 0;
  let dp = 0;
  let o = 0;
  while (o < count) {
    while (bits < size) {
      if (dp >= data.length) return out;
      acc |= data[dp++] << bits;
      bits += 8;
    }
    const code = acc & mask;
    acc >>>= size;
    bits -= size;
    if (code === clear) {
      size = minCode + 1;
      mask = (1 << size) - 1;
      next = eoi + 1;
      prev = -1;
      continue;
    }
    if (code === eoi) break;
    if (prev === -1) {
      if (code >= clear) break;
      out[o++] = suffix[code];
      prev = code;
      continue;
    }
    let sp = 0;
    let c = code;
    if (code >= next) {
      // The code being defined right now: the previous string plus its own first character.
      c = prev;
      let f = prev;
      while (prefix[f] !== -1) f = prefix[f];
      stack[sp++] = suffix[f];
    }
    while (c !== -1 && sp < stack.length) {
      stack[sp++] = suffix[c];
      c = prefix[c];
    }
    const first = stack[sp - 1];
    while (sp && o < count) out[o++] = stack[--sp];
    if (next < 4096) {
      prefix[next] = prev;
      suffix[next] = first;
      next++;
      if (next > mask && size < 12) {
        size++;
        mask = (1 << size) - 1;
      }
    }
    prev = code;
  }
  return out;
}

/** A small GIF decoder for when the browser has no ImageDecoder. Frames are composed over black. */
function decodeInJs(u) {
  if (u.length < 13 || u[0] !== 0x47 || u[1] !== 0x49 || u[2] !== 0x46) return null;
  let p = 6;
  const u16 = () => {
    const v = u[p] | (u[p + 1] << 8);
    p += 2;
    return v;
  };
  const W = u16();
  const H = u16();
  const flags = u[p];
  p += 3;
  let globalTable = null;
  if (flags & 0x80) {
    const n = 2 << (flags & 7);
    globalTable = u.subarray(p, p + n * 3);
    p += n * 3;
  }
  const screen = new Uint8ClampedArray(W * H * 3);
  const frames = [];
  const durations = [];
  let delay = 100;
  let transparent = -1;
  let disposal = 0;
  const skipBlocks = () => {
    while (p < u.length && u[p]) p += u[p] + 1;
    p++;
  };
  while (p < u.length && frames.length < MAX_PREVIEW_FRAMES) {
    const block = u[p++];
    if (block === 0x3b) break;
    if (block === 0x21) {
      const label = u[p++];
      if (label === 0xf9 && u[p] >= 4) {
        const pf = u[p + 1];
        disposal = (pf >> 2) & 7;
        delay = (u[p + 2] | (u[p + 3] << 8)) * 10;
        transparent = pf & 1 ? u[p + 4] : -1;
      }
      skipBlocks();
      continue;
    }
    if (block !== 0x2c) break;
    const fx = u16();
    const fy = u16();
    const fw = u16();
    const fh = u16();
    const f = u[p++];
    let table = globalTable;
    if (f & 0x80) {
      const n = 2 << (f & 7);
      table = u.subarray(p, p + n * 3);
      p += n * 3;
    }
    const minCode = u[p++];
    let len = 0;
    for (let q = p; q < u.length && u[q]; q += u[q] + 1) len += u[q];
    const data = new Uint8Array(len);
    let o = 0;
    while (p < u.length && u[p]) {
      const n = u[p++];
      data.set(u.subarray(p, p + n), o);
      o += n;
      p += n;
    }
    p++;
    const idx = lzw(minCode, data, fw * fh);
    // Interlaced images store their rows in four passes.
    const rows = [];
    if (f & 0x40) for (const [y0, step] of [[0, 8], [4, 8], [2, 4], [1, 2]]) for (let y = y0; y < fh; y += step) rows.push(y);
    const saved = disposal === 3 ? screen.slice() : null;
    for (let i = 0; i < fh; i++) {
      const y = fy + (rows.length ? rows[i] : i);
      if (y >= H) continue;
      for (let x = 0; x < fw; x++) {
        const c = idx[i * fw + x];
        const X = fx + x;
        if (c === transparent || X >= W || !table || c * 3 + 2 >= table.length) continue;
        const k = (y * W + X) * 3;
        screen[k] = table[c * 3];
        screen[k + 1] = table[c * 3 + 1];
        screen[k + 2] = table[c * 3 + 2];
      }
    }
    frames.push(screen.slice());
    durations.push(delay || 100);
    if (disposal === 2) {
      for (let y = fy; y < Math.min(H, fy + fh); y++) screen.fill(0, (y * W + fx) * 3, (y * W + Math.min(W, fx + fw)) * 3);
    } else if (saved) {
      screen.set(saved);
    }
    delay = 100;
    transparent = -1;
    disposal = 0;
  }
  return frames.length ? { frames, durations, W, H } : null;
}

/** The preview GIF as RGB frames: the browser's decoder first, then the built-in one. */
async function decodeGif(bytes) {
  const viaBrowser = await decodeWithBrowser(bytes);
  if (viaBrowser) return viaBrowser;
  try {
    return decodeInJs(bytes);
  } catch (e) {
    console.warn('Could not read the preview GIF', e);
    return null;
  }
}

// ---------------------------------------------------------------- preview player

class AmbientPreview {
  constructor() {
    this.W = 64;
    this.H = 16;
    this.frames = [];
    this.durations = [];
    this.starts = [];
    this.segments = [];
    this.total = 0;
    this.loaded = false;
    this.canvas = h('canvas.out-canvas');
    this.view = new LedView(this.canvas, { glow: true });
    this.msg = h('div.amb-preview-msg', { hidden: true });
    this.screen = h('div.out-screen.amb-screen', this.canvas, this.msg);
    this.bar = h('i');
    this.info = h('span');
    this.pos = h('span.mono.amb-pos');
    this.el = h('div.out', this.screen, h('div.amb-progress', this.bar), h('div.out-meta', this.info, this.pos));
    this.player = new Player((i) => this._draw(i), { isVisible: visibleIn(this.canvas) });
    new ResizeObserver(() => this._layout()).observe(this.screen);
  }

  _layout() {
    // The screen has 8 px of padding on each side.
    const w = this.screen.clientWidth - 16;
    if (w <= 0) return;
    this.view.resize(this.W, this.H, clamp(Math.floor(w / this.W), 2, 12));
    this._draw(this.player.index);
  }

  _draw(i) {
    const f = this.frames[i] || new Uint8ClampedArray(this.W * this.H * 3);
    this.view.draw(f);
    if (!this.total) return;
    const at = this.starts[i] || 0;
    this.bar.style.width = `${(at / this.total) * 100}%`;
    const mmss = (ms) => {
      const s = Math.floor(ms / 1000);
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    };
    let name = '';
    for (let t = 0, k = 0; k < this.segments.length; k++) {
      t += Number(this.segments[k].ms) || 0;
      if (at < t) {
        name = SCREEN_NAMES[this.segments[k].kind] || '';
        break;
      }
    }
    this.pos.textContent = `${name ? `${name}  ` : ''}${mmss(at)} / ${mmss(this.total)}`;
  }

  message(text) {
    this.msg.hidden = !text;
    this.msg.textContent = text || '';
  }

  async load(r) {
    const decoded = await decodeGif(fromB64(r.gif));
    if (!decoded) throw new Error('Could not show the preview.');
    this.frames = decoded.frames;
    this.durations = decoded.durations.map((d) => Math.max(30, d));
    this._setSize(decoded.W, decoded.H);
    let acc = 0;
    this.starts = this.durations.map((d) => ((acc += d), acc - d));
    this.total = acc;
    this.segments = Array.isArray(r.segments) ? r.segments : [];
    this.player.set(this.frames.length, this.durations);
    this.player.start(0);
    const parts = [];
    if (r.seconds) parts.push(fmtSeconds(r.seconds));
    parts.push(`${this.frames.length} frames`);
    if (r.bytes) parts.push(fmtBytes(r.bytes));
    this.info.textContent = parts.join(', ');
    this.loaded = true;
  }

  _setSize(W, H) {
    if (W === this.W && H === this.H) return;
    this.W = W;
    this.H = H;
    this._layout();
  }
}

// ---------------------------------------------------------------- the tab

export const ambientPanel = {
  status: null,
  settings: null,
  auto: null,
  // null until the app first answers, false when this version of the app has no ambient mode.
  available: null,
  _failures: 0,
  _pending: null,
  _where: new Set(),
  _pseq: 0,

  /** openTab() shows the Ambient tab (used by the header pill). */
  init(openTab) {
    this.openTab = openTab;
    this.root = $('#tab-ambient');
    this.pill = $('#ambientPill');
    this.pillText = $('#ambientPill .amb-pill-text');
    this.pill.addEventListener('click', () => this.openTab());
    this._saveSoon = debounce(() => this._flush(), 600);
    this._previewSoon = debounce(() => this.isShown() && this.refreshPreview(), 1200);
    this._build();
    onSent((info) => this._afterManualSend(info));
    conn.onStatus(() => this._renderNote());
    // The window may start hidden (minimized or behind others): catch up once it is seen.
    document.addEventListener('visibilitychange', () => {
      if (this.isShown()) this.onShow();
    });
    this.poll();
    this.loadSettings();
    if (this.isShown()) this.onShow();
  },

  isShown() {
    return !!this.root && !this.root.hidden && !document.hidden;
  },

  /** The tab bar calls this whenever a tab is shown. */
  onTab(id) {
    if (this.root && id === 'ambient') this.onShow();
  },

  onShow() {
    this.poll();
    if (!this.settings) this.loadSettings();
    this.loadAutostart();
    if (!this.preview.loaded && !this._previewBusy) this.refreshPreview();
  },

  // ------------------------------------------------------------ building

  _build() {
    const root = this.root;

    // Status
    const en = toggle('Ambient mode', (on) => this.setEnabled(on), { title: 'Weather, a clock and world news on the sign during the day' });
    this.enabledIn = en.input;
    this.modeEl = h('span.amb-mode', { 'data-mode': 'unknown' }, h('span.dot'), h('span.amb-mode-text'));
    this.lines = h('div.amb-lines');
    this.errorEl = h('div.out-msg.amb-error', { 'data-tone': 'error', hidden: true });
    this.noteEl = h('p.amb-note', { hidden: true });
    this.topSaved = savedMark();

    // Preview
    this.preview = new AmbientPreview();
    this.previewBtn = h('button.btn.sm.ghost', { type: 'button', title: 'Make the preview again with the latest weather and news', onclick: () => this.refreshPreview() },
      h('span', { html: icon('refresh') }), 'Refresh');

    // Weather and news
    this.weatherEl = h('p.amb-weather');
    this.headsEl = h('ol.amb-heads');
    this.newsInfo = h('p.muted.small');

    // Pause, resume, run now
    this.pauseBtns = PAUSES.map(([m, label]) =>
      h('button.btn.sm', { type: 'button', title: `Pause ambient mode for ${label}. Your own content stays on the sign.`, onclick: () => this.pause(m) }, label));
    this.resumeBtn = h('button.btn.sm.primary', { type: 'button', onclick: () => this.resume() }, h('span', { html: icon('play') }), 'Resume');
    this.runBtn = h('button.btn.sm', { type: 'button', title: 'Update the sign now', onclick: () => this.runNow() }, h('span', { html: icon('send') }), h('span', 'Run now'));

    // Location
    this.locIn = h('input.input', {
      type: 'text', placeholder: 'Search for a city', autocomplete: 'off', spellcheck: false,
      role: 'combobox', 'aria-label': 'Location', 'aria-autocomplete': 'list', 'aria-expanded': 'false',
    });
    this.locList = h('div.amb-results', { role: 'listbox', hidden: true });
    this._initLocation();
    this.unitsSeg = segmented(UNITS, 'F', (v) => this.queue({ units: v }, 'loc'), { small: true });
    this.clockFmtSeg = segmented(CLOCKS, '12', (v) => this.queue({ clock_24h: v === '24' }, 'loc'), { small: true });

    // Hours
    this.startIn = h('input.input', { type: 'time', 'aria-label': 'Start time', required: true });
    this.endIn = h('input.input', { type: 'time', 'aria-label': 'End time', required: true });
    this.startIn.addEventListener('change', () => this._setHours());
    this.endIn.addEventListener('change', () => this._setHours());
    this.refreshSl = slider({ label: 'Update the sign every', min: 3, max: 15, value: 5, fmt: (v) => `${v} min`, onInput: (v) => this.queue({ refresh_minutes: v }, 'hours') });
    this.manualSeg = segmented(MANUAL_PAUSES, '30', (v) => this.queue({ pause_minutes_on_manual: Number(v) }, 'hours'), { small: true });

    // Screens
    this.screenToggles = {};
    const screenRows = SCREENS.map(([id, label, hint]) => {
      const t = toggle(label, (on) => this._setScreen(id, on));
      this.screenToggles[id] = t.input;
      return h('div.amb-row', t.el, h('span.amb-hint', hint));
    });
    this.clockSl = slider({ label: 'Clock time each minute', min: 10, max: 50, step: 5, value: 25, fmt: (v) => `${v} s`,
      onInput: (v) => this.queue({ screens: { clock: { seconds: v } } }, 'screens') });
    this.clockSl.classList.add('amb-sub');
    this.newsSpeedSeg = segmented(NEWS_SPEEDS, 'normal', (v) => this.queue({ news_speed: v }, 'screens'), { small: true });
    this.newsSpeedEl = h('div.field.amb-sub', h('span.field-label', 'Headline speed'), this.newsSpeedSeg);

    // News sources
    this.feedsEl = h('div.amb-rows');

    // Brightness and night
    this.brightSl = slider({ label: 'Day brightness', min: 1, max: 10, value: 3, onInput: (v) => this.queue({ day_brightness: v }, 'look') });
    this.colorSl = slider({ label: 'Day color strength', min: 10, max: 100, step: 5, value: 70, fmt: (v) => `${v}%`,
      title: 'Lower makes the colors softer without changing the brightness',
      onInput: (v) => this.queue({ day_color_scale: Math.round(v) / 100 }, 'look') });
    this.nightIn = h('input.input', { type: 'text', maxlength: 40, placeholder: 'Empty turns the sign off', spellcheck: false, 'aria-label': 'Night text' });
    this.nightIn.addEventListener('input', () => {
      this._syncNight();
      this.queue({ night_text: this.nightIn.value.trim() }, 'look');
    });
    this.nightHint = h('p.muted.small');
    this.nightSl = slider({ label: 'Night brightness', min: 1, max: 10, value: 1, onInput: (v) => this.queue({ night_brightness: v }, 'look') });

    // Start with Windows
    const au = toggle('Start with Windows', (on) => this.setAutostart(on), { title: 'Run ambient mode in the background from the moment you sign in' });
    this.autoIn = au.input;
    this.autoEl = au.el;
    this.autoNote = h('p.muted.small');

    this.saved = { top: this.topSaved, loc: savedMark(), hours: savedMark(), screens: savedMark(), feeds: savedMark(), look: savedMark() };

    this.unavailable = h('div.out-msg', { 'data-tone': 'info', hidden: true },
      h('span', 'Ambient mode is not available in this version of the app.'));

    this.body = h('div.amb-body',
      h('div.section',
        h('div.amb-top', en.el, this.topSaved),
        h('div.amb-status', this.modeEl, this.lines),
        this.errorEl,
        this.noteEl),
      h('div.section',
        head('Preview', this.previewBtn),
        this.preview.el),
      h('div.section',
        h('h3.section-title', 'Weather and news'),
        this.weatherEl,
        this.headsEl,
        this.newsInfo),
      h('div.section',
        h('div.amb-actions',
          h('div.amb-pause', h('span.label-sm', 'Pause for'), ...this.pauseBtns),
          h('div.amb-pause', this.resumeBtn, this.runBtn))),
      h('div.section',
        head('Location', this.saved.loc),
        h('div.amb-loc', h('span.amb-loc-ic', { html: icon('search') }), this.locIn),
        this.locList,
        h('div.grid2',
          h('div.field', h('span.field-label', 'Temperature'), this.unitsSeg),
          h('div.field', h('span.field-label', 'Clock'), this.clockFmtSeg))),
      h('div.section',
        head('Active hours', this.saved.hours),
        h('div.grid2',
          h('label.field', h('span.field-label', 'Start'), this.startIn),
          h('label.field', h('span.field-label', 'End'), this.endIn)),
        this.refreshSl,
        h('div.field', h('span.field-label', 'After I send something myself, pause for'), this.manualSeg)),
      h('div.section',
        head('Screens', this.saved.screens),
        h('div.amb-rows', screenRows[0], this.clockSl, ...screenRows.slice(1), this.newsSpeedEl)),
      h('div.section',
        head('News sources', this.saved.feeds),
        this.feedsEl),
      h('div.section',
        head('Brightness and night', this.saved.look),
        this.brightSl,
        this.colorSl,
        h('label.field', h('span.field-label', 'Night text'), this.nightIn),
        this.nightHint,
        this.nightSl),
      h('div.section',
        h('div.amb-row', this.autoEl),
        this.autoNote),
    );
    root.append(this.unavailable, this.body);
    this.syncControls();
    this.render();
  },

  // ------------------------------------------------------------ status

  async poll() {
    clearTimeout(this._timer);
    // One request at a time: a poll asked for mid-flight runs right after the current one.
    if (this._polling) {
      this._again = true;
      return;
    }
    this._polling = true;
    try {
      this.status = await api.ambientStatus();
      this.available = true;
      this._failures = 0;
    } catch (e) {
      if (e.status === 404) this.available = false;
      else if (++this._failures >= 3) this.status = null;
    }
    this._polling = false;
    this.render();
    const again = this._again;
    this._again = false;
    this._timer = setTimeout(() => this.poll(), again ? 0 : this.isShown() ? POLL_SHOWN : POLL_HIDDEN);
  },

  _modeText(s) {
    if (!s) return '';
    switch (s.mode) {
      case 'day':
        return 'Running';
      case 'night':
        return (this.settings?.night_text || '').trim() ? 'Night' : 'Night (sign off)';
      case 'paused': {
        const t = when(s.paused_until);
        return t ? `Paused until ${t}` : 'Paused';
      }
      case 'off':
        if (s.enabled && s.location_set === false) return 'Needs a location';
        // Turned on, but nothing in this app runs the schedule (no background service).
        return s.enabled && s.running === false ? 'Not running' : 'Off';
      default:
        return '';
    }
  },

  render() {
    const unavailable = this.available === false;
    this.unavailable.hidden = !unavailable;
    this.body.hidden = unavailable;
    this._renderPill();
    if (unavailable) return;
    const s = this.status;
    const st = this.settings;
    const mode = s?.mode || 'unknown';
    const enabled = !!(s ? s.enabled : st?.enabled);

    if (Date.now() - (this._enabledAt || 0) > 2500 && !this._pending) this.enabledIn.checked = enabled;
    this.modeEl.dataset.mode = mode;
    const modeText = this._modeText(s);
    const mt = this.modeEl.querySelector('.amb-mode-text');
    if (mt.textContent !== modeText) mt.textContent = modeText;

    // Status lines
    const lines = [];
    if (!s) {
      lines.push(this.available === null ? 'Checking...' : "Can't get the ambient status right now.");
    } else if (!s.enabled) {
      const from = hourText(st?.active_start || '08:00');
      const to = hourText(st?.active_end || '22:00');
      const night = (st?.night_text || '').trim() ? '' : ' The sign is off at night.';
      lines.push(`Shows the weather, a clock and world news from ${from} to ${to}.${night}`);
    } else if (s.location_set === false) {
      lines.push('Search for your city under Location to start.');
    } else if (mode === 'off') {
      lines.push('The sign keeps what it shows now.');
    } else {
      if (s.busy) lines.push('Updating the sign now...');
      if (mode === 'day' && s.next_upload) lines.push(`Next update ${when(s.next_upload)}`);
      if (mode === 'night') {
        const start = st?.active_start || '08:00';
        lines.push(`Weather and news start again at ${hourText(start)}`);
        // A night screen still to send (for example new night text) shows up as an earlier update.
        const nt = toMs(s.next_upload);
        if (isFinite(nt)) {
          const d = new Date(nt);
          const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
          if (hhmm !== start) lines.push(`Next update ${when(nt)}`);
        }
      }
      if (mode === 'paused') lines.push('Your own content stays on the sign until then.');
      if (s.last_upload) {
        const result = s.last_upload_ok === false
          ? "it didn't go through"
          : [fmtBytes(s.last_upload_bytes), s.last_upload_seconds != null ? `in ${fmtSeconds(s.last_upload_seconds)}` : ''].filter(Boolean).join(' ');
        lines.push(`Last update ${when(s.last_upload)}${result ? `, ${result}` : ''}`);
      }
    }
    const key = lines.join('\n');
    if (this.lines.dataset.key !== key) {
      this.lines.dataset.key = key;
      this.lines.replaceChildren(...lines.map((l) => h('p', l)));
    }

    const err = s && s.enabled && mode !== 'off' ? plainError(s.last_error) : '';
    this.errorEl.hidden = !err;
    if (err && this.errorEl.textContent !== err) this.errorEl.replaceChildren(h('span', err));
    this.errorEl.title = s?.last_error || '';

    // Controls
    const paused = mode === 'paused';
    if (!paused && this._pauseToast) {
      this._pauseToast();
      this._pauseToast = null;
    }
    const idle = !s || !enabled || mode === 'off';
    for (const b of this.pauseBtns) b.disabled = idle;
    this.resumeBtn.hidden = !paused;
    this.runBtn.disabled = idle || !!this._running || !!s.busy;

    this._renderWeather(s);
    this._renderFeeds(s);
    this._renderNote();
  },

  _renderPill() {
    const s = this.status;
    const show = this.available === true && !!s && s.enabled && ['day', 'night', 'paused'].includes(s.mode);
    this.pill.hidden = !show;
    if (!show) return;
    this.pill.dataset.mode = s.mode;
    const text = s.mode === 'paused' ? 'Ambient paused' : s.mode === 'night' ? 'Ambient: night' : 'Ambient';
    if (this.pillText.textContent !== text) this.pillText.textContent = text;
    const detail = this._modeText(s);
    this.pill.title = `Ambient mode: ${detail}. Click to open.`;
    this.pill.setAttribute('aria-label', `Ambient mode: ${detail}`);
  },

  _renderWeather(s) {
    const w = s?.weather;
    const unit = (this.settings?.units || 'F').toUpperCase();
    let text = 'No weather yet.';
    if (w && w.temp != null) {
      const parts = [`${Math.round(w.temp)}°${unit}`];
      if (w.condition) parts[0] += `, ${titleCase(w.condition)}`;
      if (w.high != null && w.low != null) parts.push(`High ${Math.round(w.high)}, low ${Math.round(w.low)}`);
      text = parts.join('. ') + '.';
      if (w.stale) text += ' Saved data, the latest update failed.';
    }
    if (this.weatherEl.textContent !== text) this.weatherEl.textContent = text;
    this.weatherEl.title = w?.updated ? `Updated ${when(w.updated)}` : '';
    this.weatherEl.classList.toggle('stale', !!w?.stale && w.temp != null);

    const heads = Array.isArray(s?.headlines) ? s.headlines : [];
    const key = JSON.stringify(heads.map((x) => [x.source, x.title]));
    if (this.headsEl.dataset.key !== key) {
      this.headsEl.dataset.key = key;
      this.headsEl.replaceChildren(
        ...heads.map((x) => h('li', { title: x.published ? `Published ${when(x.published)}` : null },
          h('span.amb-src', x.source || 'NEWS'), h('span.amb-title', x.title || ''))),
      );
    }
    this.headsEl.hidden = !heads.length;
    let info = '';
    if (!heads.length) info = s ? 'No headlines yet.' : '';
    else if (s?.news_updated) info = `News updated ${when(s.news_updated)}`;
    if (this.newsInfo.textContent !== info) this.newsInfo.textContent = info;
  },

  _renderFeeds(s) {
    const feeds = Array.isArray(s?.feeds) ? s.feeds : [];
    const on = this.settings?.news_sources || {};
    const key = JSON.stringify([feeds.map((f) => [f.id, f.name, f.ok, f.last_error]), on]);
    if (this.feedsEl.dataset.key === key) return;
    this.feedsEl.dataset.key = key;
    if (!feeds.length) {
      this.feedsEl.replaceChildren(h('p.muted.small', s ? 'No news sources yet.' : ''));
      return;
    }
    this.feedsEl.replaceChildren(
      ...feeds.map((f) => {
        const t = toggle(f.name || f.id, (v) => this._setFeed(f.id, v, t.input));
        t.input.checked = on[f.id] !== false;
        t.input.disabled = !this.settings;
        const state = f.ok === false
          ? h('span.amb-hint.warn', { title: f.last_error || '' }, 'Not working right now')
          : h('span.amb-hint', '');
        return h('div.amb-row', t.el, state);
      }),
    );
  },

  _renderNote() {
    const service = conn.status?.service;
    const show = this.available === true && service === false;
    this.noteEl.hidden = !show;
    if (!show) return;
    const auto = !!(this.auto?.installed && this.auto?.enabled);
    let text;
    if (this.status?.running === false) {
      // This window does not run the schedule itself: only the background service does.
      text = auto
        ? 'The background service is not running. Close LED Sign Studio and open it again to start it.'
        : 'Ambient mode runs in a background service. Turn on Start with Windows below, then open LED Sign Studio again.';
    } else {
      text = auto
        ? 'Ambient mode runs in the background from your next Windows sign-in. Until then it runs only while this app is open.'
        : 'Ambient mode runs only while this app is open. Turn on Start with Windows below to keep it running.';
    }
    if (this.noteEl.textContent !== text) this.noteEl.textContent = text;
  },

  // ------------------------------------------------------------ settings

  async loadSettings() {
    try {
      const s = await api.ambientSettings();
      if (this._pending || this._saving) return;
      this.settings = s;
      this.available = true;
      this.syncControls();
      this.render();
    } catch (e) {
      if (e.status === 404) {
        this.available = false;
        this.render();
      }
    }
  },

  /** Put the saved settings into every control, except the one being edited. */
  syncControls() {
    const s = this.settings;
    const disabled = !s;
    for (const el of this.body.querySelectorAll('input, button.btn')) {
      if (el === this.enabledIn || el === this.previewBtn || el === this.autoIn || this.pauseBtns.includes(el) || el === this.resumeBtn || el === this.runBtn) continue;
      if (el.closest('.amb-actions')) continue;
      el.disabled = disabled;
    }
    for (const b of this.manualSeg.children) b.disabled = disabled;
    for (const b of this.newsSpeedSeg.children) b.disabled = disabled;
    for (const b of this.unitsSeg.children) b.disabled = disabled;
    for (const b of this.clockFmtSeg.children) b.disabled = disabled;
    if (!s) return;
    const active = document.activeElement;
    if (active !== this.locIn) this.locIn.value = s.location?.name || '';
    if (active !== this.startIn) this.startIn.value = s.active_start || '08:00';
    if (active !== this.endIn) this.endIn.value = s.active_end || '22:00';
    this.refreshSl.setValue(clamp(Number(s.refresh_minutes) || 5, 3, 15));
    this.manualSeg.setValue(String(s.pause_minutes_on_manual ?? 30));
    for (const [id] of SCREENS) this.screenToggles[id].checked = s.screens?.[id]?.on !== false;
    this.clockSl.setValue(clamp(Number(s.screens?.clock?.seconds) || 25, 10, 50));
    this.newsSpeedSeg.setValue(String(s.news_speed ?? 'normal'));
    this.unitsSeg.setValue(s.units === 'C' ? 'C' : 'F');
    this.clockFmtSeg.setValue(s.clock_24h ? '24' : '12');
    this.brightSl.setValue(clamp(Number(s.day_brightness) || 3, 1, 10));
    this.colorSl.setValue(clamp(Math.round((Number(s.day_color_scale) || 0.7) * 100), 10, 100));
    this.nightSl.setValue(clamp(Number(s.night_brightness) || 1, 1, 10));
    if (active !== this.nightIn) this.nightIn.value = s.night_text || '';
    this._syncNight();
    this.clockSl.hidden = !this.screenToggles.clock.checked;
    this.newsSpeedEl.hidden = !this.screenToggles.news.checked;
    this.feedsEl.dataset.key = '';
    this._renderFeeds(this.status);
  },

  _syncNight() {
    const st = this.settings || {};
    const has = !!this.nightIn.value.trim();
    this.nightSl.hidden = !has;
    const from = hourText(st.active_end || '22:00');
    const to = hourText(st.active_start || '08:00');
    this.nightHint.textContent = has ? `Shown from ${from} to ${to}.` : `The sign is off from ${from} to ${to}. Type something to show it instead.`;
  },

  /** Change settings now and save them shortly (several quick changes go out together). */
  queue(patch, where, { now = false } = {}) {
    if (!this.settings) return;
    merge(this.settings, patch);
    this._pending = merge(this._pending || {}, patch);
    if (where) this._where.add(where);
    if (now) {
      this._saveSoon.cancel();
      this._flush();
    } else {
      this._saveSoon();
    }
  },

  async _flush() {
    if (!this._pending) return;
    if (this._saving) {
      this._saveAgain = true;
      return;
    }
    const patch = this._pending;
    const where = [...this._where];
    this._pending = null;
    this._where.clear();
    this._saving = true;
    try {
      const r = await api.ambientSave(patch);
      if (r && r.ok === false) throw new Error(r.message || 'The app did not take the change.');
      if (!this._pending && r && typeof r === 'object') {
        this.settings = r;
        this.syncControls();
      }
      for (const w of where) this._flashSaved(w);
      if (VISUAL.some((k) => k in patch)) this._previewSoon();
      this.poll();
    } catch (e) {
      toast(`Could not save the ambient settings. ${e.message || ''}`.trim(), { type: 'error', key: 'ambient-save' });
      this._pending = null;
      this._saving = false;
      await this.loadSettings();
    } finally {
      this._saving = false;
      if (this._saveAgain) {
        this._saveAgain = false;
        this._flush();
      }
    }
  },

  _flashSaved(where) {
    const el = this.saved[where];
    if (!el) return;
    el.classList.add('on');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('on'), 1600);
  },

  setEnabled(on) {
    this._enabledAt = Date.now();
    if (!this.settings) {
      this.enabledIn.checked = !on;
      return;
    }
    this.queue({ enabled: on }, 'top', { now: true });
  },

  _setHours() {
    const a = this.startIn.value;
    const b = this.endIn.value;
    if (!a || !b) return;
    if (a === b) {
      toast('Start and end need to be different times.', { type: 'error', key: 'ambient-hours' });
      this.startIn.value = this.settings.active_start;
      this.endIn.value = this.settings.active_end;
      return;
    }
    this.queue({ active_start: a, active_end: b }, 'hours');
    this.syncControls();
  },

  _setScreen(id, on) {
    const anyOn = SCREENS.some(([k]) => (k === id ? on : this.screenToggles[k].checked));
    if (!anyOn) {
      this.screenToggles[id].checked = true;
      toast('Keep at least one screen on.', { key: 'ambient-screens' });
      return;
    }
    if (id === 'clock') this.clockSl.hidden = !on;
    if (id === 'news') this.newsSpeedEl.hidden = !on;
    this.queue({ screens: { [id]: { on } } }, 'screens');
  },

  _setFeed(id, on, input) {
    const feeds = this.status?.feeds || [];
    const cur = this.settings?.news_sources || {};
    const anyOn = feeds.some((f) => (f.id === id ? on : cur[f.id] !== false));
    if (!anyOn && feeds.length) {
      input.checked = true;
      toast('Keep at least one news source on, or turn off the News screen.', { key: 'ambient-feeds' });
      return;
    }
    this.queue({ news_sources: { [id]: on } }, 'feeds');
  },

  // ------------------------------------------------------------ location search

  _initLocation() {
    const input = this.locIn;
    let seq = 0;
    let results = [];
    let active = -1;
    const close = () => {
      this.locList.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      active = -1;
    };
    const show = (children) => {
      this.locList.replaceChildren(...children);
      this.locList.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    };
    const mark = () => {
      [...this.locList.querySelectorAll('button')].forEach((b, i) => b.classList.toggle('active', i === active));
    };
    const pick = (r) => {
      const name = placeName(r);
      input.value = name;
      close();
      input.blur();
      this.queue({ location: { name, lat: r.lat, lon: r.lon, timezone: r.timezone } }, 'loc', { now: true });
    };
    const search = debounce(async () => {
      const q = input.value.trim();
      const my = ++seq;
      if (q.length < 2) {
        close();
        return;
      }
      show([h('p', 'Searching...')]);
      try {
        const list = await api.geocode(q);
        if (my !== seq) return;
        results = Array.isArray(list) ? list.slice(0, 8) : [];
        active = -1;
        if (!results.length) {
          show([h('p', 'No places found. Try another spelling.')]);
          return;
        }
        show(results.map((r) =>
          h('button', {
            type: 'button',
            role: 'option',
            // Keep focus in the box so picking does not count as leaving it.
            onmousedown: (e) => e.preventDefault(),
            onclick: () => pick(r),
          },
          h('span.amb-place', r.name),
          h('span.muted', [r.admin1, r.country].filter(Boolean).join(', ')))));
      } catch (e) {
        if (my !== seq) return;
        show([h('p', e.code === 'offline' || e.code === 'timeout' ? e.message : "Can't search right now. Check the internet connection.")]);
      }
    }, 350);
    input.addEventListener('focus', () => input.select());
    input.addEventListener('input', search);
    input.addEventListener('keydown', (e) => {
      const n = results.length;
      if (e.key === 'ArrowDown' && n && !this.locList.hidden) {
        e.preventDefault();
        active = (active + 1) % n;
        mark();
      } else if (e.key === 'ArrowUp' && n && !this.locList.hidden) {
        e.preventDefault();
        active = (active - 1 + n) % n;
        mark();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (!this.locList.hidden && n && this.locList.querySelector('button')) pick(results[Math.max(0, active)]);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        seq++;
        close();
        input.value = this.settings?.location?.name || '';
        input.blur();
      }
    });
    input.addEventListener('blur', () => {
      // Leaving the box without picking keeps the saved place.
      setTimeout(() => {
        if (document.activeElement === input) return;
        seq++;
        search.cancel();
        close();
        input.value = this.settings?.location?.name || '';
      }, 150);
    });
  },

  // ------------------------------------------------------------ actions

  async pause(minutes) {
    try {
      this.status = await api.ambientPause(minutes);
      this.render();
    } catch (e) {
      toast(`Could not pause. ${e.message}`, { type: 'error', key: 'ambient' });
    }
    this.poll();
  },

  async resume() {
    try {
      this.status = await api.ambientResume();
      this.render();
      this._pauseToast = null;
      toast('Ambient mode is back on.', { type: 'ok', key: 'ambient-pause', timeout: 2500 });
    } catch (e) {
      toast(`Could not resume. ${e.message}`, { type: 'error', key: 'ambient' });
    }
    this.poll();
  },

  async runNow() {
    if (this._running) return;
    this._running = true;
    this.render();
    const label = this.runBtn.querySelector('span:last-child');
    label.textContent = 'Updating...';
    const before = this.status?.last_upload;
    conn.fast(4000);
    try {
      const r = await api.ambientRunNow();
      if (r && r.ok === false) throw new Error(explainRun(r));
      if (r && r.mode) this.status = r;
      if (r?.last_upload_ok === false && r.last_upload !== before) {
        toast(plainError(r.last_error) || "The update didn't go through.", { type: 'error', key: 'ambient' });
      } else if (r?.last_upload && r.last_upload !== before) {
        toast('Sign updated.', { type: 'ok', key: 'ambient' });
      } else {
        toast('Updating the sign now.', { key: 'ambient' });
      }
    } catch (e) {
      toast(`Could not update the sign. ${e.message}`, { type: 'error', key: 'ambient' });
    } finally {
      this._running = false;
      label.textContent = 'Run now';
      this.render();
      conn.fast(3000);
      this.poll();
    }
  },

  async refreshPreview() {
    const seq = ++this._pseq;
    this._previewBusy = true;
    this.previewBtn.disabled = true;
    this.preview.message(this.preview.loaded ? '' : 'Making a preview...');
    this.preview.el.classList.add('busy');
    try {
      const r = await api.ambientPreview(1);
      if (seq !== this._pseq) return;
      if (!r || !r.gif) throw new Error((r && r.message) || 'The app sent back no picture.');
      await this.preview.load(r);
      this.preview.message('');
    } catch (e) {
      if (seq !== this._pseq) return;
      if (e.status === 404) {
        this.available = false;
        this.render();
      }
      const why = e.message || '';
      const text = /^could not/i.test(why) ? why : `Could not make a preview. ${why}`.trim();
      // Keep showing the last preview when a refresh fails.
      if (this.preview.loaded) toast(text, { type: 'error', key: 'ambient-preview' });
      else this.preview.message(text);
    } finally {
      if (seq === this._pseq) {
        this._previewBusy = false;
        this.previewBtn.disabled = false;
        this.preview.el.classList.remove('busy');
      }
    }
  },

  // ------------------------------------------------------------ start with Windows

  async loadAutostart() {
    try {
      this.auto = await api.autostart();
    } catch (e) {
      this.auto = null;
      this.autoNote.textContent = e.status === 404 ? 'Not available in this version of the app.' : "Can't check this right now.";
      this.autoIn.disabled = true;
      this._renderNote();
      return;
    }
    this._renderAuto();
  },

  _renderAuto() {
    const a = this.auto || {};
    if (Date.now() - (this._autoAt || 0) > 1500) this.autoIn.checked = !!(a.installed && a.enabled);
    this.autoIn.disabled = !!this._autoBusy;
    let text;
    if (this._autoBusy) text = this.autoIn.checked ? 'Turning on...' : 'Turning off...';
    else if (a.installed && a.enabled) text = a.running ? 'Running in the background now.' : 'Starts in the background when you sign in to Windows.';
    else text = 'Keeps ambient mode going when this window is closed, from the moment you sign in.';
    this.autoNote.textContent = text;
    this._renderNote();
  },

  async setAutostart(on) {
    this._autoAt = Date.now();
    this._autoBusy = true;
    this._renderAuto();
    try {
      const r = await api.setAutostart(on);
      if (r && r.ok === false) throw new Error(r.message || 'Windows did not take the change.');
      this.auto = r;
      this._autoAt = 0;
    } catch (e) {
      this.autoIn.checked = !on;
      toast(`Could not change Start with Windows. ${e.message}`, { type: 'error', key: 'ambient-auto' });
    } finally {
      this._autoBusy = false;
      this._renderAuto();
    }
  },

  // ------------------------------------------------------------ manual sends pause ambient mode

  async _afterManualSend({ quiet }) {
    if (this.available === false) return;
    const wasPaused = this.status?.mode === 'paused';
    let s;
    try {
      s = await api.ambientStatus();
    } catch {
      return;
    }
    this.status = s;
    this.available = true;
    this.render();
    if (!s || !s.enabled || s.mode !== 'paused') return;
    // Live mode sends often: only say it when the pause starts.
    if (quiet && wasPaused) return;
    const left = Math.round((toMs(s.paused_until) - Date.now()) / 60000);
    const set = Number(this.settings?.pause_minutes_on_manual);
    let text = 'Ambient mode paused';
    if (set && Math.abs(left - set) <= 2) text += ` for ${minutesText(set)}`;
    else if (isFinite(left)) text += ` until ${when(s.paused_until)}`;
    this._pauseToast = toast(text, { key: 'ambient-pause', timeout: 8000, action: { label: 'Resume', fn: () => this.resume() } });
  },
};

function explainRun(r) {
  if (r.code === 'not_connected') return 'The sign is not connected.';
  if (r.code === 'busy') return 'The sign is busy with another upload. Try again in a moment.';
  return r.message || 'Something went wrong.';
}

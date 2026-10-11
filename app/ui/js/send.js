// Sending to the sign: main Send button, slideshow, live mode, result messages.

import { api } from './api.js';
import { store, clampMs, MAX_MS, BIG_UPLOAD_BYTES } from './state.js';
import { conn } from './connection.js';
import { h, $, toB64, fmtSeconds, fmtBytes } from './util.js';
import { icon } from './icons.js';
import { toast, popover, closePopover, segmented, slider, modal } from './ui.js';

const MAX_SLIDES = 30;
const TRANSITIONS = [
  ['left', 'Slide left'],
  ['right', 'Slide right'],
  ['up', 'Slide up'],
  ['down', 'Slide down'],
  ['snow', 'Snow'],
  ['roll', 'Roll'],
  ['laser', 'Laser'],
  ['static', 'None'],
];

let inFlight = 0;
const sendListeners = new Set();
export const onSendState = (fn) => sendListeners.add(fn);
const notify = () => sendListeners.forEach((fn) => fn(inFlight > 0));
// Called after the sign took a manual send ({quiet} is true for live mode updates).
const sentListeners = new Set();
export const onSent = (fn) => sentListeners.add(fn);
const notifySent = (info) => sentListeners.forEach((fn) => {
  try {
    fn(info);
  } catch (e) {
    console.error(e);
  }
});

/** Messages for error codes. */
function explain(code, message) {
  switch (code) {
    case 'not_connected':
      return 'The sign is not connected.';
    case 'storage_full':
      return 'The sign is out of space. Try fewer frames.';
    case 'no_confirm':
      return "The sign didn't confirm. Try again.";
    case 'busy':
      return 'The sign is busy with another upload. Try again in a moment.';
    case 'bad_request':
      return message || 'The sign could not use that. Try again.';
    case 'offline':
    case 'timeout':
    case 'forbidden':
      return message;
    default:
      return message || 'Something went wrong. Try again.';
  }
}

function showError(code, message, { quietKey = null } = {}) {
  const action = code === 'not_connected' ? { label: 'Connect', fn: () => conn.retry() } : null;
  toast(explain(code, message), { type: 'error', action, key: quietKey || 'send' });
}

/** Check that the frames can go to the connected sign. Returns an error message or null. */
function sizeProblem(W, H) {
  const dev = conn.device;
  if (dev && dev.width && dev.height && (dev.width !== W || dev.height !== H)) {
    return `Your canvas is ${W}x${H} but the sign is ${dev.width}x${dev.height}.`;
  }
  if (!dev && W > 64) return 'This is wider than the sign can show.';
  return null;
}

function sizeToast(problem) {
  const action = conn.device ? { label: 'Fit to sign', fn: () => conn.matchSize() } : null;
  toast(problem, { type: 'error', key: 'send', action });
}

// ---------------------------------------------------------------- big uploads

const ORDINAL = { 2: '2nd', 3: '3rd', 4: '4th' };

/** Every k-th frame, each shown as long as the k frames it replaces, so the speed stays the same. */
function everyKth(frames, durations, k) {
  const f = [];
  const d = [];
  for (let i = 0; i < frames.length; i += k) {
    let ms = 0;
    for (let j = i; j < Math.min(i + k, frames.length); j++) ms += clampMs(durations[j] ?? durations[0]);
    f.push(frames[i]);
    d.push(Math.min(MAX_MS, ms));
  }
  return { frames: f, durations: d };
}

/** A lighter version that fits under the limit, or the lightest tried. Null when none helps. */
async function lighterVersion(frames, durations, size, full) {
  let best = null;
  for (const k of [2, 3, 4]) {
    if (frames.length / k < 4) break;
    const v = everyKth(frames, durations, k);
    const est = await api.estimate(v.frames.map((f) => toB64(f)), v.durations, size);
    best = { ...v, est, k };
    if (est.bytes <= BIG_UPLOAD_BYTES) break;
  }
  return best && best.est.bytes < full.bytes * 0.8 ? best : null;
}

function bigUploadDialog(est, light) {
  return new Promise((resolve) => {
    let answer = null;
    const pick = (v) => () => {
      answer = v;
    };
    const body = h('div',
      h('p', `It takes about ${fmtSeconds(est.seconds)} to send (${fmtBytes(est.bytes)}). The sign keeps showing its old content until it is done.`),
      light
        ? h('p.muted', `Shorter option: keep every ${ORDINAL[light.k]} frame at the same speed. About ${fmtSeconds(light.est.seconds)} to send. The motion is a little less smooth.`)
        : null,
    );
    const actions = [{ label: 'Cancel', fn: pick(null) }];
    if (light) actions.push({ label: 'Send shorter', fn: pick('light') });
    actions.push({ label: 'Send anyway', primary: true, fn: pick('full') });
    modal({ title: 'This is a big upload', body, actions, onClose: () => resolve(answer) });
  });
}

/**
 * Before a long upload, ask. Returns {frames, durations} to send, or null when cancelled.
 * Small uploads (and anything when the estimate fails) go straight through.
 */
async function confirmBig(frames, durations, size) {
  if (frames.length < 2) return { frames, durations };
  let est;
  try {
    est = await api.estimate(frames.map((f) => toB64(f)), durations, size);
  } catch {
    return { frames, durations };
  }
  if (!est || !(est.bytes > BIG_UPLOAD_BYTES)) return { frames, durations };
  let light = null;
  try {
    light = await lighterVersion(frames, durations, size, est);
  } catch {
    light = null;
  }
  const choice = await bigUploadDialog(est, light);
  if (choice === 'full') return { frames, durations };
  if (choice === 'light') return { frames: light.frames, durations: light.durations };
  return null;
}

let preparing = false;

/**
 * Send frames (Uint8ClampedArray[]) with durations (ms[]). One frame sends a still.
 * Returns true when the sign took it.
 */
export async function sendFrames(frames, durations, { quiet = false, what = null, size = null } = {}) {
  size = size || { width: store.width, height: store.height };
  const problem = sizeProblem(size.width, size.height);
  if (problem) {
    sizeToast(problem);
    return false;
  }
  if (!quiet && frames.length > 1) {
    if (preparing) return false;
    preparing = true;
    let picked;
    try {
      picked = await confirmBig(frames, frames.map((_, i) => clampMs(durations[i] ?? durations[0])), size);
    } finally {
      preparing = false;
    }
    if (!picked) return false;
    if (inFlight) {
      toast('Still sending. Wait for it to finish.', { key: 'send' });
      return false;
    }
    ({ frames, durations } = picked);
  }
  inFlight++;
  notify();
  conn.fast(4000);
  try {
    const b64 = frames.map((f) => toB64(f));
    const r =
      b64.length === 1
        ? await api.sendStill(b64[0], size)
        : await api.sendAnimation(b64, frames.map((_, i) => clampMs(durations[i] ?? durations[0])), size);
    if (r && r.ok) {
      if (!quiet) {
        const secs = r.seconds != null ? `Sent in ${fmtSeconds(r.seconds)}` : 'Sent';
        toast(what ? `${secs}: ${what}` : secs, { type: 'ok', key: 'send' });
      }
      notifySent({ quiet });
      return true;
    }
    showError(r?.code, r?.message, { quietKey: quiet ? 'live' : null });
    return false;
  } catch (e) {
    showError(e.code, e.message, { quietKey: quiet ? 'live' : null });
    return false;
  } finally {
    inFlight--;
    notify();
    conn.fast(1500);
  }
}

export async function sendSlideshow(frames, transition, speed, dwell) {
  const problem = sizeProblem(store.width, store.height);
  if (problem) {
    sizeToast(problem);
    return false;
  }
  inFlight++;
  notify();
  conn.fast(4000);
  try {
    const r = await api.sendSlideshow(frames.map((f) => toB64(f)), transition, speed, dwell);
    if (r && r.ok) {
      toast(r.seconds != null ? `Sent in ${fmtSeconds(r.seconds)}` : 'Sent', { type: 'ok', key: 'send' });
      notifySent({ quiet: false });
      return true;
    }
    showError(r?.code, r?.message);
    return false;
  } catch (e) {
    showError(e.code, e.message);
    return false;
  } finally {
    inFlight--;
    notify();
    conn.fast(1500);
  }
}

export const isSending = () => inFlight > 0;

// ---------------------------------------------------------------- header Send button

// The sign's own speed and hold units are unknown (not measured on the real sign), so the UI
// never promises seconds. Hold values stay well clear of 0, which could break playback, and
// read sensibly whether the unit is 0.1 s (1, 3, 6 s) or 1 s. The server clamps them too.
const HOLDS = [['10', 'Short'], ['30', 'Medium'], ['60', 'Long']];
const slideOpts = { transition: 'left', speed: 3, dwell: 30 };

export const sender = {
  init() {
    this.btn = $('#sendBtn');
    this.more = $('#sendMore');
    this.label = $('#sendBtn .send-label');
    this.fill = $('#sendBtn .send-fill');
    this.btn.addEventListener('click', () => this.sendProject());
    this.more.addEventListener('click', () => this.openMore());
    store.on('change', () => this.sync());
    onSendState(() => this.sync());
    conn.onStatus(() => this.sync());
    live.init();
    this.sync();
  },

  sendProject() {
    if (inFlight) {
      toast('Still sending. Wait for it to finish.', { key: 'send' });
      return;
    }
    const frames = store.frames;
    sendFrames(frames.map((f) => f.pixels), frames.map((f) => f.ms));
  },

  sync() {
    const n = store.frames.length;
    const sending = inFlight > 0;
    const s = conn.status;
    const p = s?.busy ? s.progress : null;
    let text = n === 1 ? 'Send to sign' : 'Send animation';
    if (sending) text = p != null ? `Sending ${Math.round(p * 100)}%` : 'Sending...';
    if (this.label.textContent !== text) this.label.textContent = text;
    this.btn.classList.toggle('sending', sending);
    this.fill.style.width = sending ? `${Math.round((p ?? 0.04) * 100)}%` : '0%';
    this.btn.disabled = sending;
    this.btn.title = n === 1 ? 'Send this picture to the sign (Ctrl+Enter)' : `Send all ${n} frames as an animation (Ctrl+Enter)`;
  },

  openMore() {
    const n = store.frames.length;
    const tooMany = n > MAX_SLIDES;
    const trans = segmented(TRANSITIONS.map(([v, l]) => [v, l]), slideOpts.transition, (v) => (slideOpts.transition = v), { small: true });
    trans.classList.add('wrap');
    const speed = slider({ label: 'Transition speed', min: 1, max: 10, value: slideOpts.speed, onInput: (v) => (slideOpts.speed = v) });
    const dwell = h('div.field', h('span.field-label', 'Hold each frame'),
      segmented(HOLDS, String(slideOpts.dwell), (v) => (slideOpts.dwell = Number(v)), { small: true }));
    // A slideshow is raw frames: 3 bytes per LED per frame, at about 6.5 KB/s.
    const secs = (n * store.width * store.height * 3 + 64) / 6500 + 1;
    const go = h('button.btn.primary.block', {
      type: 'button',
      disabled: tooMany || inFlight > 0,
      onclick: () => {
        closePopover();
        sendSlideshow(store.frames.map((f) => f.pixels), slideOpts.transition, slideOpts.speed, slideOpts.dwell);
      },
    }, h('span', { html: icon('slides') }), n === 1 ? 'Send with transition' : `Send ${n} frames as a slideshow`);
    const content = h('div.more-pop',
      h('h3', 'Send as slideshow'),
      h('p.muted', 'The sign shows each frame in turn with its own transition. It sets the timing itself, so frame times are ignored and speed and hold are rough.'),
      h('div.field', h('span.field-label', 'Transition'), trans),
      speed,
      dwell,
      tooMany ? h('p.warn-text', `A slideshow holds up to ${MAX_SLIDES} frames. This has ${n}.`) : h('p.muted', `About ${fmtSeconds(secs)} to send.`),
      go,
    );
    popover(this.more, content, { align: 'end' });
  },
};

// ---------------------------------------------------------------- live mode

export const live = {
  on: false,
  timer: 0,
  busy: false,
  pending: false,
  lastSent: null,

  init() {
    this.input = $('#liveToggle');
    this.wrap = $('#liveWrap');
    this.input.addEventListener('change', () => this.set(this.input.checked));
    store.on('change', (e) => {
      this.syncUi();
      if (!this.on || !e.edit) return;
      this.lastEditAt = Date.now();
      this.schedule();
    });
    this.syncUi();
  },

  set(on) {
    this.on = on;
    this.input.checked = on;
    this.syncUi();
    if (on) {
      if (store.frames.length !== 1) {
        toast('Live mode works with a single frame.', { key: 'live' });
      } else {
        toast('Live mode on. Each change goes to the sign after a short pause.', { key: 'live' });
        this.lastSent = null;
        this.schedule(50);
      }
    } else {
      clearTimeout(this.timer);
      this.pending = false;
    }
  },

  syncUi() {
    const single = store.frames.length === 1;
    this.wrap.classList.toggle('paused', this.on && !single);
    this.wrap.classList.toggle('active', this.on && single);
    this.wrap.title = !single && this.on
      ? 'Live mode is paused: it works with a single frame.'
      : 'Live mode: sends each change by itself. Each update takes about a second.';
  },

  schedule(ms = 700) {
    clearTimeout(this.timer);
    if (store.frames.length !== 1) return;
    this.timer = setTimeout(() => this.fire(), ms);
  },

  async fire() {
    if (!this.on || store.frames.length !== 1) return;
    const px = store.frames[0].pixels;
    if (px === this.lastSent) return;
    if (this.busy || inFlight) {
      // Newest wins: send again once the current upload finishes.
      this.pending = true;
      return;
    }
    this.busy = true;
    this.wrap.classList.add('sending');
    const ok = await sendFrames([px], [1000], { quiet: true });
    this.busy = false;
    this.wrap.classList.remove('sending');
    if (ok) this.lastSent = px;
    if (this.pending || (ok && store.frames[0].pixels !== px)) {
      this.pending = false;
      // Still wait for a pause in editing, so a long drawing session does not upload nonstop.
      this.schedule(ok ? Math.max(100, 700 - (Date.now() - (this.lastEditAt || 0))) : 2500);
    }
  },
};

// Pending live updates also go out once a regular send finishes.
onSendState((busy) => {
  if (!busy && live.on && live.pending && !live.busy) {
    live.pending = false;
    live.schedule(150);
  }
});


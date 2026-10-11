// Talks to the local app server. Every /api call carries the X-Token header.

import { store } from './state.js';

const TOKEN_KEY = 'ledsign.token';

function readToken() {
  let fromUrl = '';
  try {
    const url = new URL(location.href);
    fromUrl = url.searchParams.get('t') || '';
    if (fromUrl) {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
      // Keep the token out of the address bar once it is stored.
      url.searchParams.delete('t');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    }
  } catch {
    // sessionStorage blocked: keep the token in the URL so a reload still works.
  }
  if (fromUrl) return fromUrl;
  try {
    return sessionStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

const token = readToken();
export const hasToken = () => !!token;

export class ApiError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function call(method, path, body, { timeout = 30000, keepalive = false } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const headers = { 'X-Token': token };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      keepalive,
      cache: 'no-store',
    });
  } catch (e) {
    clearTimeout(timer);
    if (e && e.name === 'AbortError') throw new ApiError('timeout', 'The app did not answer in time.');
    throw new ApiError('offline', 'Cannot reach the app. Is it still running?');
  }
  clearTimeout(timer);
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.status === 403) throw new ApiError('forbidden', 'This page needs to be opened from LED Sign Studio.', 403);
  if (!res.ok) {
    // A refused request ({ok:false, code, message} with a 4xx/5xx status) always throws, so no
    // caller mistakes the error body for a result. Sign errors come back as 200 {ok:false}.
    throw new ApiError((data && data.code) || 'error', (data && data.message) || `Request failed (${res.status}).`, res.status);
  }
  return data;
}

const get = (p, o) => call('GET', p, undefined, o);
const post = (p, b = {}, o) => call('POST', p, b, o);
const SEND_TIMEOUT = 240000;
// Pixel calls carry the canvas size so the server checks it against the sign.
const sz = () => ({ width: store.width, height: store.height });

export const api = {
  health: () => get('/api/health', { timeout: 4000 }),
  status: () => get('/api/status', { timeout: 4000 }),
  connect: () => post('/api/connect', {}, { timeout: 15000 }),
  disconnect: () => post('/api/disconnect', {}, { timeout: 15000 }),
  fonts: () => get('/api/fonts'),
  textMask: (text, font, size) => post('/api/text_mask', { text, font, size, height: store.height }),
  sendStill: (pixels, size = sz()) => post('/api/send_still', { pixels, ...size }, { timeout: SEND_TIMEOUT }),
  sendAnimation: (frames, durations, size = sz()) =>
    post('/api/send_animation', { frames, durations, ...size }, { timeout: SEND_TIMEOUT }),
  sendSlideshow: (frames, transition, speed, dwell) =>
    post('/api/send_slideshow', { frames, transition, speed, dwell, ...sz() }, { timeout: SEND_TIMEOUT }),
  estimate: (frames, durations, size = sz()) => post('/api/estimate', { frames, durations, ...size }, { timeout: 60000 }),
  importFile: (name, data, fit, maxFrames) =>
    post('/api/import', { name, data, fit, max_frames: maxFrames, ...sz() }, { timeout: 120000 }),
  exportGif: (frames, durations, scale) => post('/api/export_gif', { frames, durations, scale, ...sz() }, { timeout: 120000 }),
  getAutosave: () => get('/api/autosave', { timeout: 10000 }),
  autosave: (project, opts) => post('/api/autosave', project, { timeout: 20000, ...opts }),
  library: () => get('/api/library'),
  libraryLoad: (slug) => get('/api/library/load?slug=' + encodeURIComponent(slug)),
  librarySave: (name, project) => post('/api/library/save', { name, project }, { timeout: 30000 }),
  libraryDelete: (slug) => post('/api/library/delete', { slug }),
  brightness: (level) => post('/api/brightness', { level }),
  power: (on) => post('/api/power', { on }),
  openBluetoothSettings: () => post('/api/open_bluetooth_settings'),
  log: (level, message) => post('/api/log', { level, message }, { timeout: 5000 }).catch(() => {}),
  // Ambient mode (weather, clock and news on the sign during the day).
  ambientStatus: () => get('/api/ambient/status', { timeout: 8000 }),
  ambientSettings: () => get('/api/ambient/settings', { timeout: 10000 }),
  ambientSave: (patch) => post('/api/ambient/settings', patch, { timeout: 15000 }),
  ambientPreview: (minutes = 1) => post('/api/ambient/preview', { minutes }, { timeout: 120000 }),
  ambientRunNow: () => post('/api/ambient/run_now', {}, { timeout: SEND_TIMEOUT }),
  ambientPause: (minutes) => post('/api/ambient/pause', { minutes }, { timeout: 15000 }),
  ambientResume: () => post('/api/ambient/resume', {}, { timeout: 15000 }),
  geocode: (q) => get('/api/ambient/geocode?q=' + encodeURIComponent(q), { timeout: 15000 }),
  autostart: () => get('/api/autostart', { timeout: 20000 }),
  setAutostart: (enabled) => post('/api/autostart', { enabled }, { timeout: 30000 }),
};

// Send UI errors to the server log so they can be read later.
let logged = 0;
function report(level, message) {
  if (logged++ > 50) return;
  api.log(level, String(message).slice(0, 2000));
}
window.addEventListener('error', (e) => {
  // Harmless browser notice while panels resize; not an app error.
  if (String(e.message).startsWith('ResizeObserver loop')) return;
  report('error', `${e.message} at ${e.filename}:${e.lineno}:${e.colno}`);
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason;
  if (r instanceof ApiError) return; // already shown to the user
  report('error', `Unhandled: ${(r && (r.stack || r.message)) || r}`);
});

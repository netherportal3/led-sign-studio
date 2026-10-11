// Connection status: polling, header pill, connect button, brightness, display power, help panel.

import { api } from './api.js';
import { store } from './state.js';
import { h, $, debounce } from './util.js';
import { icon } from './icons.js';
import { toast, paintRange } from './ui.js';
import { autosave } from './library.js';

const PROBLEM = new Set(['not_found', 'bluetooth_off', 'error']);

export const conn = {
  status: null,
  offline: false,
  forbidden: false,
  _listeners: new Set(),
  _timer: 0,
  _deviceKey: '',
  helpHidden: false,
  lastProblem: null,

  onStatus(fn) {
    this._listeners.add(fn);
  },
  get state() {
    if (this.forbidden) return 'forbidden';
    if (this.offline) return 'offline';
    return this.status?.state || 'searching';
  },
  get connected() {
    return !this.offline && this.status?.state === 'connected';
  },
  get device() {
    return this.status?.device || null;
  },
  get busy() {
    return !!this.status?.busy;
  },

  init() {
    this.pill = $('#connPill');
    this.pillText = $('#connPill .pill-text');
    this.pillBar = $('#connPill .pill-bar i');
    this.connectBtn = $('#connectBtn');
    this.connectBtn.addEventListener('click', () => this.toggleConnection());
    this._initBrightness();
    this._initPower();
    this._buildHelp();
    store.on('change', (e) => {
      if (e.kind === 'load' || e.kind === 'Open project' || e.kind === 'New project') this.matchSize();
    });
    this.poll();
    document.addEventListener('visibilitychange', () => !document.hidden && this.poll());
  },

  async poll() {
    clearTimeout(this._timer);
    // One request at a time: a poll asked for mid-flight runs right after the current one.
    if (this._inFlight) {
      this._again = true;
      return;
    }
    this._inFlight = true;
    try {
      const s = await api.status();
      this.status = s;
      this.offline = false;
      this.forbidden = false;
      this._failures = 0;
    } catch (e) {
      if (e.code === 'forbidden') this.forbidden = true;
      // One missed poll is not an outage; the server may be busy for a moment.
      else if (++this._failures >= 3) this.offline = true;
    }
    this._inFlight = false;
    this._render();
    for (const fn of this._listeners) {
      try {
        fn(this.status);
      } catch (e) {
        console.error(e);
      }
    }
    const fast = this.busy || this._fastUntil > Date.now();
    clearTimeout(this._timer);
    if (this._again) {
      this._again = false;
      this._timer = setTimeout(() => this.poll(), 0);
      return;
    }
    this._timer = setTimeout(() => this.poll(), this.forbidden ? 5000 : fast ? 250 : 500);
  },

  /** Poll quickly for a while (an upload just started). */
  fast(ms = 3000) {
    this._fastUntil = Date.now() + ms;
    this.poll();
  },

  async toggleConnection() {
    const s = this.state;
    const wantDisconnect = s === 'connected' || s === 'connecting' || s === 'searching';
    this.connectBtn.disabled = true;
    try {
      this.status = wantDisconnect ? await api.disconnect() : await api.connect();
      if (wantDisconnect) toast('Disconnected. The phone app can connect now.', { key: 'conn' });
    } catch (e) {
      toast(e.message, { type: 'error', key: 'conn' });
    } finally {
      this.connectBtn.disabled = false;
      this.fast(2000);
    }
  },

  async retry() {
    this.helpHidden = false;
    try {
      this.status = await api.connect();
    } catch (e) {
      toast(e.message, { type: 'error', key: 'conn' });
    }
    this.fast(2000);
  },

  _render() {
    const s = this.status || {};
    const state = this.state;
    let text;
    let tone = state;
    const dev = s.device;
    switch (state) {
      case 'connected':
        text = dev ? `Connected to ${dev.name}, ${dev.width}x${dev.height}` : 'Connected';
        break;
      case 'searching':
        text = 'Searching for the sign';
        break;
      case 'connecting':
        text = dev?.name ? `Connecting to ${dev.name}` : 'Connecting';
        break;
      case 'not_found':
        text = 'Not found';
        break;
      case 'bluetooth_off':
        text = 'Bluetooth is off';
        break;
      case 'disconnected':
        text = 'Disconnected';
        break;
      case 'offline':
        text = 'App not responding';
        break;
      case 'forbidden':
        text = 'Open from the app';
        break;
      default:
        text = 'Connection problem';
        tone = 'error';
    }
    if (s.busy && !this.offline) {
      const p = s.progress;
      text = p == null ? 'Uploading' : `Uploading ${Math.round(p * 100)}%`;
      tone = 'busy';
      this.pillBar.style.width = `${Math.round((p ?? 0) * 100)}%`;
    }
    this.pill.dataset.state = tone;
    this.pill.title = s.message || text;
    if (this.pillText.textContent !== text) this.pillText.textContent = text;
    $('#fakeBadge').hidden = !s.fake;

    const isOn = state === 'connected' || state === 'connecting' || state === 'searching';
    const label = isOn ? 'Disconnect' : 'Connect';
    if (this.connectBtn.dataset.label !== label) {
      this.connectBtn.dataset.label = label;
      this.connectBtn.innerHTML = icon(isOn ? 'unlink' : 'link') + `<span>${label}</span>`;
      this.connectBtn.title = isOn ? 'Let go of the sign so the phone app can connect' : 'Connect to the sign';
    }
    this.connectBtn.disabled = state === 'offline' || state === 'forbidden';
    const ok = state === 'connected';
    // Show what the server last set, unless the user just moved the control.
    const quiet = Date.now() - (this._touchedAt || 0) > 2500;
    if (quiet && Number.isInteger(s.brightness) && String(s.brightness) !== this.brightness.value) {
      this.brightness.value = s.brightness;
      $('#brightnessOut').textContent = s.brightness;
      paintRange(this.brightness);
    }
    if (quiet && typeof s.display_on === 'boolean' && s.display_on !== this.power.checked) this.power.checked = s.display_on;
    this.brightness.disabled = !ok;
    this.power.disabled = !ok;
    $('#signControls').classList.toggle('disabled', !ok);
    $('#signControls').title = ok ? '' : 'Connect to the sign to use these';

    // Match the canvas to the sign once per device (and when a project is opened, below).
    if (ok && dev && dev.width && dev.height) {
      const key = `${dev.address || dev.name}|${dev.width}x${dev.height}`;
      if (key !== this._deviceKey) {
        this._deviceKey = key;
        this.matchSize();
      }
    }
    this._renderHelp(state, s);
  },

  /** Resize the canvas to the connected sign. Undo can bring the old size back. */
  matchSize() {
    const dev = this.device;
    if (!this.connected || !dev || !dev.width || !dev.height) return false;
    if (dev.width === store.width && dev.height === store.height) return false;
    store.resize(dev.width, dev.height, 'Match the sign size');
    toast(`The canvas now matches your sign: ${dev.width}x${dev.height}.`, { key: 'size', action: { label: 'Undo', fn: () => store.undo() } });
    return true;
  },

  // ------------------------------------------------------------ brightness and power

  _initBrightness() {
    const input = $('#brightness');
    const out = $('#brightnessOut');
    this.brightness = input;
    let saved = 10;
    try {
      saved = Number(localStorage.getItem('ledsign.brightness')) || 10;
    } catch {}
    input.value = saved;
    out.textContent = saved;
    paintRange(input);
    const send = debounce(async (v) => {
      try {
        const r = await api.brightness(v);
        if (r && r.ok === false) throw new Error(r.message || 'The sign did not take the change.');
        try {
          localStorage.setItem('ledsign.brightness', String(v));
        } catch {}
      } catch (e) {
        toast(e.message || 'Could not change brightness.', { type: 'error', key: 'bright' });
      }
    }, 250);
    input.addEventListener('input', () => {
      this._touchedAt = Date.now();
      out.textContent = input.value;
      paintRange(input);
      send(Number(input.value));
    });
  },

  _initPower() {
    const input = $('#powerToggle');
    this.power = input;
    input.addEventListener('change', async () => {
      this._touchedAt = Date.now();
      const on = input.checked;
      try {
        const r = await api.power(on);
        if (r && r.ok === false) throw new Error(r.message || 'The sign did not take the change.');
        toast(on ? 'Display on' : 'Display off', { key: 'power', timeout: 1500 });
      } catch (e) {
        input.checked = !on;
        toast(e.message || 'Could not switch the display.', { type: 'error', key: 'power' });
      }
    });
  },

  // ------------------------------------------------------------ help panel

  _buildHelp() {
    const el = $('#connHelp');
    this.help = el;
    const step = (ic, title, body, extra = null, id = null) =>
      h('li.help-step', { 'data-step': id }, h('span.help-ic', { html: icon(ic) }), h('div', h('strong', title), h('p', body), extra));
    this.helpTitle = h('h2');
    this.helpLead = h('p.help-lead');
    this.helpLive = h('span.help-live');
    const btBtn = h('button.btn.sm', { type: 'button', onclick: () => api.openBluetoothSettings().catch((e) => toast(e.message, { type: 'error' })) },
      h('span', { html: icon('bluetooth') }), 'Open Bluetooth settings');
    this.helpSteps = h('ol.help-steps',
      step('plug', 'Plug in the sign', 'Check that it lights up.', null, 'plug'),
      step('phone', 'Close the iLedColor app on your phone', 'The sign talks to one device at a time.', null, 'phone'),
      step('bluetooth', 'Turn on Bluetooth', 'On this PC.', btBtn, 'bt'),
      step('wave', 'Stay close', 'Within a few meters of the sign.', null, 'near'),
    );
    const retry = h('button.btn.primary', { type: 'button', onclick: () => this.retry() }, h('span', { html: icon('refresh') }), 'Try again');
    const hide = h('button.btn.ghost.sm', { type: 'button', onclick: () => { this.helpHidden = true; this._render(); } }, 'Hide');
    el.append(
      h('div.help-head',
        h('div.help-titles', this.helpTitle, this.helpLead),
        h('div.help-actions', this.helpLive, retry, hide)),
      this.helpSteps,
      h('p.help-foot', 'No pairing needed. It keeps trying by itself. You can keep drawing and send once it connects.'),
    );
    this.helpMini = $('#connHelpMini');
    this.helpMini.addEventListener('click', () => {
      this.helpHidden = false;
      this._render();
    });
  },

  _renderHelp(state, s) {
    if (PROBLEM.has(state)) {
      if (state !== this.lastProblem) this.helpHidden = false;
      this.lastProblem = state;
    } else if (state === 'connected' || state === 'disconnected' || state === 'offline' || state === 'forbidden') {
      this.lastProblem = null;
    }
    // Keep the panel up while it searches again after a failure, so it does not flicker.
    const problem = this.lastProblem;
    const show = !!problem && !this.helpHidden;
    this.help.hidden = !show;
    this.helpMini.hidden = !problem || !this.helpHidden;
    $('#offlineBanner').hidden = !(state === 'offline' || state === 'forbidden');
    $('#offlineBanner .msg').textContent =
      state === 'forbidden'
        ? 'This page has to be opened from LED Sign Studio.'
        : `The app stopped answering. Close LED Sign Studio and open it again.${autosave.dirty ? ' Your latest changes are not saved yet.' : ' Your work is saved.'}`;
    if (!show) return;
    const titles = {
      not_found: ["Can't find your sign", 'Check these and it will connect by itself.'],
      bluetooth_off: ['Bluetooth is off', 'Turn on Bluetooth on this PC and it will connect by itself.'],
      error: ['Something went wrong with the connection', s?.last_error || s?.message || 'It will keep trying.'],
    };
    const [t, lead] = titles[problem] || titles.error;
    this.helpTitle.textContent = t;
    this.helpLead.textContent = lead;
    this.help.dataset.problem = problem;
    const searching = state === 'searching' || state === 'connecting';
    this.helpLive.textContent = searching ? 'Looking again...' : '';
    this.helpLive.classList.toggle('on', searching);
    for (const li of this.helpSteps.children) li.classList.toggle('focus', problem === 'bluetooth_off' && li.dataset.step === 'bt');
  },
};

// Library tab (save, open, delete, export) and autosave.

import { api } from './api.js';
import { store, clampMs, isBlank } from './state.js';
import { drawDots } from './render.js';
import { h, $, fromB64, fmtAgo, toB64 } from './util.js';
import { icon } from './icons.js';
import { toast, confirmDialog, segmented } from './ui.js';

// ---------------------------------------------------------------- autosave

export const autosave = {
  savedRev: -1,
  saving: false,
  ready: false,
  failures: 0,

  /** Load the autosaved project. Autosave only starts once the old one was read. */
  async restore() {
    for (let attempt = 0; attempt < 12; attempt++) {
      try {
        const data = await api.getAutosave();
        this._apply(data);
        this.ready = true;
        return true;
      } catch (e) {
        if (e.code === 'forbidden') return false;
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    // Never autosave over a project we could not read. Keep trying in the background.
    this.ready = false;
    this._bootRev = store.revision;
    this._retryLater();
    return false;
  },

  _apply(data) {
    if (!(data && Array.isArray(data.frames) && data.frames.length)) return;
    if (!store.load(data, { undoable: false })) return;
    this.savedRev = store.revision;
    const blank = store.frames.length === 1 && isBlank(store.frames[0].pixels);
    if (!blank) toast(`Picked up where you left off: ${store.name}`, { key: 'restore' });
  },

  _retryLater() {
    setTimeout(async () => {
      let data;
      try {
        data = await api.getAutosave();
      } catch {
        this._retryLater();
        return;
      }
      const has = data && Array.isArray(data.frames) && data.frames.length;
      if (has && store.revision === this._bootRev) this._apply(data);
      else if (has) {
        // The user already started something new: keep the old work in the library.
        try {
          await api.librarySave(`Recovered ${new Date().toLocaleString()}`, data);
          toast('Your earlier work was saved to the Library as "Recovered".', { key: 'restore' });
        } catch {
          this._retryLater();
          return;
        }
      }
      this.ready = true;
      this._paint();
      this.save();
    }, 5000);
  },

  init() {
    this.el = $('#saveState');
    store.on('change', () => {
      this._paint();
      clearTimeout(this._soon);
      this._soon = setTimeout(() => this.save(), 1500);
    });
    setInterval(() => this.save(), 4000);
    // The app window calls this before it closes and repeats it while the answer is 'saving'.
    window.ledsignBeforeClose = () => {
      if (!this.ready || this.failures >= 2) return 'skipped';
      this.save();
      return this.saving || this.dirty ? 'saving' : 'saved';
    };
    // Last chance when the window closes.
    window.addEventListener('pagehide', () => this.flush());
    document.addEventListener('visibilitychange', () => document.visibilityState === 'hidden' && this.flush());
    this._paint();
  },

  get dirty() {
    return store.revision !== this.savedRev;
  },

  async save() {
    if (!this.ready || this.saving || !this.dirty) return;
    this.saving = true;
    const rev = store.revision;
    this._paint();
    try {
      await api.autosave(store.toJSON());
      this.savedRev = rev;
      this.failures = 0;
    } catch (e) {
      this.failures++;
      if (this.failures === 3) toast("Can't save your work right now. It will keep trying.", { type: 'error', key: 'autosave' });
    } finally {
      this.saving = false;
      this._paint();
    }
  },

  flush() {
    if (!this.ready || !this.dirty) return;
    try {
      const body = JSON.stringify(store.toJSON());
      // keepalive requests are limited to 64 KB; bigger projects rely on the regular saves.
      if (body.length < 60000) {
        fetch('/api/autosave', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json', 'X-Token': tokenHeader() }, body });
      }
    } catch {}
  },

  _paint() {
    if (!this.el) return;
    let text = 'Saved';
    let state = 'saved';
    if (!this.ready) {
      text = 'Not saving';
      state = 'error';
    } else if (this.failures >= 2) {
      text = "Can't save";
      state = 'error';
    } else if (this.saving || this.dirty) {
      text = 'Saving...';
      state = 'saving';
    }
    this.el.dataset.state = state;
    this.el.textContent = text;
    this.el.title = state === 'saved' ? 'Your work is saved automatically' : state === 'error' ? 'Changes are not being saved right now' : '';
  },
};

function tokenHeader() {
  try {
    return sessionStorage.getItem('ledsign.token') || new URL(location.href).searchParams.get('t') || '';
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------- library tab

export const libraryPanel = {
  items: [],
  scale: '8',

  init() {
    const root = $('#tab-library');
    this.nameIn = h('input.input', { type: 'text', maxlength: 80, 'aria-label': 'Project name', placeholder: 'Name' });
    this.nameIn.value = store.name;
    this.nameIn.addEventListener('change', () => store.rename(this.nameIn.value));
    this.nameIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this.save();
    });
    store.on('change', (e) => {
      if (document.activeElement !== this.nameIn) this.nameIn.value = store.name;
      if (e.kind !== 'select') this._syncInfo();
    });
    this.saveBtn = h('button.btn.primary', { type: 'button', onclick: () => this.save() }, h('span', { html: icon('save') }), 'Save');
    this.info = h('p.muted.small');
    this.list = h('div.lib-list', { role: 'list' });
    this.count = h('span.muted.small');
    const scaleSeg = segmented([['1', 'Actual size'], ['8', 'Big (8x)']], this.scale, (v) => (this.scale = v), { small: true });
    this.exportBtn = h('button.btn', { type: 'button', onclick: () => this.exportGif() }, h('span', { html: icon('download') }), 'Export GIF');

    root.append(
      h('div.section',
        h('h3.section-title', 'This project'),
        h('div.row-tight', this.nameIn, this.saveBtn),
        this.info,
        h('button.btn.sm.ghost', { type: 'button', onclick: () => this.newProject() }, h('span', { html: icon('plus') }), 'New project')),
      h('div.section',
        h('div.section-head', h('h3.section-title', 'Saved'), this.count),
        this.list),
      h('div.section',
        h('h3.section-title', 'Export'),
        h('p.muted.small', 'Save the animation as a GIF file to share.'),
        h('div.row-tight', scaleSeg, this.exportBtn)),
    );
    this._syncInfo();
    this.refresh();
  },

  _syncInfo() {
    const n = store.frames.length;
    this.info.textContent = `${n === 1 ? '1 frame' : `${n} frames`}, ${store.width}x${store.height}. Changes are saved automatically. Save keeps a copy in the list below.`;
  },

  async refresh() {
    try {
      this.items = (await api.library()) || [];
    } catch (e) {
      this.list.replaceChildren(h('p.muted.small', 'Could not load saved projects.'));
      return;
    }
    this.items.sort((a, b) => String(b.updated).localeCompare(String(a.updated)));
    this.count.textContent = this.items.length ? String(this.items.length) : '';
    this.list.replaceChildren();
    if (!this.items.length) {
      this.list.append(h('p.empty', 'Nothing saved yet. Give your project a name and press Save.'));
      return;
    }
    for (const it of this.items) this.list.append(this._row(it));
  },

  _row(it) {
    const canvas = h('canvas.lib-thumb');
    try {
      const px = fromB64(it.thumb);
      if (px.length === it.width * it.height * 3) drawDots(canvas, px, it.width, it.height, 2);
    } catch {}
    const row = h('div.lib-row', { role: 'listitem' },
      h('button.lib-open', { type: 'button', title: `Open ${it.name}`, onclick: () => this.open(it) },
        canvas,
        h('span.lib-text', h('span.lib-name', it.name), h('span.muted.small', `${it.frames === 1 ? '1 frame' : `${it.frames} frames`}, ${fmtAgo(it.updated)}`))),
      h('button.icon-btn.ghost', { type: 'button', title: `Delete ${it.name}`, 'aria-label': `Delete ${it.name}`, html: icon('trash'), onclick: () => this.remove(it) }),
    );
    return row;
  },

  async save() {
    const name = (this.nameIn.value || '').trim() || store.name || 'Untitled';
    store.rename(name);
    const existing = this.items.find((i) => i.name.toLowerCase() === name.toLowerCase());
    if (existing && !(await confirmDialog('Replace it?', `"${existing.name}" is already saved. Replace it with this project?`, 'Replace'))) return;
    this.saveBtn.disabled = true;
    try {
      await api.librarySave(name, store.toJSON());
      toast(`Saved "${name}"`, { type: 'ok', key: 'lib' });
      this.refresh();
    } catch (e) {
      toast(`Could not save. ${e.message}`, { type: 'error', key: 'lib' });
    } finally {
      this.saveBtn.disabled = false;
    }
  },

  async open(it) {
    try {
      const p = await api.libraryLoad(it.slug);
      if (!store.load(p, { label: 'Open project' })) throw new Error('That project looks damaged.');
      toast(`Opened "${store.name}"`, { type: 'ok', key: 'lib', action: { label: 'Undo', fn: () => store.undo() } });
    } catch (e) {
      toast(`Could not open it. ${e.message}`, { type: 'error', key: 'lib' });
    }
  },

  async remove(it) {
    if (!(await confirmDialog('Delete it?', `Delete "${it.name}" from your saved projects? This can't be undone.`, 'Delete', { danger: true }))) return;
    try {
      await api.libraryDelete(it.slug);
      toast(`Deleted "${it.name}"`, { key: 'lib' });
    } catch (e) {
      toast(`Could not delete it. ${e.message}`, { type: 'error', key: 'lib' });
    }
    this.refresh();
  },

  newProject() {
    store.load(
      { version: 1, name: 'Untitled', width: store.width, height: store.height, frames: [{ pixels: toB64(store.blankPixels()), ms: 100 }], current: 0 },
      { label: 'New project' },
    );
    toast('Started a new project. Your old one is still in Undo.', { key: 'lib', action: { label: 'Undo', fn: () => store.undo() } });
  },

  async exportGif() {
    this.exportBtn.disabled = true;
    try {
      const frames = store.frames;
      const r = await api.exportGif(frames.map((f) => toB64(f.pixels)), frames.map((f) => clampMs(f.ms)), Number(this.scale));
      const bytes = fromB64(r.gif);
      const blob = new Blob([bytes], { type: 'image/gif' });
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: `${safeName(store.name)}${this.scale === '8' ? '-big' : ''}.gif` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      toast('GIF exported.', { type: 'ok', key: 'lib' });
    } catch (e) {
      toast(`Could not export. ${e.message}`, { type: 'error', key: 'lib' });
    } finally {
      this.exportBtn.disabled = false;
    }
  },
};

function safeName(s) {
  return (String(s || 'led-sign').replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-') || 'led-sign').slice(0, 60);
}

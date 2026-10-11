// Import tab: GIF, PNG, JPG, WebP, BMP files turned into frames by the server.

import { api } from './api.js';
import { store } from './state.js';
import { OutputBox } from './output.js';
import { h, $, fromB64, clamp, fmtBytes } from './util.js';
import { icon } from './icons.js';
import { segmented, toast } from './ui.js';

const ACCEPT = '.gif,.png,.jpg,.jpeg,.webp,.bmp,image/gif,image/png,image/jpeg,image/webp,image/bmp';
const MAX_BYTES = 40 * 1024 * 1024;

export const importPanel = {
  file: null, // {name, data (base64), size}
  fit: 'contain',
  maxFrames: 300,

  init() {
    const root = $('#tab-import');
    this.out = new OutputBox({ label: 'Import' });
    this.out.el.hidden = true;
    this.picker = h('input', { type: 'file', accept: ACCEPT, hidden: true });
    this.picker.addEventListener('change', () => {
      const f = this.picker.files && this.picker.files[0];
      if (f) this.load(f);
      this.picker.value = '';
    });
    this.drop = h('div.dropzone', { tabindex: 0, role: 'button', 'aria-label': 'Choose a picture or GIF' },
      h('span.drop-ic', { html: icon('upload') }),
      h('strong', 'Drop a picture or GIF here'),
      h('span.muted', 'GIF, PNG, JPG or WebP'),
      h('span.btn.sm', 'Choose file'));
    this.drop.addEventListener('click', () => this.picker.click());
    this.drop.addEventListener('keydown', (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), this.picker.click()));
    this.drop.addEventListener('dragover', (e) => {
      e.preventDefault();
      this.drop.classList.add('over');
    });
    this.drop.addEventListener('dragleave', () => this.drop.classList.remove('over'));
    this.drop.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      this.drop.classList.remove('over');
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) this.load(f);
    });

    this.fitSeg = segmented([['contain', 'Fit', 'Show the whole picture'], ['cover', 'Fill', 'Fill the sign, cropping the edges'], ['stretch', 'Stretch']], this.fit, (v) => {
      this.fit = v;
      this.convert();
    }, { small: true });
    this.maxIn = h('input.input.num', { type: 'number', min: 1, max: 1000, value: this.maxFrames, 'aria-label': 'Most frames to keep' });
    this.maxIn.addEventListener('change', () => {
      this.maxFrames = clamp(Number(this.maxIn.value) || 300, 1, 1000);
      this.maxIn.value = this.maxFrames;
      this.convert();
    });
    this.fileInfo = h('div.file-info', { hidden: true });
    this.status = h('p.muted.small', { hidden: true });

    root.append(
      h('div.section', h('h3.section-title', 'Import a picture or GIF'), this.drop, this.picker, this.fileInfo),
      h('div.section',
        h('div.grid2',
          h('div.field', h('span.field-label', 'Fit'), this.fitSeg),
          h('label.field', h('span.field-label', 'Most frames'), this.maxIn))),
      h('div.section.out-section', this.status, this.out.el),
    );
    this.out.sendBtn.querySelector('span:last-child').textContent = 'Send now';
  },

  async load(file) {
    if (file.size > MAX_BYTES) {
      toast('That file is too big. Try one under 40 MB.', { type: 'error' });
      return;
    }
    const data = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(',')[1] || '');
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    }).catch(() => null);
    if (data == null) {
      toast('Could not read that file.', { type: 'error' });
      return;
    }
    this.file = { name: file.name, data, size: file.size };
    this.fileInfo.hidden = false;
    this.fileInfo.replaceChildren(h('span', { html: icon('image') }), h('span.file-name', file.name), h('span.muted', fmtBytes(file.size)));
    this.convert();
  },

  async convert() {
    if (!this.file) return;
    const seq = (this._seq = (this._seq || 0) + 1);
    this.status.hidden = false;
    this.status.textContent = 'Converting...';
    this.drop.classList.add('busy');
    try {
      const r = await api.importFile(this.file.name, this.file.data, this.fit, this.maxFrames);
      if (seq !== this._seq) return;
      const W = store.width;
      const H = store.height;
      let frames = (r.frames || []).map((b) => fromB64(b));
      if (r.width !== W || r.height !== H) frames = frames.map((f) => store.fit(f, W, H, r.width, r.height));
      if (!frames.length) throw new Error('No frames in that file.');
      const n = frames.length;
      const src = r.source_frames ?? n;
      this.status.textContent = src > n ? `Kept ${n} of ${src} frames. Raise "Most frames" to keep more.` : '';
      this.status.hidden = !(src > n);
      this.out.el.hidden = false;
      this.out.label = this.file.name;
      this.out.set(frames, r.durations && r.durations.length ? r.durations : frames.map(() => 100));
    } catch (e) {
      if (seq !== this._seq) return;
      this.status.textContent = '';
      this.status.hidden = true;
      // Server messages already say what is wrong ("Could not read that file. Use a GIF, ...").
      toast(e.message || 'Could not open that file.', { type: 'error', key: 'import' });
    } finally {
      if (seq === this._seq) this.drop.classList.remove('busy');
    }
  },
};

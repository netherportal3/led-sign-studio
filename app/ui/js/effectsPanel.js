// Effects tab: gallery with live mini previews, effect settings, and "Animate my drawing".

import { store } from './state.js';
import { EFFECTS, defaults, generate, DRAWING_ANIMS, animateDrawing } from './effects.js';
import { OutputBox } from './output.js';
import { drawDots } from './render.js';
import { Player, visibleIn } from './anim.js';
import { colorButton } from './colors.js';
import { h, $, debounce } from './util.js';
import { icon } from './icons.js';
import { segmented, slider } from './ui.js';

const params = {};
for (const id of Object.keys(EFFECTS)) params[id] = defaults(id);

export const effectsPanel = {
  selected: 'rainbow',
  cards: {},

  init() {
    const root = $('#tab-effects');
    this.root = root;
    this.out = new OutputBox({ label: EFFECTS[this.selected].name });
    this.titleEl = h('h3.section-title');
    this.shuffleBtn = h('button.btn.sm.ghost', { type: 'button', title: 'Try a different random pattern', onclick: () => this.shuffle() },
      h('span', { html: icon('shuffle') }), 'Shuffle');
    this.paramsEl = h('div.params');
    this.gallery = h('div.gallery', { role: 'list' });
    for (const [id, fx] of Object.entries(EFFECTS)) this.gallery.append(this._card(id, fx));

    // Animate my drawing
    this.anim = { kind: 'left', speed: 6 };
    this.animOut = new OutputBox({ label: 'Animated drawing' });
    const kinds = Object.entries(DRAWING_ANIMS).map(([k, v]) => [k, v.name]);
    this.animSeg = segmented(kinds, this.anim.kind, (v) => { this.anim.kind = v; this.rebuildAnim(); }, { small: true });
    this.animSeg.classList.add('wrap');
    const animSpeed = slider({ label: 'Speed', min: 1, max: 10, value: this.anim.speed, onInput: (v) => { this.anim.speed = v; this.rebuildAnimSoon(); } });
    this.animNote = h('p.muted.small');

    root.append(
      h('div.section', h('div.section-head', this.titleEl, this.shuffleBtn), this.out.el, this.paramsEl),
      h('div.section', h('h3.section-title', 'All effects'), this.gallery),
      h('div.section#animate-drawing',
        h('h3.section-title', 'Animate my drawing'),
        h('p.muted.small', 'Uses the frame you have selected in the editor.'),
        this.animNote, this.animOut.el, this.animSeg, animSpeed),
    );
    this.select(this.selected, false);
    store.on('change', (e) => {
      if (e.kind === 'rename') return;
      this.rebuildAnimSoon();
      if (this.out.W !== store.width || this.out.H !== store.height) {
        this.rebuild();
        for (const id of Object.keys(EFFECTS)) this._renderCard(id);
      }
    });
    this.rebuildAnim();
  },

  _card(id, fx) {
    const canvas = h('canvas.card-canvas');
    const card = h('button.card', { type: 'button', role: 'listitem', 'data-id': id, onclick: () => this.select(id, true) },
      h('div.card-screen', canvas), h('span.card-name', fx.name));
    const entry = { card, canvas, frames: [], player: null };
    entry.player = new Player((i) => {
      const f = entry.frames[i];
      if (f) drawDots(canvas, f, store.width, store.height, 3);
    }, { isVisible: visibleIn(canvas) });
    this.cards[id] = entry;
    // Build previews a little later so the first paint is quick.
    setTimeout(() => this._renderCard(id), 30);
    return card;
  },

  _renderCard(id) {
    const entry = this.cards[id];
    const g = generate(id, store.width, store.height, params[id]);
    entry.frames = g.frames;
    entry.player.set(g.frames.length, g.durations);
    if (!entry.player.running) entry.player.start(0);
  },

  select(id, scroll) {
    this.selected = id;
    for (const [k, e] of Object.entries(this.cards)) e.card.setAttribute('aria-pressed', String(k === id));
    this.titleEl.textContent = EFFECTS[id].name;
    this.shuffleBtn.hidden = !EFFECTS[id].seeded;
    this._buildParams();
    this.rebuild();
    if (scroll) this.root.scrollTo({ top: 0, behavior: 'smooth' });
  },

  _buildParams() {
    const id = this.selected;
    const p = params[id];
    const fx = EFFECTS[id];
    this.paramsEl.replaceChildren();
    const fields = [];
    for (const d of fx.params) {
      let el;
      if (d.type === 'range') {
        el = slider({
          label: d.label,
          min: d.min,
          max: d.max,
          value: p[d.key],
          fmt: (v) => (d.unit ? `${v}${d.unit}` : v),
          onInput: (v) => {
            p[d.key] = v;
            this.rebuildSoon();
          },
        });
      } else if (d.type === 'select') {
        const seg = segmented(d.options, p[d.key], (v) => {
          p[d.key] = v;
          this._syncShow();
          this.rebuild();
        }, { small: true });
        if (d.options.length > 3) seg.classList.add('wrap');
        el = h('div.field', h('span.field-label', d.label), seg);
      } else if (d.type === 'color') {
        el = h('div.field', h('span.field-label', d.label), colorButton(p[d.key], (c) => {
          p[d.key] = c;
          this.rebuildSoon();
        }, { label: d.label }));
      }
      el.dataset.key = d.key;
      fields.push([d, el]);
      this.paramsEl.append(el);
    }
    this._fields = fields;
    this._syncShow();
  },

  _syncShow() {
    const p = params[this.selected];
    for (const [d, el] of this._fields) el.hidden = d.show ? !d.show(p) : false;
  },

  shuffle() {
    params[this.selected].seed = Math.floor(Math.random() * 1e9);
    this.rebuild();
  },

  rebuildSoon: debounce(function () {
    effectsPanel.rebuild();
  }, 60),

  rebuild() {
    const id = this.selected;
    const g = generate(id, store.width, store.height, params[id]);
    this.out.label = EFFECTS[id].name;
    this.out.set(g.frames, g.durations);
    // Keep the gallery card in step with the chosen settings.
    const entry = this.cards[id];
    entry.frames = g.frames;
    entry.player.set(g.frames.length, g.durations);
  },

  rebuildAnimSoon: debounce(function () {
    effectsPanel.rebuildAnim();
  }, 250),

  rebuildAnim() {
    const src = store.frame.pixels;
    let lit = false;
    for (let i = 0; i < src.length; i++) {
      if (src[i]) {
        lit = true;
        break;
      }
    }
    this.animNote.textContent = lit ? '' : 'The selected frame is empty. Draw something first.';
    this.animNote.hidden = lit;
    const g = animateDrawing(this.anim.kind, src, store.width, store.height, this.anim.speed);
    this.animOut.label = `${DRAWING_ANIMS[this.anim.kind].name} drawing`;
    this.animOut.set(g.frames, g.durations);
  },
};

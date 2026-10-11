// Entry point: wires the modules together.

import { hasToken } from './api.js';
import { store } from './state.js';
import { hydrateIcons } from './icons.js';
import { colors } from './colors.js';
import { editor } from './editor.js';
import { timeline } from './timeline.js';
import { conn } from './connection.js';
import { sender } from './send.js';
import { textPanel } from './text.js';
import { effectsPanel } from './effectsPanel.js';
import { importPanel } from './import.js';
import { autosave, libraryPanel } from './library.js';
import { ambientPanel } from './ambient.js';
import { initShortcuts, showHelp } from './shortcuts.js';
import { $, $$ } from './util.js';

const TABS = ['text', 'effects', 'import', 'library', 'ambient'];

function initTabs() {
  const buttons = $$('.tabs [data-tab]');
  const show = (id, focus = false) => {
    for (const b of buttons) {
      const on = b.dataset.tab === id;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
    for (const t of TABS) $(`#tab-${t}`).hidden = t !== id;
    try {
      localStorage.setItem('ledsign.tab', id);
    } catch {}
    ambientPanel.onTab(id);
  };
  for (const b of buttons) {
    b.addEventListener('click', () => show(b.dataset.tab));
    b.addEventListener('keydown', (e) => {
      const i = TABS.indexOf(b.dataset.tab);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        e.stopPropagation();
        show(TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length], true);
      }
    });
  }
  let first = 'text';
  try {
    const saved = localStorage.getItem('ledsign.tab');
    if (TABS.includes(saved)) first = saved;
  } catch {}
  show(first);
  return show;
}

function initFileDrop(showTab) {
  let depth = 0;
  const veil = $('#dropVeil');
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    depth++;
    veil.hidden = false;
  });
  window.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) veil.hidden = true;
  });
  window.addEventListener('dragover', (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    veil.hidden = true;
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) {
      showTab('import');
      importPanel.load(f);
    }
  });
}

function initProjectName(showTab) {
  const btn = $('#projectName');
  const sync = () => {
    btn.textContent = store.name;
    document.title = store.name && store.name !== 'Untitled' ? `${store.name} - LED Sign Studio` : 'LED Sign Studio';
  };
  btn.addEventListener('click', () => {
    showTab('library');
    const input = $('#tab-library input[type=text]');
    input?.focus();
    input?.select();
  });
  store.on('change', sync);
  sync();
}

async function boot() {
  hydrateIcons();
  colors.load();
  if (!hasToken()) {
    $('#offlineBanner').hidden = false;
    $('#offlineBanner .msg').textContent = 'This page has to be opened from LED Sign Studio.';
  }
  // Restore the last project before anything can change it.
  await autosave.restore();

  editor.init();
  timeline.init();
  conn.init();
  sender.init();
  const showTab = initTabs();
  textPanel.init();
  effectsPanel.init();
  importPanel.init();
  libraryPanel.init();
  ambientPanel.init(() => showTab('ambient'));
  autosave.init();
  initFileDrop(showTab);
  initProjectName(showTab);
  $('#helpBtn').addEventListener('click', showHelp);
  initShortcuts({
    save: () => {
      showTab('library');
      libraryPanel.save();
    },
  });
  hydrateIcons();
  document.body.classList.add('ready');
}

boot();

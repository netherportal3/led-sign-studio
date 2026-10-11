// Inline SVG icons (24x24, stroke based). Use <span data-icon="name"></span> or icon('name').

const P = {
  pencil: '<path d="M4 20h4L19.2 8.8a2.6 2.6 0 0 0-3.7-3.7L4.3 16.3 4 20z"/><path d="M13.6 7l3.7 3.7"/>',
  eraser:
    '<path d="M8.5 20H20"/><path d="M4.6 15.4l9.8-9.8a2 2 0 0 1 2.8 0l2.4 2.4a2 2 0 0 1 0 2.8L11 19.4a2 2 0 0 1-1.4.6H8.3a2 2 0 0 1-1.4-.6l-2.3-2.3a1.4 1.4 0 0 1 0-1.7z"/><path d="M9 11l5.5 5.5"/>',
  fill:
    '<path d="M11 3.5l8 8-7.3 7.3a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L11 3.5z"/><path d="M4.5 12h14"/><path d="M20.5 15.5s1.8 2 1.8 3.2a1.8 1.8 0 0 1-3.6 0c0-1.2 1.8-3.2 1.8-3.2z"/>',
  line: '<path d="M5.5 18.5l13-13"/><circle cx="5" cy="19" r="1.6"/><circle cx="19" cy="5" r="1.6"/>',
  rect: '<rect x="4" y="6" width="16" height="12" rx="1.5"/>',
  ellipse: '<ellipse cx="12" cy="12" rx="8.5" ry="6.5"/>',
  picker:
    '<path d="M19.4 4.6a2.2 2.2 0 0 0-3.1 0l-2.6 2.6-1.1-1.1-1.5 1.5 6.3 6.3 1.5-1.5-1.1-1.1 2.6-2.6a2.2 2.2 0 0 0 0-3.1z"/><path d="M12.6 8.8L5 16.4V19h2.6l7.6-7.6"/>',
  rainbow:
    '<path d="M3 18a9 9 0 0 1 18 0" stroke="#ff4d6d"/><path d="M6.3 18a5.7 5.7 0 0 1 11.4 0" stroke="#ffd23d"/><path d="M9.6 18a2.4 2.4 0 0 1 4.8 0" stroke="#3de8ff"/>',
  mirror: '<path d="M12 3v18" stroke-dasharray="2 2.4"/><path d="M9 7.5L4 12l5 4.5z"/><path d="M15 7.5l5 4.5-5 4.5z"/>',
  onion: '<rect x="3" y="8" width="11" height="11" rx="2" opacity=".45"/><rect x="10" y="5" width="11" height="11" rx="2"/>',
  flipH: '<path d="M12 3v18"/><path d="M8.5 7L3.5 17h5z"/><path d="M15.5 7l5 10h-5z" opacity=".5"/>',
  flipV: '<path d="M3 12h18"/><path d="M7 8.5L17 3.5v5z"/><path d="M7 15.5l10 5v-5z" opacity=".5"/>',
  clear: '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M9 9l6 6M15 9l-6 6"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  redo: '<path d="M15 14l5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
  play: '<path d="M7.5 4.8v14.4a.8.8 0 0 0 1.2.7l11.3-7.2a.8.8 0 0 0 0-1.4L8.7 4.1a.8.8 0 0 0-1.2.7z" fill="currentColor"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4.5h6V7"/>',
  left: '<path d="M15 6l-6 6 6 6"/>',
  right: '<path d="M9 6l6 6-6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  send: '<path d="M21 3L10.5 13.5"/><path d="M21 3l-6.5 18-4-7.5-7.5-4L21 3z"/>',
  bluetooth: '<path d="M7 7l10 10-5 4.5V2.5l5 4.5L7 17"/>',
  power: '<path d="M12 3v8"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6L6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>',
  sunDim: '<circle cx="12" cy="12" r="3.5"/><path d="M12 5v.5M12 18.5v.5M5 12h.5M18.5 12h.5M7 7l.4.4M16.6 16.6l.4.4M7 17l.4-.4M16.6 7.4L17 7"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.3a2.6 2.6 0 0 1 5 .9c0 1.8-2.5 2.3-2.5 3.8"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
  upload: '<path d="M12 15V4"/><path d="M7.5 8.5L12 4l4.5 4.5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  download: '<path d="M12 4v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  save: '<path d="M5 4h11l3 3v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4z"/><path d="M8 4v5h7V4"/><rect x="8" y="13" width="8" height="7"/>',
  folder: '<path d="M3.5 6.5a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-11z"/>',
  text: '<path d="M5 6V4.5h14V6"/><path d="M12 4.5v15"/><path d="M9 19.5h6"/>',
  sparkles:
    '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M18.5 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/><path d="M5.5 15.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="M20.5 15.5l-5-5L5 19.5"/>',
  library: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20v-4h-4"/>',
  unlink: '<path d="M9.5 14.5l-1.8 1.8a3.2 3.2 0 0 1-4.5-4.5l2.5-2.5"/><path d="M14.5 9.5l1.8-1.8a3.2 3.2 0 0 1 4.5 4.5l-2.5 2.5"/><path d="M4 4l16 16"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  shuffle: '<path d="M4 7h3.5a4 4 0 0 1 3.2 1.6l2.6 3.8a4 4 0 0 0 3.2 1.6H20"/><path d="M17 11l3 3-3 3"/><path d="M4 17h3.5a4 4 0 0 0 3.2-1.6"/><path d="M13.3 8.6A4 4 0 0 1 16.5 7H20"/><path d="M17 4l3 3-3 3"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M11 18.5h2"/>',
  plug: '<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0V8z"/><path d="M12 16.5V21"/>',
  wave: '<path d="M2 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7.5 14h9"/>',
  slides: '<rect x="3" y="5" width="13" height="10" rx="1.5"/><path d="M19 8v9a1.5 1.5 0 0 1-1.5 1.5H7"/>',
  ambient:
    '<path d="M9 3v1.6M3.2 9H4.8M4.9 4.9L6 6M13.1 4.9L12 6"/><path d="M5.8 11.4A3.4 3.4 0 0 1 12 8.2"/><path d="M8.5 20h9.2a3.6 3.6 0 0 0 .4-7.2 5.1 5.1 0 0 0-9.8 1.3A2.95 2.95 0 0 0 8.5 20z"/>',
  pause: '<rect x="6.5" y="5" width="3.6" height="14" rx="1"/><rect x="13.9" y="5" width="3.6" height="14" rx="1"/>',
  pin: '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
};

export function icon(name, cls = '') {
  const body = P[name] || '';
  return `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

export function hydrateIcons(root = document) {
  for (const el of root.querySelectorAll('[data-icon]')) {
    if (el.firstElementChild && el.firstElementChild.tagName === 'svg') continue;
    el.innerHTML = icon(el.dataset.icon);
  }
}

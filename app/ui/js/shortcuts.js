// Keyboard shortcuts and the "?" help overlay.

import { editor } from './editor.js';
import { timeline } from './timeline.js';
import { sender } from './send.js';
import { libraryPanel } from './library.js';
import { TOOL_INFO } from './tools.js';
import { h, isTyping } from './util.js';
import { modal, closeModal, isModalOpen, closePopover, isPopoverOpen } from './ui.js';

const GROUPS = [
  ['Tools', [
    ...Object.values(TOOL_INFO).map((t) => [t.key, t.label]),
    ['Right mouse', 'Erase with any tool'],
    ['Alt + click', 'Pick a color'],
    ['M', 'Mirror drawing'],
    ['K', 'Onion skin'],
  ]],
  ['Edit', [
    ['Ctrl + Z', 'Undo'],
    ['Ctrl + Y | Ctrl + Shift + Z', 'Redo'],
    ['Arrow keys', 'Shift the drawing (wraps around)'],
    ['H', 'Flip left to right'],
    ['V', 'Flip upside down'],
    ['Delete', 'Clear this frame'],
  ]],
  ['Frames', [
    ['Space', 'Play or stop'],
    [', | .', 'Previous or next frame'],
    ['N', 'Add a blank frame'],
    ['Ctrl + D', 'Duplicate frame'],
    ['Ctrl + Delete', 'Delete frame'],
    ['Alt + Left / Right', 'Move frame'],
  ]],
  ['Sign and project', [
    ['Ctrl + Enter', 'Send to sign'],
    ['Ctrl + S', 'Save to the library'],
    ['?', 'Show these shortcuts'],
    ['Esc', 'Close a window'],
  ]],
];

export function showHelp() {
  const body = h('div.keys');
  for (const [title, rows] of GROUPS) {
    const g = h('div.keys-group', h('h3', title));
    for (const [k, label] of rows) {
      // ' + ' joins keys pressed together, ' | ' separates alternatives.
      const alts = k.split(' | ').map((alt) => alt.split(' + ').flatMap((part, i) => (i ? [' + ', h('kbd', part)] : [h('kbd', part)])));
      g.append(h('div.keys-row', h('span.keys-k', ...alts.flatMap((a, i) => (i ? [' or ', ...a] : a))), h('span', label)));
    }
    body.append(g);
  }
  modal({ title: 'Keyboard shortcuts', body, wide: true });
}

export function initShortcuts(actions) {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented) return;
    const key = e.key;
    const ctrl = e.ctrlKey || e.metaKey;
    if (key === 'Escape') {
      if (closePopover() || closeModal()) e.preventDefault();
      return;
    }
    if (isModalOpen() || isPopoverOpen()) return;
    if (isTyping(e.target)) {
      if (ctrl && key.toLowerCase() === 's') {
        e.preventDefault();
        libraryPanel.save();
      }
      return;
    }
    const k = key.length === 1 ? key.toLowerCase() : key;
    let handled = true;
    if (ctrl) {
      if (k === 'z' && !e.shiftKey) editor.undo();
      else if (k === 'y' || (k === 'z' && e.shiftKey)) editor.redo();
      else if (k === 'd') timeline.duplicate();
      else if (k === 's') actions.save();
      else if (k === 'Enter') sender.sendProject();
      else if (k === 'Delete' || k === 'Backspace') timeline.remove();
      else handled = false;
    } else if (e.altKey) {
      if (k === 'ArrowLeft') timeline.move(-1);
      else if (k === 'ArrowRight') timeline.move(1);
      else handled = false;
    } else {
      const step = e.shiftKey ? 4 : 1;
      const tool = Object.entries(TOOL_INFO).find(([, t]) => t.key.toLowerCase() === k && !e.shiftKey);
      if (tool) editor.setTool(tool[0]);
      else if (k === 'ArrowLeft') editor.shift(-step, 0);
      else if (k === 'ArrowRight') editor.shift(step, 0);
      else if (k === 'ArrowUp') editor.shift(0, -step);
      else if (k === 'ArrowDown') editor.shift(0, step);
      else if (k === 'm') editor.toggleMirror();
      else if (k === 'k') editor.toggleOnion();
      else if (k === 'h') editor.flipH();
      else if (k === 'v') editor.flipV();
      else if (k === 'Delete' || k === 'Backspace') editor.clear();
      else if (k === ' ') timeline.toggle();
      else if (k === ',' || k === '<') timeline.prev();
      else if (k === '.' || k === '>') timeline.next();
      else if (k === 'n') timeline.add();
      else if (k === '?' || (k === '/' && e.shiftKey)) showHelp();
      else handled = false;
    }
    if (handled) {
      e.preventDefault();
      // Keep focus off buttons so Space does not click the last one pressed.
      if (document.activeElement && document.activeElement.tagName === 'BUTTON') document.activeElement.blur();
    }
  });
}

// Runs the capture page script against a tiny fake DOM and prints what happened as JSON.
// usage: node capture_harness.js <script.js> <scenario>
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function el(id) {
  return {
    id, listeners: {}, value: '', parent: null, disabled: false, textContent: '',
    classList: {add() {}, remove() {}},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    append() {}, focus() {},
  };
}
const els = {box: el('box'), files: el('files'), msg: el('msg'), go: el('go'), depth: el('depth')};
els.depth.value = 'standard';
const win = el('window');
els.box.parent = win; // events on the box bubble to the window, like in a browser
const doc = el('document');
doc.getElementById = id => els[id];
doc.createElement = () => el('x');
doc.body = el('body');

class FakeFile {
  constructor(parts, name, opts) { this.name = name; this.type = (opts && opts.type) || ''; }
}
class FakeForm {
  constructor() { this.entries = []; }
  append(k, v, name) { this.entries.push([k, name || v]); }
}
const calls = [];
const fakeFetch = async (url, init) => {
  calls.push({url, entries: init.body.entries});
  return {ok: true, json: async () => ({id: 'r'})};
};
let closed = 0;
win.close = () => { closed += 1; };

new Function('document', 'window', 'File', 'FormData', 'fetch', 'setTimeout', src)(
  doc, win, FakeFile, FakeForm, fakeFetch, () => 0);

function fire(target, type, ev) {
  ev.type = type;
  ev.defaultPrevented = false;
  ev.preventDefault = () => { ev.defaultPrevented = true; };
  ev.stopPropagation = () => { ev.stopped = true; };
  for (const fn of target.listeners[type] || []) fn(ev);
  if (!ev.stopped && target.parent) fire(target.parent, type, ev);
  return ev;
}
const dt = (files, data) => ({files, getData: t => data[t] || ''});
const png = new FakeFile([], 'a.png', {type: 'image/png'});

(async () => {
  const out = {};
  if (scenario === 'drop-file-once') {
    out.drop = fire(els.box, 'drop', {dataTransfer: dt([png], {})}).defaultPrevented;
    els.go.onclick();
    await new Promise(r => setImmediate(r));
    out.attachments = calls[0].entries.filter(e => e[0] === 'attachments').map(e => e[1]);
  } else if (scenario === 'drop-link') {
    const ev = fire(win, 'drop', {dataTransfer: dt([], {'text/uri-list': '# c\nhttps://x.test/a'})});
    out.prevented = ev.defaultPrevented;
    out.value = els.box.value;
  } else if (scenario === 'drop-nothing') {
    out.prevented = fire(win, 'drop', {dataTransfer: dt([], {})}).defaultPrevented;
  } else if (scenario === 'double-submit') {
    els.box.value = 'topic';
    const key = {key: 'Enter', ctrlKey: true};
    fire(doc, 'keydown', {...key});
    fire(doc, 'keydown', {...key});
    await new Promise(r => setImmediate(r));
    out.fetches = calls.length;
  }
  console.log(JSON.stringify(out));
})();

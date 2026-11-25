"""The quick-capture popup: one static page, shown by the tray in a minimal browser window.

One input takes typed text, pasted text and links, pasted images and dropped files. It posts to
the same submit endpoint every other client uses and closes the window once the Run is accepted.
Nothing from any Run is put into this page.
"""

from __future__ import annotations

from investigate.webpages import _CSS

_CSS_EXTRA = """\
body{margin:0;padding:.75rem;height:100vh;box-sizing:border-box;display:flex;\
flex-direction:column;gap:.5rem}
#box{flex:1;min-height:4rem;resize:none;padding:.5rem;border:2px solid var(--line);\
border-radius:.4rem;background:var(--card);color:var(--fg);font:inherit}
#box.over{border-color:var(--link)}
#files{margin:0;padding:0;list-style:none;font-size:.85rem;color:var(--muted);\
max-height:5rem;overflow:auto}
#files button{border:0;background:none;color:var(--link);cursor:pointer;padding:0 .3rem}
.row{display:flex;gap:.5rem;align-items:center}
.row .grow{flex:1;color:var(--muted);font-size:.85rem}
#msg{color:#b3261e;font-size:.85rem;min-height:1.2em}
"""

_JS = """\
const box = document.getElementById('box'), list = document.getElementById('files');
const msg = document.getElementById('msg'), go = document.getElementById('go');
const picked = [];
function show() {
  list.textContent = '';
  picked.forEach((f, i) => {
    const li = document.createElement('li');
    li.append(f.name + ' ');
    const x = document.createElement('button');
    x.textContent = 'remove'; x.type = 'button';
    x.onclick = () => { picked.splice(i, 1); show(); };
    li.append(x); list.append(li);
  });
}
function add(files) {
  for (const f of files) {
    const generic = !f.name || f.name === 'image.png';
    const name = generic
      ? 'pasted-image-' + (picked.length + 1) + '.' + (f.type.split('/')[1] || 'png')
      : f.name;
    picked.push(new File([f], name, {type: f.type}));
  }
  show();
}
box.addEventListener('paste', e => {
  const files = [...(e.clipboardData ? e.clipboardData.files : [])];
  if (files.length) { e.preventDefault(); add(files); }
});
for (const t of [window, box]) {
  t.addEventListener('dragover', e => { e.preventDefault(); box.classList.add('over'); });
  t.addEventListener('dragleave', () => box.classList.remove('over'));
  t.addEventListener('drop', e => {
    e.preventDefault(); box.classList.remove('over');
    if (e.dataTransfer && e.dataTransfer.files.length) add([...e.dataTransfer.files]);
  });
}
async function submit() {
  const form = new FormData();
  form.append('text', box.value);
  form.append('depth', document.getElementById('depth').value);
  picked.forEach(f => form.append('attachments', f, f.name));
  go.disabled = true; msg.textContent = '';
  try {
    const resp = await fetch('/api/runs', {method: 'POST', body: form});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || 'The server said ' + resp.status);
    window.close();
    setTimeout(() => {
      document.body.textContent = 'Submitted. You can close this window.';
    }, 300);
  } catch (err) { msg.textContent = err.message; go.disabled = false; }
}
go.onclick = submit;
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') window.close();
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit();
});
box.focus();
"""


def capture_page() -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Investigate</title><style>{_CSS}{_CSS_EXTRA}</style></head><body>"
        '<textarea id="box" placeholder="Type, paste text, links or images, or drop files here"'
        " autofocus></textarea>"
        '<ul id="files"></ul><div id="msg"></div>'
        '<div class="row"><label>Depth <select id="depth">'
        '<option value="quick">Quick</option>'
        '<option value="standard" selected>Standard</option>'
        '<option value="deep">Deep</option></select></label>'
        '<span class="grow">Ctrl+Enter to submit</span>'
        '<button id="go" type="button">Submit</button></div>'
        f"<script>{_JS}</script></body></html>"
    )

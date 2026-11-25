"""The Settings page: a plain HTML form, no script. Every value shown is escaped.

The form posts to `/settings` as urlencoded fields named by `settings.Field.key`. The page's CSS is
the only inline code, allowed by hash in `settings_csp`.
"""

from __future__ import annotations

import base64
import hashlib
from html import escape

from investigate.settings import (
    CONTEXT_BLANK_ROWS,
    CONTEXTS_KEY,
    SECTIONS,
    Field,
    FormState,
)
from investigate.webpages import _CSS

_CSS_EXTRA = """\
form.settings{max-width:44rem}
fieldset{border:1px solid var(--line);border-radius:.4rem;margin:0 0 1.2rem;padding:.6rem 1rem}
legend{font-weight:600;padding:0 .4rem}
.intro{color:var(--muted);font-size:.85rem;margin:0 0 .6rem}
.field{display:grid;grid-template-columns:14rem 1fr;gap:.2rem .8rem;align-items:center;\
margin:.5rem 0}
.field label{font-weight:500}
.field .help,.field .err{grid-column:2;font-size:.82rem;margin:0}
.help{color:var(--muted)}
.err{color:#b3261e}
.field.bad input,.field.bad select{border-color:#b3261e}
.ctx{display:grid;grid-template-columns:10rem 1fr;gap:.4rem;margin:.3rem 0}
.banner{padding:.6rem 1rem;border-radius:.4rem;margin:0 0 1rem;background:var(--card)}
.banner.bad{border:1px solid #b3261e;color:#b3261e}
.banner.ok{border:1px solid var(--line)}
@media(max-width:36rem){.field{grid-template-columns:1fr}.field .help,.field .err{grid-column:1}}
"""


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def settings_csp() -> str:
    """No script at all; this page's style by hash; the form posts to ourselves; no framing."""
    return (
        f"default-src 'none'; style-src {_hash(_CSS + _CSS_EXTRA)}; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )


def _control(f: Field, state: FormState, error: str | None) -> str:
    value = state.values.get(f.key, "")
    fid = "f-" + f.key.replace(".", "-")
    name = escape(f.key, quote=True)
    if f.kind == "bool":
        checked = " checked" if value else ""
        control = f'<input type="checkbox" id="{fid}" name="{name}"{checked}>'
    elif f.kind == "choice":
        options = "".join(
            f'<option value="{escape(v, quote=True)}"{" selected" if v == value else ""}>'
            f"{escape(label)}</option>"
            for v, label in f.choices
        )
        control = f'<select id="{fid}" name="{name}">{options}</select>'
    else:
        mode = ' inputmode="decimal"' if f.kind == "number" else ""
        control = (
            f'<input type="text" id="{fid}" name="{name}"{mode} '
            f'value="{escape(str(value), quote=True)}">'
        )
    help_html = f'<p class="help">{escape(f.help)}</p>' if f.help else ""
    err_html = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
    cls = "field bad" if error else "field"
    return (
        f'<div class="{cls}"><label for="{fid}">{escape(f.label)}</label>{control}'
        f"{help_html}{err_html}</div>"
    )


def _contexts(state: FormState, error: str | None) -> str:
    rows = list(state.contexts) + [("", "")] * CONTEXT_BLANK_ROWS
    inputs = "".join(
        '<div class="ctx">'
        f'<input type="text" name="ctx_name_{i}" aria-label="Context name" placeholder="name" '
        f'value="{escape(name, quote=True)}">'
        f'<input type="text" name="ctx_path_{i}" aria-label="Context file path" '
        f'placeholder="path to a Markdown file" value="{escape(path, quote=True)}"></div>'
        for i, (name, path) in enumerate(rows)
    )
    err_html = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
    return (
        "<fieldset><legend>Contexts</legend>"
        '<p class="intro">Markdown files describing what you work on. Reports relate their '
        "findings to each one. Clear a row to remove it; relative paths count from the data "
        "folder.</p>"
        f"{inputs}{err_html}</fieldset>"
    )


def settings_page(
    state: FormState,
    errors: dict[str, str],
    *,
    saved: bool = False,
    problem: str = "",
) -> str:
    """`errors` are by field key. `saved` shows the confirmation; `problem` is a page-level
    message (the config file cannot be read, or a save failed)."""
    if problem:
        banner = f'<div class="banner bad" role="alert">{escape(problem)}</div>'
    elif errors:
        banner = (
            '<div class="banner bad" role="alert">Not saved. Fix the '
            f"{len(errors)} marked setting{'s' if len(errors) != 1 else ''} below.</div>"
        )
    elif saved:
        banner = '<div class="banner ok" role="status">Saved. Changes apply now.</div>'
    else:
        banner = ""
    shown = set()
    sections = []
    for section in SECTIONS:
        intro = f'<p class="intro">{escape(section.intro)}</p>' if section.intro else ""
        controls = "".join(_control(f, state, errors.get(f.key)) for f in section.fields)
        shown.update(f.key for f in section.fields)
        sections.append(
            f"<fieldset><legend>{escape(section.title)}</legend>{intro}{controls}</fieldset>"
        )
    contexts = _contexts(state, errors.get(CONTEXTS_KEY))
    # A problem on a key this form has no control for (a hand-edited depth, say) is still shown.
    shown.add(CONTEXTS_KEY)
    stray = "".join(
        f'<p class="err" role="alert">{escape(k)}: {escape(v)}</p>'
        for k, v in errors.items()
        if k not in shown
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Settings</title><style>{_CSS}{_CSS_EXTRA}</style></head><body><main>"
        '<h1>Settings</h1><p><a href="/">History</a></p>'
        f"{banner}"
        '<form class="settings" method="post" action="/settings">'
        f"{''.join(sections)}{contexts}{stray}"
        '<button type="submit">Save</button></form></main></body></html>'
    )

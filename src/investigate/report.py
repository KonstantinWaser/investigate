"""Render the Report: one self-contained HTML page, the same template for every Run.

All agent text is escaped. The page has inline CSS and no scripts or fonts, so viewing it makes no
network request. The only images are the Item's own attached images, shown as thumbnails by
relative path into `attachments/` of the run folder the page sits in. Raw HTML in the Markdown
body is escaped and images in it are disabled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt

from investigate.attachments import Attachment
from investigate.result import ParsedResult, parse_result

_md = MarkdownIt("commonmark", {"html": False}).disable("image")

STATUS_NOTES = {
    "partial": "This Run hit its Depth time limit and was stopped. The Report holds what the "
    "agent had written by then and may be incomplete.",
    "failed": "This Run failed. See agent.log in the run folder for the error.",
}

_CSS = """\
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1e21;--muted:#5f6670;--line:#d9dde3;\
--card:#f6f7f9;--warn:#fff4d6;--warnfg:#5a4300;--bad:#fde8e8;--badfg:#7a1d1d;--link:#1459b3}
@media(prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e6e8eb;--muted:#9aa3ad;\
--line:#2f343b;--card:#1d2024;--warn:#3a2f10;--warnfg:#f0d68a;--bad:#3b1a1a;--badfg:#f3b4b4;\
--link:#7db3ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:46rem;margin:0 auto;padding:2rem 1rem 4rem}
h1{font-size:1.7rem;line-height:1.2;margin:0 0 .5rem}
h2{font-size:1.2rem;margin:2rem 0 .75rem}
h3{font-size:1rem;margin:1.25rem 0 .25rem}
a{color:var(--link)}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:1.5rem}
.summary{font-size:1.1rem}
.notice{padding:.75rem 1rem;border-radius:.5rem;margin:1rem 0;background:var(--warn);\
color:var(--warnfg)}
.notice.bad{background:var(--bad);color:var(--badfg)}
.finding{border:1px solid var(--line);background:var(--card);border-radius:.5rem;\
padding:.9rem 1rem;margin:.75rem 0}
.finding p{margin:.25rem 0}
.conf{display:inline-block;font-size:.75rem;font-weight:600;text-transform:uppercase;\
border:1px solid var(--line);border-radius:1rem;padding:0 .5rem;color:var(--muted)}
.cites{margin:.5rem 0 0;padding-left:1.2rem;font-size:.9rem}
.note{color:var(--muted)}
details{margin-top:1.5rem}
.followup{border-top:2px solid var(--line);margin-top:2rem}
.followup blockquote{margin:.5rem 0;padding:.25rem .75rem;border-left:3px solid var(--line);\
white-space:pre-wrap}
.followup-box textarea{box-sizing:border-box;width:100%;min-height:5rem;font:inherit;\
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:.4rem;\
padding:.5rem}
.followup-box button{font:inherit;margin-top:.5rem;padding:.35rem 1rem}
.thumbs{display:flex;flex-wrap:wrap;gap:.75rem;margin:.5rem 0}
.thumbs img{max-width:12rem;max-height:9rem;border:1px solid var(--line);border-radius:.4rem}
pre{white-space:pre-wrap;word-break:break-word;background:var(--card);padding:.75rem;\
border-radius:.5rem;font-size:.85rem}
"""


# Comment markers in report.html. The page is a plain file; the Follow-up sections live between
# the first two, and the server swaps the third for the ask box (and adds a progress refresh at
# HEAD_MARK) only while serving, so the file view never shows a dead form.
FOLLOWUPS_OPEN = "<!--investigate:followups-->"
FOLLOWUPS_CLOSE = "<!--/investigate:followups-->"
FOLLOWUP_BOX = "<!--investigate:followup-box-->"
HEAD_MARK = "<!--investigate:head-->"


def render_followups(entries: list[dict]) -> str:
    """One dated section per stored Follow-up, in the order they were asked."""
    out = []
    for n, entry in enumerate(entries, 1):
        text = entry.get("text") or ""
        parsed = parse_result(text)
        structured = parsed.structured
        parts = [
            f'<section class="followup" id="followup-{n}">',
            f"<h2>Follow-up {n}, {escape(str(entry.get('asked', '')))}</h2>",
            f"<blockquote>{escape(str(entry.get('question', '')))}</blockquote>",
        ]
        if entry.get("note"):
            parts.append(f'<div class="notice">{escape(str(entry["note"]))}</div>')
        new_run = entry.get("new_run_id")
        if new_run:
            href = escape(quote(str(new_run), safe=""), quote=True)
            parts.append(
                f'<p>The answer is in a new Run: <a href="/runs/{href}/">'
                f"{escape(str(new_run))}</a></p>"
            )
        status = entry.get("status")
        if status == "partial":
            parts.append(f'<div class="notice">{escape(STATUS_NOTES["partial"])}</div>')
        if status == "failed":
            error = entry.get("error") or "The Follow-up failed."
            parts.append(f'<div class="notice bad">Follow-up failed: {escape(str(error))}</div>')
        if text.strip() and not new_run:
            if structured:
                parts.append(f'<p class="summary">{escape(structured.summary)}</p>')
                parts.append(_findings(parsed, "h3"))
            else:
                problems = "".join(f"<li>{escape(p)}</li>" for p in parsed.problems)
                parts.append(
                    '<div class="notice bad">The structured answer is missing or malformed, so '
                    f"the agent's raw text is shown.<ul>{problems}</ul></div>"
                )
            if parsed.body:
                parts.append(_md.render(parsed.body))
        parts.append("</section>")
        out.append("".join(parts))
    return "".join(out)


@dataclass(frozen=True)
class ReportInput:
    title: str  # fallback title, used when the result has none
    item: str
    kind: str
    depth: str
    status: str  # completed | partial | failed
    created: str
    result: ParsedResult
    error: str | None = None
    contexts: tuple[str, ...] = ()  # Context names in config order, one subsection each
    attachments: tuple[Attachment, ...] = ()


def _safe_link(url: str, label: str) -> str:
    if urlparse(url).scheme in ("http", "https"):
        return f'<a href="{escape(url, quote=True)}" rel="noopener noreferrer">{escape(label)}</a>'
    return f"{escape(label)} ({escape(url)})"


def _citations(citations) -> str:
    if not citations:
        return '<p class="note">No citations.</p>'
    rows = []
    for c in citations:
        note = f' <span class="note">{escape(c.note)}</span>' if c.note else ""
        rows.append(f"<li>{_safe_link(c.url, c.title)}{note}</li>")
    return f'<ul class="cites">{"".join(rows)}</ul>'


def _findings(result: ParsedResult, level: str = "h2") -> str:
    structured = result.structured
    if structured is None:
        return ""
    if not structured.findings:
        return f'<{level}>Findings</{level}><p class="note">The agent reported no findings.</p>'
    blocks = []
    for f in structured.findings:
        detail = f"<p>{escape(f.detail)}</p>" if f.detail else ""
        blocks.append(
            '<div class="finding">'
            f"<p><strong>{escape(f.claim)}</strong> "
            f'<span class="conf">{escape(f.confidence)} confidence</span></p>'
            f"{detail}{_citations(f.citations)}</div>"
        )
    return f"<{level}>Findings</{level}>" + "".join(blocks)


def _relevance(result: ParsedResult, contexts: tuple[str, ...]) -> str:
    """One subsection per configured Context, in config order. Other names are never shown."""
    if not contexts:
        return ""
    structured = result.structured
    by_name = {r.context: r for r in structured.relevance} if structured else {}
    parts = ["<h2>Relevance to your work</h2>"]
    for name in contexts:
        entry = by_name.get(name)
        if entry is None and structured is None:
            text = '<p class="note">Not available, the structured result is malformed.</p>'
        elif entry is None:
            text = '<p class="note">The agent reported nothing for this Context.</p>'
        elif entry.relevant and entry.text:
            text = _md.render(entry.text)
        else:
            text = '<p class="note">Not relevant</p>'
        parts.append(f'<section class="context"><h3>{escape(name)}</h3>{text}</section>')
    return "".join(parts)


_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _item_section(item: str, attachments: tuple[Attachment, ...]) -> str:
    parts = ["<h2>Item</h2>"]
    if item.strip():
        parts.append(f"<pre>{escape(item)}</pre>")
    urls = list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL.findall(item)))
    if urls:
        rows = "".join(f"<li>{_safe_link(u, u)}</li>" for u in urls)
        parts.append(f'<p class="note">URLs</p><ul class="cites">{rows}</ul>')
    if attachments:
        rows = []
        for a in attachments:
            href = escape(quote(a.relative_path), quote=True)
            rows.append(
                f'<li><a href="{href}">{escape(a.name)}</a> '
                f'<span class="note">{a.size:,} bytes</span></li>'
            )
        parts.append(f'<p class="note">Attachments</p><ul class="cites">{"".join(rows)}</ul>')
        images = [a for a in attachments if a.is_image]
        if images:
            thumbs = "".join(
                f'<a href="{escape(quote(a.relative_path), quote=True)}">'
                f'<img src="{escape(quote(a.relative_path), quote=True)}" '
                f'alt="{escape(a.name, quote=True)}" loading="lazy"></a>'
                for a in images
            )
            parts.append(f'<div class="thumbs">{thumbs}</div>')
    return "<section>" + "".join(parts) + "</section>"


def render_report(data: ReportInput) -> str:
    structured = data.result.structured
    title = structured.title if structured else data.title
    notices = []
    if data.status in STATUS_NOTES:
        cls = "notice bad" if data.status == "failed" else "notice"
        notices.append(f'<div class="{cls}">{escape(STATUS_NOTES[data.status])}</div>')
    if data.error:
        notices.append(f'<div class="notice bad">Error: {escape(data.error)}</div>')
    if structured is None:
        problems = "".join(f"<li>{escape(p)}</li>" for p in data.result.problems)
        notices.append(
            '<div class="notice bad">The structured result is missing or malformed, so this '
            f"Report shows the agent's raw text instead.<ul>{problems}</ul></div>"
        )
    summary = f'<p class="summary">{escape(structured.summary)}</p>' if structured else ""
    body = _md.render(data.result.body) if data.result.body else ""
    body_html = f"<h2>Write-up</h2>{body}" if body else ""
    meta = f"{data.kind} &middot; {escape(data.depth)} &middot; {escape(data.status)} &middot; "
    meta += escape(data.created)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{HEAD_MARK}<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{escape(title)}</h1>"
        f'<div class="meta">{meta}</div>'
        f"{''.join(notices)}{summary}{_findings(data.result)}"
        f"{_relevance(data.result, data.contexts)}{body_html}"
        f"{_item_section(data.item, data.attachments)}"
        f"{FOLLOWUPS_OPEN}{FOLLOWUPS_CLOSE}{FOLLOWUP_BOX}"
        "</main></body></html>\n"
    )

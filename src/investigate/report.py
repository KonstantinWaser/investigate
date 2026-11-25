"""Render the Report: one self-contained HTML page, the same template for every Run.

All agent text is escaped. The page has inline CSS and no scripts, fonts or images, so viewing
it makes no network request. Raw HTML in the Markdown body is escaped and images are disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import urlparse

from markdown_it import MarkdownIt

from investigate.result import ParsedResult

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
pre{white-space:pre-wrap;word-break:break-word;background:var(--card);padding:.75rem;\
border-radius:.5rem;font-size:.85rem}
"""


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


def _findings(result: ParsedResult) -> str:
    structured = result.structured
    if structured is None:
        return ""
    if not structured.findings:
        return '<h2>Findings</h2><p class="note">The agent reported no findings.</p>'
    blocks = []
    for f in structured.findings:
        detail = f"<p>{escape(f.detail)}</p>" if f.detail else ""
        blocks.append(
            '<div class="finding">'
            f"<p><strong>{escape(f.claim)}</strong> "
            f'<span class="conf">{escape(f.confidence)} confidence</span></p>'
            f"{detail}{_citations(f.citations)}</div>"
        )
    return "<h2>Findings</h2>" + "".join(blocks)


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
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{escape(title)}</h1>"
        f'<div class="meta">{meta}</div>'
        f"{''.join(notices)}{summary}{_findings(data.result)}"
        f"{_relevance(data.result, data.contexts)}{body_html}"
        f"<details><summary>Item</summary><pre>{escape(data.item)}</pre></details>"
        "</main></body></html>\n"
    )

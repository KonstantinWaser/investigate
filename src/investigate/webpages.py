"""HTML for the History page. Every value from an index entry or the query is escaped."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from html import escape

from investigate.history import HistoryEntry

KIND_LABELS = {"investigation": "Investigation", "fact-check": "Fact-check"}
VERDICT_LABELS = {
    "confirmed": "Confirmed",
    "partly_true": "Partly true",
    "outdated": "Outdated",
    "unverified": "Unverified",
    "wrong": "Wrong",
}

_CSS = """\
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1e21;--muted:#5f6670;--line:#d9dde3;\
--card:#f6f7f9;--link:#1459b3;--run:#8a5a00}
@media(prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e6e8eb;--muted:#9aa3ad;\
--line:#2f343b;--card:#1d2024;--link:#7db3ff;--run:#f0c060}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:64rem;margin:0 auto;padding:1.5rem 1rem 4rem}
h1{font-size:1.5rem;margin:0 0 1rem}
a{color:var(--link)}
form.filters{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1rem}
input,select,button{font:inherit;padding:.3rem .5rem}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:.45rem .5rem;border-bottom:1px solid var(--line);\
vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.8rem;text-transform:uppercase}
.running{color:var(--run);font-weight:600}
.queued{color:var(--muted);font-style:italic}
.muted{color:var(--muted)}
.empty{padding:2rem;text-align:center;color:var(--muted);background:var(--card);\
border-radius:.5rem}
td form{margin:0}
.notice{padding:.75rem 1rem;border-radius:.5rem;margin:0 0 1rem;background:var(--card);\
border:1px solid var(--run)}
"""


@dataclass(frozen=True)
class Filters:
    q: str = ""
    kind: str = ""
    verdict: str = ""
    since: str = ""  # YYYY-MM-DD, inclusive
    until: str = ""  # YYYY-MM-DD, inclusive


def local_day(created: str, tz: tzinfo | None = None) -> str:
    """The calendar day (YYYY-MM-DD) of an ISO timestamp in local time. A timestamp without an
    offset is UTC. Empty when it cannot be parsed."""
    try:
        moment = datetime.fromisoformat(created)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%Y-%m-%d")


def local_minute(created: str) -> str:
    try:
        moment = datetime.fromisoformat(created)
    except (TypeError, ValueError):
        return str(created)[:16]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _clean(e: HistoryEntry) -> HistoryEntry:
    """Coerce a hand-edited or newer-version entry so rendering cannot raise."""
    verdicts = e.verdicts
    if isinstance(verdicts, dict):
        verdicts = {str(k): v for k, v in verdicts.items() if isinstance(v, int)}
    else:
        verdicts = None
    created = e.created if isinstance(e.created, str) else ""
    return replace(
        e,
        id=str(e.id),
        kind=str(e.kind),
        depth=str(e.depth),
        status=str(e.status),
        title=str(e.title),
        created=created,
        verdicts=verdicts,
    )


def apply_filters(entries: Sequence[HistoryEntry], f: Filters) -> list[HistoryEntry]:
    """Newest first, then filtered by kind, Verdict, date range and a title search."""
    needle = f.q.strip().lower()
    out = []
    for e in sorted((_clean(e) for e in entries), key=lambda e: e.created, reverse=True):
        day = local_day(e.created)
        if f.kind and e.kind != f.kind:
            continue
        if f.verdict and not (e.verdicts or {}).get(f.verdict):
            continue
        if f.since and (not day or day < f.since):
            continue
        if f.until and (not day or day > f.until):
            continue
        if needle and needle not in e.title.lower():
            continue
        out.append(e)
    return out


def _options(choices: dict[str, str], selected: str, blank: str) -> str:
    rows = [f'<option value="">{escape(blank)}</option>']
    for value, label in choices.items():
        sel = " selected" if value == selected else ""
        rows.append(f'<option value="{escape(value)}"{sel}>{escape(label)}</option>')
    return "".join(rows)


def _verdict_cell(e: HistoryEntry) -> str:
    if not e.verdicts:
        return ""
    parts = [
        f"{n} {escape(VERDICT_LABELS.get(v, v))}"
        for v, n in e.verdicts.items()
        if isinstance(n, int)
    ]
    return ", ".join(parts)


def _row(e: HistoryEntry, active: Collection[str]) -> str:
    rid = escape(e.id, quote=True)
    status = (
        f'<span class="{e.status}">{e.status}</span>'
        if e.status in ("running", "queued")
        else escape(e.status)
    )
    if e.id in active:
        action = '<span class="muted">in progress</span>'
    else:
        action = (
            f'<form method="post" action="/runs/{rid}/delete" '
            f"onsubmit=\"return confirm('Delete this Run and its folder?')\">"
            "<button>Delete</button></form>"
        )
    return (
        "<tr>"
        f'<td><a href="/runs/{rid}/">{escape(e.title)}</a></td>'
        f"<td>{escape(KIND_LABELS.get(e.kind, e.kind))}</td>"
        f"<td>{escape(e.depth)}</td>"
        f"<td>{status}</td>"
        f"<td>{escape(local_minute(e.created))}</td>"
        f"<td>{_verdict_cell(e)}</td>"
        f"<td>{action}</td>"
        "</tr>"
    )


def history_page(
    entries: Sequence[HistoryEntry],
    f: Filters,
    active: Collection[str],
    total: int,
    notices: Sequence[str] = (),
) -> str:
    banner = "".join(f'<div class="notice">{escape(str(n))}</div>' for n in notices)
    refresh = (
        '<meta http-equiv="refresh" content="5">'
        if any(e.status in ("running", "queued") for e in entries)
        else ""
    )
    if entries:
        body = (
            "<table><thead><tr><th>Title</th><th>Kind</th><th>Depth</th><th>Status</th>"
            "<th>Date</th><th>Verdicts</th><th></th></tr></thead><tbody>"
            + "".join(_row(e, active) for e in entries)
            + "</tbody></table>"
        )
    elif total:
        body = '<div class="empty">No Run matches these filters.</div>'
    else:
        body = '<div class="empty">No Runs yet.</div>'
    kinds = _options(KIND_LABELS, f.kind, "Any kind")
    verdicts = _options(VERDICT_LABELS, f.verdict, "Any Verdict")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>History</title>{refresh}<style>{_CSS}</style></head><body><main>"
        f"<h1>History</h1>{banner}"
        '<form class="filters" method="get" action="/">'
        '<input type="search" name="q" placeholder="Search titles" '
        f'value="{escape(f.q, quote=True)}">'
        f'<select name="kind">{kinds}</select>'
        f'<select name="verdict">{verdicts}</select>'
        f'<input type="date" name="since" value="{escape(f.since, quote=True)}" title="From">'
        f'<input type="date" name="until" value="{escape(f.until, quote=True)}" title="To">'
        '<button>Filter</button> <a href="/">Reset</a></form>'
        f"{body}</main></body></html>"
    )

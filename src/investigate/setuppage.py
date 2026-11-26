"""The setup page: plain HTML forms, no script. Every value shown is escaped.

Each step shows its status and one action. A button that starts a program posts to
`/setup/<action>` and the server answers with a redirect back to `/setup?did=<action>`, so a
reload never repeats it. While a started program is still unfinished the page reloads itself with
a meta refresh (a bounded number of times). The page's CSS is the only inline code, allowed by
hash in `setup_csp`.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from investigate.settingspage import _hash
from investigate.setup import CHROME_URL, CLAUDE_INSTALL_URL, FFMPEG_URL
from investigate.setupweb import MAX_CONTEXT_BYTES, SetupStatus, UploadOutcome
from investigate.webpages import _CSS

REFRESH_SECONDS = 4
MAX_REFRESHES = 30

NOTICES = {
    "claude-login": "The login started in its own window. This page updates when it is done.",
    "browser-login": "Chrome opened. Log in, then close the window; this page updates then.",
    "ffmpeg": "The installer started in its own window. Restart Investigate afterwards so it "
    "finds ffmpeg.",
    "inbox": "Saved the Inbox settings.",
    "autostart": "Saved. The tray applies it now.",
    "browser-flag": "Saved.",
}

_CSS_EXTRA = """\
main{max-width:46rem}
.step{border:1px solid var(--line);border-radius:.5rem;margin:0 0 1rem;padding:.8rem 1rem}
.step h2{font-size:1.05rem;margin:0 0 .3rem;display:flex;gap:.6rem;align-items:center}
.pill{font-size:.75rem;font-weight:600;border-radius:1rem;padding:.05rem .6rem;\
border:1px solid var(--line)}
.pill.ok{border-color:#1b7f3b;color:#1b7f3b}
.pill.todo{border-color:var(--run);color:var(--run)}
.step p{margin:.3rem 0}
.help{color:var(--muted);font-size:.85rem}
.step form{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0 0}
.step label{display:flex;gap:.4rem;align-items:center}
.banner{padding:.6rem 1rem;border-radius:.4rem;margin:0 0 1rem;background:var(--card)}
.banner.bad{border:1px solid #b3261e;color:#b3261e}
.banner.ok{border:1px solid var(--line)}
ul.files{margin:.3rem 0;padding-left:1.2rem}
code{background:var(--card);padding:0 .25rem;border-radius:.2rem}
"""


def setup_csp() -> str:
    """No script at all; this page's style by hash; forms post to ourselves; no framing."""
    return (
        f"default-src 'none'; style-src {_hash(_CSS + _CSS_EXTRA)}; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )


def _pill(ok: bool, yes: str = "Ready", no: str = "To do") -> str:
    return f'<span class="pill {"ok" if ok else "todo"}">{escape(yes if ok else no)}</span>'


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}" rel="noopener noreferrer">{escape(label)}</a>'


def _button(action: str, label: str, *, fields: str = "") -> str:
    return (
        f'<form method="post" action="/setup/{escape(action, quote=True)}">{fields}'
        f'<button type="submit">{escape(label)}</button></form>'
    )


def _step(title: str, ok: bool, body: str, *, yes: str = "Ready", no: str = "To do") -> str:
    return f'<section class="step"><h2>{escape(title)} {_pill(ok, yes, no)}</h2>{body}</section>'


def _claude(s: SetupStatus) -> str:
    agent = s.agent
    if not agent.installed:
        body = (
            "<p>Claude Code is not installed.</p>"
            f"<p>{_link(CLAUDE_INSTALL_URL, 'Open the official install page')}, install it, "
            "then reload this page.</p>"
        )
        return _step("Claude Code", False, body, no="Not installed")
    if agent.logged_in is True:
        return _step("Claude Code", True, "<p>Installed and logged in.</p>", yes="Logged in")
    if agent.logged_in is False:
        body = (
            "<p>Installed, but not logged in.</p>"
            + _button("claude-login", "Log in")
            + '<p class="help">Opens the official <code>claude auth login</code> in its own '
            "window. If you use an API key or a cloud provider through environment variables, "
            "you can skip this.</p>"
        )
        return _step("Claude Code", False, body, no="Not logged in")
    body = (
        "<p>Installed. The login could not be checked "
        f"(<code>claude auth status</code> gave no answer, exit code {agent.code}).</p>"
        + _button("claude-login", "Log in")
    )
    return _step("Claude Code", False, body, no="Login not verified")


def _contexts(s: SetupStatus, outcomes: Sequence[UploadOutcome]) -> str:
    listed = "".join(
        f"<li>{escape(c.name)}: <code>{escape(c.path)}</code></li>" for c in s.contexts
    )
    files = f'<ul class="files">{listed}</ul>' if listed else "<p>No Context files yet.</p>"
    results = "".join(
        f'<li class="{"bad" if o.status in ("rejected", "kept") else "ok"}">'
        f"{escape(o.message)}</li>"
        for o in outcomes
    )
    results_html = f'<ul class="files" role="status">{results}</ul>' if results else ""
    form = (
        '<form method="post" action="/setup/contexts" enctype="multipart/form-data">'
        '<input type="file" name="files" multiple accept=".md,.markdown,.txt,text/*" '
        'aria-label="Context files">'
        '<label><input type="checkbox" name="replace"> Replace files that already exist</label>'
        '<button type="submit">Upload</button></form>'
    )
    help_html = (
        '<p class="help">A Context file is a Markdown file about what you work on. Reports '
        "relate their findings to it. Files are copied into the contexts folder of the data "
        f"folder (up to {MAX_CONTEXT_BYTES // 1024} KB each).</p>"
    )
    return _step("Context files", bool(s.contexts), files + results_html + form + help_html)


def _browser(s: SetupStatus) -> str:
    if not s.chrome:
        body = (
            "<p>Google Chrome was not found.</p>"
            f"<p>{_link(CHROME_URL, 'Download Chrome')}, install it, then reload this page.</p>"
        )
        return _step("Browser profile", False, body, no="Chrome missing")
    if s.browser_login_running:
        body = "<p>The login window is open. Log in, then close it.</p>"
        return _step("Browser profile", False, body, no="Login open")
    if s.browser_profile_busy and not s.browser_logged_in:
        body = "<p>The browser profile is in use (a Run or another window). Try again later.</p>"
        return _step("Browser profile", False, body, no="In use")
    if not s.browser_logged_in:
        body = (
            "<p>Chrome is installed. Log in once to YouTube, Instagram and X so Runs and the "
            "Inboxes can use your accounts.</p>" + _button("browser-login", "Log in to sites")
        )
        return _step("Browser profile", False, body, no="Not logged in")
    body = "<p>A login is recorded for the browser profile.</p>"
    if s.browser_flag:
        body += "<p>Runs use the logged-in browser.</p>"
    else:
        body += _button(
            "browser-flag",
            "Let Runs use the logged-in browser",
            fields=('<input type="hidden" name="browser" value="on">'),
        )
    body += _button("browser-login", "Log in again")
    return _step("Browser profile", True, body, yes="Logged in")


def _inbox(s: SetupStatus, title: str, field: str, label: str, value: str, help_text: str) -> str:
    other = "collection" if field == "playlist" else "playlist"
    other_value = s.collection if field == "playlist" else s.playlist
    body = (
        '<form method="post" action="/setup/inbox">'
        f'<label>{escape(label)} <input type="text" name="{field}" size="42" '
        f'value="{escape(value, quote=True)}"></label>'
        f'<input type="hidden" name="{other}" value="{escape(other_value, quote=True)}">'
        '<button type="submit">Save</button></form>'
        f'<p class="help">{escape(help_text)} Empty turns it off.</p>'
    )
    return _step(title, bool(value), body, yes="On", no="Off")


def _transcription(s: SetupStatus) -> str:
    if s.ffmpeg:
        extra = (
            ""
            if s.whisper
            else '<p class="help">faster-whisper is missing: <code>uv sync --extra '
            "transcribe</code></p>"
        )
        return _step("Transcription", s.whisper, "<p>ffmpeg is installed.</p>" + extra, no="Partly")
    if s.winget:
        body = (
            "<p>ffmpeg is not installed. It decodes the audio of videos and reels for "
            "transcription.</p>" + _button("ffmpeg", "Install ffmpeg with winget")
        )
    else:
        body = (
            "<p>ffmpeg is not installed and winget was not found.</p>"
            f"<p>{_link(FFMPEG_URL, 'Download ffmpeg')}</p>"
        )
    return _step("Transcription", False, body + _optional_help(), no="ffmpeg missing")


def _optional_help() -> str:
    return (
        '<p class="help">Optional. Without it, videos are checked on subtitles, title and '
        "description only.</p>"
    )


def _autostart(s: SetupStatus) -> str:
    checked = " checked" if s.start_at_login else ""
    body = (
        '<form method="post" action="/setup/autostart">'
        f'<label><input type="checkbox" name="start_at_login"{checked}> Start the tray when I '
        'log in</label><button type="submit">Save</button></form>'
    )
    return _step("Start at login", s.start_at_login, body, yes="On", no="Off")


def _refresh(s: SetupStatus, did: str, tick: int) -> str:
    waiting = {
        "claude-login": s.agent.installed and s.agent.logged_in is not True,
        "browser-login": s.chrome
        and (s.browser_login_running or s.browser_profile_busy or not s.browser_logged_in),
        "ffmpeg": not s.ffmpeg,
    }.get(did, False)
    if not waiting or tick >= MAX_REFRESHES:
        return ""
    target = f"/setup?did={escape(did, quote=True)}&amp;t={tick + 1}"
    return f'<meta http-equiv="refresh" content="{REFRESH_SECONDS};url={target}">'


def setup_page(
    status: SetupStatus,
    *,
    did: str = "",
    tick: int = 0,
    error: str = "",
    outcomes: Sequence[UploadOutcome] = (),
) -> str:
    """`did` names the action just done (its notice is shown); `error` is a page-level message."""
    if error:
        banner = f'<div class="banner bad" role="alert">{escape(error)}</div>'
    elif status.problem:
        banner = f'<div class="banner bad" role="alert">{escape(status.problem)}</div>'
    elif did in NOTICES:
        banner = f'<div class="banner ok" role="status">{escape(NOTICES[did])}</div>'
    else:
        banner = ""
    steps = [_claude(status)]
    if not status.problem:
        steps += [
            _contexts(status, outcomes),
            _browser(status),
            _inbox(
                status,
                "YouTube Inbox",
                "playlist",
                "Playlist URL or id",
                status.playlist,
                "Videos you add to this playlist are fact-checked twice a day.",
            ),
            _inbox(
                status,
                "Instagram Inbox",
                "collection",
                "Collection name",
                status.collection,
                "Reels you save to this Collection are fact-checked. Needs the browser login"
                + ("" if status.playwright else " and `uv sync --extra instagram`")
                + ".",
            ),
            _transcription(status),
            _autostart(status),
        ]
    refresh = _refresh(status, did, tick)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{refresh}<title>Setup</title><style>{_CSS}{_CSS_EXTRA}</style></head><body><main>"
        '<h1>Setup</h1><p><a href="/">History</a> | <a href="/settings">Settings</a></p>'
        f"{banner}{''.join(steps)}</main></body></html>"
    )

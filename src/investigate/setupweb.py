"""What the browser setup page (`/setup`) checks and does, apart from the HTML.

The status checks come from `setup.py`, the same code `investigate setup` runs. Every outside
effect (the `claude` and `winget` programs, the browser login, program lookup) is a field of
`SetupActions`, so tests drive the whole page with fakes. Rules the steps keep:

- A config write goes through `setup.edit_config`: comments kept, validated with the same rules
  `load_config` runs, replaced atomically.
- A Context file is only ever written inside `<data folder>/contexts/`, under a sanitised name.
  A file that is already there is kept unless the user ticked "replace".
- A program is started in its own window and never waited for. Its effect shows up the next time
  the page asks (`claude auth status`, `shutil.which`, the login marker).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from investigate import browser
from investigate.attachments import sanitise_name
from investigate.config import Config, ConfigError, ContextFile, config_path, load_config
from investigate.history import _replace
from investigate.inbox import parse_inbox_config
from investigate.profile_lock import ProfileBusy, profile_lock
from investigate.settings import read_raw
from investigate.setup import (
    AgentStatus,
    SetupIO,
    _add_context,
    agent_status,
    edit_config,
    ffmpeg_install_command,
)

CONTEXTS_DIR = "contexts"
MAX_CONTEXT_BYTES = 1024 * 1024  # a Context file is prose; the agent reads all of it
MAX_CONTEXT_FILES = 10  # per upload


class SetupError(ValueError):
    """A step cannot be done; the message tells the user why."""


def open_in_window(argv: list[str]) -> subprocess.Popen:
    """Start a program in a console window of its own and return it at once."""
    if sys.platform == "win32":
        return subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE)  # noqa: S603
    return subprocess.Popen(  # noqa: S603
        argv, stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True
    )


_login_lock = threading.Lock()
_login_thread: threading.Thread | None = None


def _profile_busy(home: Path) -> bool:
    """True while anything holds the browser profile: a Run, the tray's login window, a login
    from another process."""
    try:
        with profile_lock(home):
            return False
    except ProfileBusy:
        return True
    except OSError:
        return False


def background_login(home: Path) -> bool:
    """Run `browser.login` on a thread (it blocks until Chrome is closed). False when the profile
    is in use, so the caller never reports a login that cannot open."""
    global _login_thread
    with _login_lock:
        if _login_thread is not None and _login_thread.is_alive():
            return False
        if _profile_busy(home) or browser.profile_in_use(browser.profile_dir(home)):
            return False

        def work() -> None:
            with contextlib.suppress(Exception):  # nothing to tell: the page shows the marker
                browser.login(home)

        _login_thread = threading.Thread(target=work, name="setup-login", daemon=True)
        _login_thread.start()
        return True


def login_running(home: Path) -> bool:
    """A login window started from this page is open. Read-only: it never takes the profile
    lock, so loading the page cannot make a Run or an Inbox check lose the browser."""
    with _login_lock:
        return _login_thread is not None and _login_thread.is_alive()


def profile_open_elsewhere(home: Path) -> bool:
    """A browser has the profile open that this page did not start (a Run, another login).
    Read-only, like `login_running`."""
    return not login_running(home) and browser.profile_in_use(browser.profile_dir(home))


AGENT_STATUS_TTL_SECONDS = 30.0


class AgentStatusCache:
    """`claude auth status` result kept for a short time, with one check in flight at a time:
    concurrent page loads wait for the running check and share its answer."""

    def __init__(
        self, ttl: float = AGENT_STATUS_TTL_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        self._value: AgentStatus | None = None
        self._at = 0.0

    def get(self, io: SetupIO, *, refresh: bool = False) -> AgentStatus:
        seen = self._at
        with self._lock:
            fresh = self._value is not None and self._clock() - self._at < self._ttl
            if fresh and not (refresh and self._at == seen):
                return self._value  # type: ignore[return-value]
            self._value = agent_status(io)
            self._at = self._clock()
            return self._value


@dataclass(frozen=True)
class SetupActions:
    io: SetupIO = field(default_factory=SetupIO)
    # Starts a program in its own window; may return the process, so it is not started twice.
    open_window: Callable[[list[str]], Any] = open_in_window
    start_browser_login: Callable[[Path], bool] = background_login
    browser_login_running: Callable[[Path], bool] = login_running
    agent_cache: AgentStatusCache = field(default_factory=AgentStatusCache)
    started: dict[str, Any] = field(default_factory=dict)  # action name -> its process


@dataclass
class SetupStatus:
    agent: AgentStatus = field(default_factory=lambda: AgentStatus(False))
    contexts: list[ContextFile] = field(default_factory=list)
    chrome: bool = False
    browser_logged_in: bool = False
    browser_login_running: bool = False
    browser_profile_busy: bool = False
    browser_flag: bool = False
    playlist: str = ""
    collection: str = ""
    ffmpeg: bool = False
    winget: bool = False
    whisper: bool = False
    playwright: bool = False
    start_at_login: bool = False
    problem: str = ""  # the config file cannot be read


def gather_status(home: Path, actions: SetupActions, *, refresh: bool = False) -> SetupStatus:
    io = actions.io
    status = SetupStatus(
        agent=actions.agent_cache.get(io, refresh=refresh),
        chrome=io.chrome_found(),
        browser_logged_in=browser.is_logged_in(home),
        browser_login_running=actions.browser_login_running(home),
        browser_profile_busy=profile_open_elsewhere(home),
        ffmpeg=io.which("ffmpeg") is not None,
        winget=io.which("winget") is not None,
        whisper=io.has_module("faster_whisper"),
        playwright=io.has_module("playwright"),
    )
    try:
        config = load_config(home)  # creates the default file when it is absent
        raw = read_raw(home)
    except ConfigError as exc:
        status.problem = str(exc)
        return status
    status.contexts = list(config.contexts)
    status.browser_flag = config.browser
    status.start_at_login = config.start_at_login
    inbox = raw.get("inbox", {})
    inbox = inbox if isinstance(inbox, dict) else {}
    for key, attr in (("youtube", "playlist"), ("instagram", "collection")):
        table = inbox.get(key, {})
        value = table.get(attr, "") if isinstance(table, dict) else ""
        setattr(status, attr, value if isinstance(value, str) else "")
    return status


# programs


def _start_once(actions: SetupActions, action: str, argv: list[str]) -> None:
    """Open the program's window unless the one opened for this action is still running."""
    previous = actions.started.get(action)
    if previous is not None and previous.poll() is None:
        raise SetupError("That is already running in its own window. Finish it there first.")
    proc = actions.open_window(argv)
    if proc is not None:
        actions.started[action] = proc


def start_claude_login(actions: SetupActions) -> None:
    found = actions.io.which("claude")
    if found is None:
        raise SetupError("Claude Code is not installed. Use the install link first.")
    _start_once(actions, "claude-login", [found, "auth", "login"])


def start_ffmpeg_install(actions: SetupActions) -> None:
    if actions.io.which("ffmpeg") is not None:
        raise SetupError("ffmpeg is already installed.")
    winget = actions.io.which("winget")
    if winget is None:
        raise SetupError("winget was not found on this PC. Use the download link instead.")
    _start_once(actions, "ffmpeg", [winget, *ffmpeg_install_command()[1:]])


def start_browser_login(home: Path, actions: SetupActions) -> None:
    if not actions.io.chrome_found():
        raise SetupError("Google Chrome was not found. Install it with the download link first.")
    in_use = SetupError("The browser profile is in use (a Run or another login window).")
    if actions.browser_login_running(home) or not actions.start_browser_login(home):
        raise in_use


# Context files


@dataclass(frozen=True)
class UploadOutcome:
    filename: str
    status: str  # "added", "replaced", "kept" or "rejected"
    message: str


def _write_bytes_atomically(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _context_problem(data: bytes) -> str:
    if not data.strip():
        return "the file is empty"
    if len(data) > MAX_CONTEXT_BYTES:
        return f"the file is over {MAX_CONTEXT_BYTES // 1024} KB"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "the file is not UTF-8 text"
    if "\x00" in text:
        return "the file is not text"
    return ""


def _add_one(
    home: Path, config: Config, folder: Path, filename: str, data: bytes, replace: bool
) -> UploadOutcome:
    name = sanitise_name(filename)
    if "." not in name:
        name += ".md"
    problem = _context_problem(data)
    if problem:
        return UploadOutcome(name, "rejected", f"Not added: {problem}.")
    target = folder / name
    if target.resolve().parent != folder.resolve():  # cannot happen after sanitising; checked
        return UploadOutcome(name, "rejected", "Not added: the name is not allowed.")
    resolved = str(target.resolve())
    context_name = target.stem
    # Windows file names ignore case, so "Work.md" is the entry "work" already has.
    listed = {os.path.normcase(c.name): os.path.normcase(c.path) for c in config.contexts}
    key = os.path.normcase(context_name)
    if listed.get(key, os.path.normcase(resolved)) != os.path.normcase(resolved):
        return UploadOutcome(
            name,
            "rejected",
            f"Not added: a Context named {context_name!r} is already listed for another file.",
        )
    exists = target.exists()
    if exists and not replace:
        return UploadOutcome(
            name,
            "kept",
            f"{name} already exists in the contexts folder and was kept. "
            "Tick 'Replace files that already exist' to overwrite it.",
        )
    folder.mkdir(parents=True, exist_ok=True)
    try:
        _write_bytes_atomically(target, data)
    except OSError as exc:
        return UploadOutcome(name, "rejected", f"Not added: {exc}")
    if key not in listed:
        try:
            _add_context(home, context_name, Path(resolved))
        except (ConfigError, OSError) as exc:
            if not exists:
                with contextlib.suppress(OSError):
                    target.unlink()
            return UploadOutcome(name, "rejected", f"Not added: {exc}")
    return UploadOutcome(
        name, "replaced" if exists else "added", f"{'Replaced' if exists else 'Added'} {name}."
    )


def add_contexts(
    home: Path, uploads: Sequence[tuple[str, bytes]], replace: bool
) -> list[UploadOutcome]:
    """Copy uploaded files into `<data folder>/contexts/` and list them in the config."""
    if not uploads:
        raise SetupError("Choose at least one file.")
    if len(uploads) > MAX_CONTEXT_FILES:
        raise SetupError(f"At most {MAX_CONTEXT_FILES} files at a time.")
    try:
        config = load_config(home)
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc
    folder = home / CONTEXTS_DIR
    outcomes = []
    for filename, data in uploads:
        outcomes.append(_add_one(home, config, folder, filename, data, replace))
        with contextlib.suppress(ConfigError):  # a name added just now must count for the next
            config = load_config(home)
    return outcomes


# Inbox and switches


def _set_or_clear(doc: tomlkit.TOMLDocument, path: tuple[str, ...], value: str) -> None:
    *parents, last = path
    node: Any = doc
    for part in parents:
        if part not in node:
            if not value:
                return
            node[part] = tomlkit.table()
        node = node[part]
    if value:
        node[last] = value
    elif last in node:
        del node[last]


def set_inbox(home: Path, playlist: str, collection: str) -> Config:
    """Save the YouTube playlist and the Instagram Collection; an empty value removes it. Both are
    checked by `inbox.parse_inbox_config`, the code `load_inbox_config` runs."""
    path = config_path(home)

    def edit(doc: tomlkit.TOMLDocument) -> None:
        _set_or_clear(doc, ("inbox", "youtube", "playlist"), playlist.strip())
        _set_or_clear(doc, ("inbox", "instagram", "collection"), collection.strip())

    def check(raw: dict) -> None:
        parse_inbox_config(raw, path)

    return _edit(home, edit, check)


def _edit(home: Path, edit: Callable, check: Callable | None = None) -> Config:
    try:
        load_config(home)  # creates the default file when missing
        return edit_config(home, edit, check)
    except ConfigError as exc:
        message = str(exc)
        prefix = f"{config_path(home)}: "
        raise SetupError(message.removeprefix(prefix)) from exc
    except OSError as exc:
        raise SetupError(f"Could not write the config: {exc}") from exc


def set_start_at_login(home: Path, on: bool) -> Config:
    return _edit(home, lambda doc: doc.__setitem__("start_at_login", on))


def set_browser(home: Path, on: bool) -> Config:
    return _edit(home, lambda doc: doc.__setitem__("browser", on))

"""The logged-in browser: the persistent profile, the Playwright MCP server and `login`.

The profile lives in the data folder, never in the repo. Runs drive it through Playwright MCP;
`login` opens the same profile in a visible Chrome so the user can sign in once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from investigate.profile_lock import ProfileBusy, profile_lock

PROFILE_DIR = "browser-profile"
MARKER_NAME = "logged_in_at"  # written by `login`; Runs use the browser only once it exists
SERVER_NAME = "playwright"
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.86"  # pinned; bump on purpose
LOGIN_URLS = ("https://www.youtube.com", "https://www.instagram.com", "https://x.com")

# The read-only tools a logged-in search needs. Nothing else is auto-approved: not
# `browser_evaluate`, `browser_run_code_unsafe` or `browser_file_upload`.
ALLOWED_TOOLS = tuple(
    f"mcp__{SERVER_NAME}__browser_{name}"
    for name in (
        "navigate",
        "navigate_back",
        "snapshot",
        "find",
        "click",
        "type",
        "press_key",
        "wait_for",
        "tabs",
        "take_screenshot",
    )
)
# Denied in every permission mode: each runs code or reads files on the host, so a prompt
# injected through a web page must never reach them.
DENIED_TOOLS = tuple(
    f"mcp__{SERVER_NAME}__browser_{name}" for name in ("run_code_unsafe", "evaluate", "file_upload")
)

Launcher = Callable[[Path, list[str]], int]


def profile_dir(home: Path) -> Path:
    return home / PROFILE_DIR


def is_logged_in(home: Path) -> bool:
    return (home / MARKER_NAME).is_file()


def mark_logged_in(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER_NAME).write_text(datetime.now(UTC).isoformat(timespec="seconds"), "utf-8")


def playwright_server(profile: Path) -> dict:
    """The `mcpServers` entry that runs Playwright MCP on the persistent profile (headed)."""
    args = ["-y", PLAYWRIGHT_MCP, "--browser", "chrome", "--user-data-dir", str(profile)]
    if sys.platform == "win32":  # npx is a .cmd shim on Windows
        return {"command": "cmd", "args": ["/c", "npx", *args]}
    return {"command": "npx", "args": args}


def _find_chrome() -> str | None:
    """Chrome only: Runs use the `chrome` channel, and one profile must belong to one browser."""
    for name in ("chrome", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for root in filter(None, roots):
        candidate = Path(root) / "Google/Chrome/Application/chrome.exe"
        if candidate.exists():
            return str(candidate)
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.exists() else None


def profile_in_use(profile: Path) -> bool:
    """True when a Chrome still has the profile open (its own lock marker is live)."""
    if sys.platform == "win32":
        lockfile = profile / "lockfile"
        if not lockfile.exists():
            return False
        try:  # Chrome holds it open without delete sharing; a stale one can be removed
            lockfile.unlink()
        except OSError:
            return True
        return False
    link = profile / "SingletonLock"
    if not link.is_symlink():
        return False
    try:
        pid = int(os.readlink(link).rsplit("-", 1)[-1])
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError):
        return False  # stale
    except OSError:
        return True
    return True


def kill_profile_browsers(profile: Path) -> None:
    """Best effort: end a Chrome left on the profile after a Run.

    On Windows the runner's process-tree kill reaches it. On POSIX Playwright starts Chrome in
    its own process group, so a Run timeout would orphan it; match it by its profile argument.
    """
    if sys.platform == "win32":
        return
    try:
        subprocess.run(  # noqa: S603
            ["pkill", "-f", "--", f"--user-data-dir={profile}"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    except OSError:
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    from investigate.runner import _kill_tree as kill  # runner imports this module

    kill(proc)


def _launch_and_wait(profile: Path, urls: list[str]) -> int:
    exe = _find_chrome()
    if exe is None:
        print("error: Google Chrome was not found. Install it to log in.", file=sys.stderr)
        return 1
    proc = subprocess.Popen([exe, f"--user-data-dir={profile}", "--no-first-run", *urls])  # noqa: S603
    try:
        return proc.wait()
    finally:
        # Ctrl+C, or a chrome.exe that handed off and exited: never leave a window on the
        # profile after the lock is released.
        _kill_tree(proc)


def login(home: Path, launch: Launcher = _launch_and_wait) -> int:
    """Open the profile in a visible Chrome and wait until it is closed.

    The tray's "Log in to sites" item calls this too. Holds the profile lock meanwhile, so no
    Run opens the profile at the same time. A clean exit writes the login marker that lets Runs
    use the browser. Returns a process exit code.
    """
    if launch is _launch_and_wait and _find_chrome() is None:
        print("error: Google Chrome was not found. Install it to log in.", file=sys.stderr)
        return 1
    profile = profile_dir(home)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        with profile_lock(home):
            if profile_in_use(profile):
                print(
                    "error: The browser profile is already open in a Chrome window. Close it.",
                    file=sys.stderr,
                )
                return 1
            print("Log in to YouTube, Instagram and X, then close the browser window.")
            code = launch(profile, list(LOGIN_URLS))
            if code == 0:
                mark_logged_in(home)
            return code
    except ProfileBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

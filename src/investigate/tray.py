"""The tray host, without any GUI code.

`TrayApp` owns the History server (and through it the Run queue), builds the menu as plain data,
turns finished Runs into toasts and handles Quit. `run_tray` adds the single-instance lock. The
icon and the toast itself live in `tray_gui.py`, which only adapts these pieces to pystray, so
everything here runs and is tested headless.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from investigate import browser
from investigate.autostart import Autostart, is_ephemeral, startup_command
from investigate.config import Config, ConfigError, load_config, parse_hotkey
from investigate.hotkeys import HotkeyBackend, HotkeyError
from investigate.inbox import (
    InboxConfig,
    InboxSource,
    InboxState,
    check_inbox,
    default_sources,
    load_inbox_config,
)
from investigate.inbox_timer import InboxScheduler
from investigate.instance import InstanceLock, recover_interrupted, running_url
from investigate.runner import Runner
from investigate.server import DEFAULT_PORT, FinishedRun, HistoryServer

QUIT_GRACE_SECONDS = 10.0
POPUP_SIZE = "--window-size=560,380"


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: Callable[[], None] | None = None
    enabled: bool = True
    default: bool = False  # run on a left click of the icon


@dataclass(frozen=True)
class Toast:
    title: str
    message: str
    url: str  # opened when the toast is clicked


class Notifier:
    """Shows a toast; a click opens `toast.url`. The GUI layer provides the real one."""

    def show(self, toast: Toast) -> None:  # pragma: no cover - interface
        raise NotImplementedError


_UNSAFE = re.compile(r"[^\w .,:;!?()'/%+-]")
_TOAST_TITLES = {
    "completed": "Report ready",
    "partial": "Report ready (partial)",
    "failed": "Run failed",
}


def toast_text(text: str, limit: int = 100) -> str:
    """Report titles come from the agent, which read web pages. Toast backends build XML or a
    script from the text, so keep a single line of plain characters only."""
    plain = _UNSAFE.sub(" ", text)
    plain = " ".join(plain.split())
    return plain if len(plain) <= limit else plain[: limit - 3].rstrip() + "..."


def toast_for(finished: FinishedRun, base_url: str) -> Toast:
    title = _TOAST_TITLES.get(finished.status, "Run finished")
    return Toast(title, toast_text(finished.title) or "Untitled", f"{base_url}runs/{finished.id}/")


def inbox_toast(created) -> tuple[str, str]:
    """(title, message) summing up the Fact-checks one inbox check created."""
    count = len(created)
    title = "1 new Fact-check" if count == 1 else f"{count} new Fact-checks"
    names = "; ".join(toast_text(c.title, 40) or "Untitled" for c in created[:3])
    more = f" and {count - 3} more" if count > 3 else ""
    return title, f"{names}{more}"


def popup_command(url: str, env: dict[str, str] | None = None) -> list[str] | None:
    """A Chromium-based browser in app mode: a window with no tabs or address bar that shows the
    capture page. It uses the browser's own default profile, never the research profile."""
    env = os.environ if env is None else env
    candidates: list[str | None] = []
    if sys.platform == "win32":
        for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = env.get(var)
            if root:
                candidates.append(str(Path(root) / "Microsoft/Edge/Application/msedge.exe"))
        candidates.append(shutil.which("msedge"))
    chrome = browser._find_chrome()
    candidates += [chrome, shutil.which("chromium"), shutil.which("microsoft-edge")]
    for exe in candidates:
        if exe and Path(exe).is_file():
            return [exe, f"--app={url}", POPUP_SIZE]
    return None


def open_popup(url: str) -> None:
    command = popup_command(url)
    if command is None:
        webbrowser.open(url)  # no Chromium browser: an ordinary tab still works
        return
    subprocess.Popen(command)  # noqa: S603


class TrayApp:
    def __init__(
        self,
        home: Path,
        runner: Runner,
        *,
        port: int = DEFAULT_PORT,
        notifier: Notifier | None = None,
        open_url: Callable[[str], object] = webbrowser.open,
        open_capture: Callable[[str], object] = open_popup,
        login: Callable[[Path], int] = browser.login,
        stop_login: Callable[[], None] = browser.stop_login,
        quit_grace: float = QUIT_GRACE_SECONDS,
        hotkeys: HotkeyBackend | None = None,
        autostart: Autostart | None = None,
        get_startup_command: Callable[[], list[str]] | None = None,
        inbox_sources: Callable[[InboxConfig], list[InboxSource]] | None = None,
        clock: Callable[[], datetime] | None = None,
        inbox_poll_seconds: float = 30.0,
    ) -> None:
        self.home = home
        self.notifier = notifier
        self._open_url, self._open_capture = open_url, open_capture
        self._login, self._stop_login = login, stop_login
        self._quit_grace = quit_grace
        self.hotkeys, self.autostart = hotkeys, autostart
        self._startup_command = get_startup_command or (lambda: startup_command(home=home))
        self._apply_lock = threading.Lock()
        self.server = HistoryServer(
            home,
            runner,
            port=port,
            on_finished=self._on_finished,
            on_config_changed=self.apply_config,
        )
        self._inbox_sources = inbox_sources or (lambda config: default_sources(config, home))
        self._clock, self._inbox_poll = clock, inbox_poll_seconds
        self.inbox: InboxScheduler | None = None
        self._inbox_problem: str | None = None  # the outage last toasted, to toast it once
        self._login_thread: threading.Thread | None = None
        self._stop_gui: Callable[[], None] = lambda: None
        self._quitting = False

    # lifecycle

    def start(self) -> None:
        self.server.start()
        times = load_inbox_config(self.home).check_times  # a bad [inbox] fails the start-up
        options = {"clock": self._clock} if self._clock else {}
        self.inbox = InboxScheduler(
            InboxState(self.home),
            times,
            self._check_inbox,
            poll_seconds=self._inbox_poll,
            **options,
        )
        self.inbox.start()  # its first pass catches up a check missed while the app was off

    def bind_gui(self, stop: Callable[[], None]) -> None:
        """The GUI gives the way to end its loop; Quit calls it."""
        self._stop_gui = stop

    def shutdown(self) -> None:
        """Stop the server and the Runs, and close any login browser. Safe to call twice."""
        self._quitting = True
        if self.inbox is not None:
            self.inbox.stop()
        self._stop_login()
        if self.hotkeys is not None:
            self.hotkeys.unregister()
        self.server.shutdown(self._quit_grace)
        thread = self._login_thread
        if thread is not None:
            thread.join(10)

    # menu

    def menu_items(self) -> list[MenuItem]:
        return [
            MenuItem("New", self.on_new, default=True),
            MenuItem("History", self.on_history),
            MenuItem("Check inbox now", self.on_check_inbox),
            MenuItem("Log in to sites", self.on_login),
            MenuItem("Settings", self.on_settings),
            MenuItem("Quit", self.on_quit),
        ]

    def on_new(self) -> None:
        self._open_capture(f"{self.server.url}capture")

    def on_history(self) -> None:
        self._open_url(self.server.url)

    def on_settings(self) -> None:
        self._open_url(self._settings_url())

    def _settings_url(self) -> str:
        return f"{self.server.url}settings"

    def activate(self) -> None:
        """Apply the settings that live in the tray itself (hotkey, start at login) from the
        config file. The GUI calls this once its icon is up, so a notice has somewhere to go."""
        try:
            config = load_config(self.home)
        except ConfigError as exc:
            self._tell(Toast("Settings not applied", toast_text(str(exc)), self._settings_url()))
            return
        self.apply_config(config, at_startup=True)

    def apply_config(self, config: Config, *, at_startup: bool = False) -> None:
        """Make a new Config take effect in the tray: no restart needed for either setting."""
        with self._apply_lock:
            self._sync_hotkey(config.hotkey)
            self._sync_autostart(config.start_at_login, at_startup)

    def _sync_hotkey(self, text: str) -> None:
        if self.hotkeys is None:
            return
        self.hotkeys.unregister()
        try:
            hotkey = parse_hotkey(text)
            if hotkey is not None:
                self.hotkeys.register(hotkey, self.on_new)
        except (HotkeyError, ValueError) as exc:
            self._tell(
                Toast(
                    "Hotkey not available",
                    toast_text(f"{text} did not register: {exc}. Pick another in Settings."),
                    self._settings_url(),
                )
            )

    def _sync_autostart(self, wanted: bool, at_startup: bool) -> None:
        if self.autostart is None:
            return
        try:
            command = self._startup_command()
            if wanted and not self.autostart.matches(command):
                temporary = is_ephemeral(Path(command[0]))
                if at_startup and temporary and self.autostart.enabled():
                    # The entry may point at the user's real install; a scratch install that
                    # happens to start a tray must not take it over.
                    self._tell(
                        Toast(
                            "Start at login not updated",
                            toast_text(f"Left as it is: this tray runs from {command[0]}"),
                            self._settings_url(),
                        )
                    )
                    return
                self.autostart.enable(command)
                if temporary:
                    self._tell(
                        Toast(
                            "Start at login uses a temporary install",
                            toast_text(f"It starts {command[0]}, which may be deleted."),
                            self._settings_url(),
                        )
                    )
            elif not wanted and self.autostart.enabled():
                self.autostart.disable()
        except Exception as exc:  # registry or file errors: say so, keep the tray running
            self._tell(
                Toast(
                    "Start at login not changed",
                    toast_text(f"{type(exc).__name__}: {exc}"),
                    self._settings_url(),
                )
            )

    def on_check_inbox(self) -> None:
        if self.inbox is None or not self.inbox.check_now_in_background():
            self._tell(Toast("Inbox", "A check is already running.", self.server.url))

    def _check_inbox(self, manual: bool) -> bool:
        """One inbox check for the scheduler. Returns False when it should be retried soon."""
        history_url = f"{self.server.url}?kind=fact-check"
        try:
            config = load_inbox_config(self.home)
        except ConfigError as exc:
            self._tell(Toast("Inbox settings problem", toast_text(str(exc)), self.server.url))
            return True  # retrying cannot fix a config file
        if not config.configured:
            if manual:
                msg = "Set an [inbox.youtube] playlist or [inbox.instagram] collection first."
                self._tell(Toast("Inbox not set up", msg, self.server.url))
            return True
        result = check_inbox(
            self.home,
            self._inbox_sources(config),
            lambda item: self.server.submit_fact_check(
                item.text, config.depth, item.attachments, item.notes
            ),
            stop=lambda: self._quitting,
            max_attempts=config.max_attempts,
            max_new=config.max_new_per_check,
        )
        if self._quitting:
            return True
        if result.busy:
            if manual:
                self._tell(Toast("Inbox", "A check is already running.", history_url))
            return True
        # An outage is toasted once, again when the error changes, and when it is over. A manual
        # check always answers.
        # A logged-out source is an outage of that source only; the others have already run.
        # Every outage is shown, whether or not items were created in the same check.
        busy = bool(result.source_busy)
        problems = result.login_required + result.errors
        problem = " | ".join(problems) if problems else None
        outage = result.unreachable or bool(result.login_required)
        # A busy skip observed nothing about that source: it is neither news nor a recovery.
        recovered = problem is None and self._inbox_problem is not None and not busy
        retry = result.unreachable or busy
        changed = problem != self._inbox_problem
        announce = bool(problem) and (manual or outage) and (manual or changed)
        if result.created:
            self._tell(Toast(*inbox_toast(result.created), history_url))
        if announce:
            text = toast_text(problem or "")
            self._tell(Toast("Inbox check had problems", text, history_url))
        elif not result.created and not problem:
            if recovered and not manual:
                self._tell(Toast("Inbox", "The inbox is reachable again.", history_url))
            elif manual and not busy:
                self._tell(Toast("Inbox", "Nothing new in the inbox.", history_url))
        if manual and busy:
            note = "Skipped, the browser profile is in use. Retrying soon."
            self._tell(Toast("Inbox", toast_text(f"{result.source_busy[0]}. {note}"), history_url))
        for message in result.gave_up:
            self._tell(Toast("Inbox gave up on an item", toast_text(message), history_url))
        if (problem is None and not busy) or outage:
            self._inbox_problem = problem
        return not retry

    def on_login(self) -> None:
        """Opens the logged-in browser on its own thread: `login` blocks until Chrome closes."""
        if self._login_thread is not None and self._login_thread.is_alive():
            return
        self._login_thread = threading.Thread(target=self._do_login, daemon=True)
        self._login_thread.start()

    def _do_login(self) -> None:
        try:
            code = self._login(self.home)
        except Exception:
            code = 1
        if self._quitting:
            return
        if code == 0:
            self._tell(Toast("Logged in", "Runs can use the logged-in browser.", self.server.url))
        else:
            self._tell(
                Toast(
                    "Login did not finish",
                    "Chrome was not found, or the browser profile is in use.",
                    self.server.url,
                )
            )

    def on_quit(self) -> None:
        self._quitting = True
        self._stop_gui()

    # status and toasts

    def working(self) -> tuple[int, int]:
        """(running, queued) Runs, for the icon and its tooltip."""
        return len(self.server.queue.running_ids()), len(self.server.queue.queued_ids())

    def tooltip(self) -> str:
        running, queued = self.working()
        if not running and not queued:
            return "Investigate"
        parts = [f"{running} running"] + ([f"{queued} queued"] if queued else [])
        return "Investigate: " + ", ".join(parts)

    def _on_finished(self, finished: FinishedRun) -> None:
        if not self._quitting:
            self._tell(toast_for(finished, self.server.url))

    def _tell(self, toast: Toast) -> None:
        if self.notifier is not None:
            self.notifier.show(toast)


def run_tray(
    home: Path,
    runner: Runner,
    gui: Callable[[TrayApp], None],
    *,
    port: int = DEFAULT_PORT,
    app_factory: Callable[..., TrayApp] = TrayApp,
    **app_options,
) -> int:
    """Take the single-instance lock, start the host, run the GUI loop on this thread (macOS
    needs the main thread for it), then shut everything down. Returns an exit code."""
    lock = InstanceLock(home)
    if not lock.acquire():
        url = running_url(home)
        where = f" at {url}" if url else ""
        print(f"investigate is already running{where}. Not starting a second one.")
        return 0
    app = None
    try:
        try:
            app = app_factory(home, runner, port=port, **app_options)
            # Before the server listens, so a submit cannot arrive while recovery decides which
            # Runs a hard kill left behind.
            recover_interrupted(home)
            app.start()
        except (ValueError, OSError) as exc:  # bad config, or the port is taken
            print(f"error: {exc}", file=sys.stderr)
            return 2
        lock.publish(app.server.url)
        print(f"Tray running; History at {app.server.url}", flush=True)
        gui(app)
        return 0
    finally:
        if app is not None:
            app.shutdown()
        lock.release()

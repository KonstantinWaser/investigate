"""`investigate first-run` and `investigate uninstall`: what an installer does, so nobody needs a
console.

Every outside effect (shortcuts, the login entry, starting a process, the browser, stopping the
tray, uv) goes through `InstallIO`, so tests drive both commands with fakes and never touch the
real Start menu, registry, Startup folder, home folder or `uv`.

Windows only. The shortcuts are `.lnk` files in the per-user Start menu Programs folder, made
with PowerShell's `WScript.Shell` (no extra dependency, run without a window).

- `first-run` is idempotent: it only writes what is missing or points elsewhere, never replaces
  an existing config (it sets `start_at_login = true` in place, comments kept), and starts a tray
  only when none holds the host lock.
- `uninstall` asks first, stops the tray through its own Quit, removes the login entry and both
  shortcuts, asks separately (default no) before deleting the data folder, and then has a
  detached process run `uv tool uninstall investigate` once this command has exited, because uv
  cannot delete an `investigate.exe` that is still running.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from investigate.autostart import Autostart, default_autostart, entry_suffix, startup_command
from investigate.config import ConfigError, config_path, load_config
from investigate.instance import holds_lock, running_url
from investigate.runner import console_flags
from investigate.setup import _edit_config

SHORTCUT_NAME = "investigate"
UNINSTALL_NAME = "Uninstall investigate"
TOOL_NAME = "investigate"
WAIT_FOR_TRAY_SECONDS = 20.0
WAIT_FOR_QUIT_SECONDS = 30.0
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
# What marks a folder as an investigate data folder, so uninstall never deletes some other one.
_DATA_MARKERS = ("config.toml", "index.json", "runs", "browser-profile")


class Shortcuts:
    """Interface: `.lnk` files. `read` gives (target, arguments) or None when there is none."""

    def read(self, path: Path) -> tuple[str, str] | None:  # pragma: no cover - interface
        raise NotImplementedError

    def create(  # pragma: no cover - interface
        self, path: Path, target: str, arguments: str, description: str
    ) -> None:
        raise NotImplementedError

    def remove(self, path: Path) -> None:  # pragma: no cover - interface
        raise NotImplementedError


_READ_SCRIPT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:INV_LNK);"
    "[Console]::Out.Write($s.TargetPath+[char]10+$s.Arguments)"
)
_CREATE_SCRIPT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:INV_LNK);"
    "$s.TargetPath=$env:INV_TARGET;$s.Arguments=$env:INV_ARGS;"
    "$s.Description=$env:INV_DESC;$s.Save()"
)


class PowerShellShortcuts(Shortcuts):
    """Shortcuts through the `WScript.Shell` COM object. Values travel in environment variables,
    so no path or argument is ever parsed as script text."""

    def _powershell(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env={**os.environ, **env},
            **console_flags(),
        )

    def read(self, path: Path) -> tuple[str, str] | None:
        if not path.is_file():
            return None
        done = self._powershell(_READ_SCRIPT, INV_LNK=str(path))
        if done.returncode != 0:
            return None
        target, _, arguments = done.stdout.partition("\n")
        return target, arguments

    def create(self, path: Path, target: str, arguments: str, description: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        done = self._powershell(
            _CREATE_SCRIPT,
            INV_LNK=str(path),
            INV_TARGET=target,
            INV_ARGS=arguments,
            INV_DESC=description,
        )
        if done.returncode != 0:
            raise OSError(done.stderr.strip() or "PowerShell could not create the shortcut")

    def remove(self, path: Path) -> None:
        path.unlink(missing_ok=True)


def default_start_menu() -> Path:
    """The per-user Start menu Programs folder."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def console_command(home: Path | None = None, executable: str | None = None) -> list[str]:
    """How to start `investigate` in a console: the `investigate` script next to the interpreter
    (a uv tool or venv install has one), else the interpreter with `-m investigate.cli`."""
    exe = Path(executable or sys.executable)
    script = exe.with_name("investigate.exe" if sys.platform == "win32" else "investigate")
    command = [str(script)] if script.is_file() else [str(exe), "-m", "investigate.cli"]
    if home is not None and entry_suffix(home):
        command += ["--home", str(Path(home).expanduser().resolve())]
    return command


def start_detached(command: list[str]) -> None:
    """Start `command` with no console window, outliving this process."""
    flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags if sys.platform == "win32" else 0,
    )


def stop_tray(home: Path, *, wait: float = WAIT_FOR_QUIT_SECONDS) -> bool:
    """Ask the running tray to quit through its own Quit, then wait for it to release the host
    lock. True when no tray is left."""
    if not holds_lock(home):
        return True
    url = running_url(home)
    if url is None:
        return False
    try:
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        request = urllib.request.Request(  # noqa: S310
            url.rstrip("/") + "/api/quit", data=b"", method="POST", headers={"Origin": origin}
        )
        urllib.request.urlopen(request, timeout=10).close()  # noqa: S310
    except OSError:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not holds_lock(home):
            return True
        time.sleep(0.25)
    return not holds_lock(home)


def _uv_tool_dir() -> Path | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603
            [uv, "tool", "dir"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            **console_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip()
    return Path(text) if done.returncode == 0 and text else None


def is_uv_tool_install() -> bool:
    """True when this interpreter lives in uv's tool folder (`uv tool install investigate`)."""
    tools = _uv_tool_dir()
    if tools is None:
        return False
    try:
        return Path(sys.prefix).resolve().is_relative_to(tools.resolve())
    except OSError:
        return False


_UV_SCRIPT = (
    "Wait-Process -Id ([int[]]($env:INV_WAIT -split ',')) -ErrorAction SilentlyContinue;"
    "for($i=0;$i -lt 5;$i++){& $env:INV_UV tool uninstall $env:INV_TOOL;"
    "if($LASTEXITCODE -eq 0){break};Start-Sleep -Seconds 2}"
)


def schedule_uv_uninstall() -> bool:
    """Start a detached, windowless PowerShell that waits for this process (and the launcher
    that started it) to exit, then runs `uv tool uninstall investigate`, retrying a few times."""
    uv = shutil.which("uv")
    if uv is None:
        return False
    pids = {os.getpid(), os.getppid()} - {0}
    env = {
        **os.environ,
        "INV_UV": uv,
        "INV_TOOL": TOOL_NAME,
        "INV_WAIT": ",".join(str(p) for p in sorted(pids)),
    }
    flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    subprocess.Popen(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _UV_SCRIPT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=env,
        creationflags=flags,
    )
    return True


def _default_autostart(home: Path) -> Autostart | None:
    return default_autostart(suffix=entry_suffix(home))


@dataclass(frozen=True)
class InstallIO:
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print
    platform: str = sys.platform
    shortcuts: Shortcuts = field(default_factory=PowerShellShortcuts)
    start_menu: Path = field(default_factory=default_start_menu)
    autostart: Autostart | None = None
    tray_command: Callable[[Path], list[str]] = lambda home: startup_command(home=home)  # noqa: E731
    uninstall_command: Callable[[Path], list[str]] = lambda home: [  # noqa: E731
        *console_command(home),
        "uninstall",
    ]
    tray_running: Callable[[Path], bool] = holds_lock
    running_url: Callable[[Path], str | None] = running_url
    start_detached: Callable[[list[str]], None] = start_detached
    open_browser: Callable[[str], bool] = webbrowser.open
    stop_tray: Callable[[Path], bool] = stop_tray
    is_uv_tool_install: Callable[[], bool] = is_uv_tool_install
    schedule_uv_uninstall: Callable[[], bool] = schedule_uv_uninstall
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def _confirm(io: InstallIO, question: str) -> bool:
    try:
        answer = io.ask(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _autostart(io: InstallIO, home: Path) -> Autostart | None:
    return io.autostart if io.autostart is not None else _default_autostart(home)


def _links(io: InstallIO, home: Path) -> list[tuple[Path, list[str], str]]:
    return [
        (io.start_menu / f"{SHORTCUT_NAME}.lnk", io.tray_command(home), "Start the research desk"),
        (
            io.start_menu / f"{UNINSTALL_NAME}.lnk",
            io.uninstall_command(home),
            "Remove investigate from this computer",
        ),
    ]


def _split(command: list[str]) -> tuple[str, str]:
    return command[0], subprocess.list2cmdline(command[1:])


def _shortcuts(io: InstallIO, home: Path) -> bool:
    ok = True
    for path, command, description in _links(io, home):
        target, arguments = _split(command)
        try:
            if io.shortcuts.read(path) == (target, arguments):
                io.say(f"Start menu: {path.stem} is already there.")
                continue
            io.shortcuts.create(path, target, arguments, description)
            io.say(f"Start menu: created {path.stem}.")
        except OSError as exc:
            io.say(f"error: could not create the shortcut {path.stem}: {exc}")
            ok = False
    return ok


def _wait_for_url(io: InstallIO, home: Path) -> str | None:
    deadline = io.clock() + WAIT_FOR_TRAY_SECONDS
    while True:
        url = io.running_url(home)
        if url:
            return url
        if io.clock() >= deadline:
            return None
        io.sleep(0.25)


def run_first_run(home: Path, io: InstallIO | None = None) -> int:
    """0 when done, 1 when a step failed (the others still ran), 2 when nothing could be done."""
    io = io or InstallIO()
    if io.platform != "win32":
        io.say("first-run is for Windows installs. Run `investigate setup` instead.")
        return 2
    try:
        load_config(home)  # creates the default file only when it is absent
        config = _edit_config(home, lambda doc: doc.__setitem__("start_at_login", True))
    except ConfigError as exc:
        io.say(f"error: {exc}")
        io.say(f"Fix {config_path(home)} by hand (or delete it to start over); nothing was done.")
        return 2
    ok = True
    io.say(f"Data folder: {home}")
    if config.start_at_login:
        io.say("Start at login: on.")
    ok = _shortcuts(io, home) and ok
    autostart = _autostart(io, home)
    command = io.tray_command(home)
    if autostart is not None:
        try:
            if not autostart.matches(command):
                autostart.enable(command)
        except Exception as exc:  # registry errors: say so, go on
            io.say(f"error: start at login not registered: {type(exc).__name__}: {exc}")
            ok = False
    if io.tray_running(home):
        io.say("The tray is already running.")
    else:
        try:
            io.start_detached(command)
        except OSError as exc:
            io.say(f"error: could not start the tray: {exc}")
            return 1
        io.say("Started the tray.")
    url = _wait_for_url(io, home)
    if url is None:
        io.say(
            "The tray did not come up. Start it from the Start menu, then open "
            "http://127.0.0.1:8765/setup (or the address `investigate tray` prints)."
        )
        return 1
    page = url.rstrip("/") + "/setup"
    io.say(f"Opening {page}")
    io.open_browser(page)
    return 0 if ok else 1


def _is_data_folder(home: Path) -> bool:
    return any((home / marker).exists() for marker in _DATA_MARKERS)


def _delete_data_folder(io: InstallIO, home: Path) -> None:
    if not home.exists():
        return
    if not _is_data_folder(home):
        io.say(f"{home} does not look like an investigate data folder, so it is kept.")
        return
    question = f"Also delete the data folder {home} (your Reports, config and browser login)?"
    if not _confirm(io, question):
        io.say(f"Your data folder is kept: {home}")
        return

    def retry(func, path, exc):  # read-only files: clear the flag and try once more
        os.chmod(path, 0o700)
        func(path)

    try:
        shutil.rmtree(home, onexc=retry)
    except OSError as exc:
        io.say(f"error: could not delete {home}: {exc}")
    else:
        io.say(f"Deleted {home}")


def run_uninstall(home: Path, io: InstallIO | None = None, *, assume_yes: bool = False) -> int:
    """0 when done, 1 when stopped before removing anything or when a step failed."""
    io = io or InstallIO()
    if io.platform != "win32":
        io.say("uninstall is for Windows installs. Use your package manager's own uninstall.")
        return 2
    if not assume_yes and not _confirm(io, "Remove investigate from this computer?"):
        io.say("Nothing was changed.")
        return 1
    if not io.stop_tray(home):
        io.say("The tray did not quit. Quit it from its menu (or Task Manager) and run this again.")
        return 1
    autostart = _autostart(io, home)
    if autostart is not None:
        try:
            autostart.disable()
            io.say("Start at login: removed.")
        except Exception as exc:
            io.say(f"error: could not remove the start at login entry: {exc}")
    for path, _, _ in _links(io, home):
        try:
            io.shortcuts.remove(path)
            io.say(f"Start menu: removed {path.stem}.")
        except OSError as exc:
            io.say(f"error: could not remove {path.stem}: {exc}")
    _delete_data_folder(io, home)
    if not io.is_uv_tool_install():
        io.say("This is not a `uv tool` install. Remove it the way you installed it.")
        io.say(f"For a uv tool install that is: uv tool uninstall {TOOL_NAME}")
    elif io.schedule_uv_uninstall():
        io.say("The program itself is removed by uv in a few seconds, after this window closes.")
    else:
        io.say(f"uv was not found. Run: uv tool uninstall {TOOL_NAME}")
    return 0

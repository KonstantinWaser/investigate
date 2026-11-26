"""Start the tray when the user logs in, per operating system.

`Autostart` is the small seam the tray talks to; tests use a fake and never touch the real
registry, Startup entries or home folder. The command it registers is the interpreter that is
running now, started as `-m investigate.cli tray`, so it follows a `uv tool install` or a repo's
own `.venv` without needing `investigate` on PATH. On Windows `pythonw.exe` is preferred so no
console window opens at login.

- Windows: a value under `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`.
- macOS: a LaunchAgent plist in `~/Library/LaunchAgents` (`RunAtLoad`), read at the next login.
- Linux: an XDG autostart `.desktop` file in `$XDG_CONFIG_HOME/autostart`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

ENTRY_NAME = "Investigate"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_LABEL = "app.investigate.tray"


def entry_suffix(home: Path | None) -> str:
    """What makes a startup entry belong to one data folder: empty for the default folder, so
    its entry keeps the plain name, else a short hash of the folder."""
    from investigate.config import default_home

    if home is None:
        return ""
    resolved = Path(home).expanduser().resolve()
    if resolved == default_home():
        return ""
    return "-" + hashlib.sha1(str(resolved).lower().encode()).hexdigest()[:8]  # noqa: S324


def is_ephemeral(path: Path, temp: Path | None = None) -> bool:
    """A path inside the temp folder or a git worktree (its `.git` is a file): an install that
    is likely to be deleted soon, so it is a poor target for a login entry."""
    import tempfile

    temp = Path(temp or tempfile.gettempdir()).resolve()
    resolved = Path(path).absolute()
    try:
        resolved = Path(path).resolve()
    except OSError:
        pass
    if resolved.is_relative_to(temp):
        return True
    return any((parent / ".git").is_file() for parent in resolved.parents)


class Autostart:
    """Interface. `command` is an argument vector."""

    def enabled(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def matches(self, command: Sequence[str]) -> bool:  # pragma: no cover - interface
        """True when an entry exists and starts exactly `command`."""
        raise NotImplementedError

    def enable(self, command: Sequence[str]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def disable(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def startup_command(executable: str | None = None, home: Path | None = None) -> list[str]:
    """How a login starts the tray: this interpreter, without a console window on Windows, on
    `home` when that is not the default data folder."""
    exe = Path(executable or sys.executable)
    suffix = ["--home", str(Path(home).expanduser().resolve())] if entry_suffix(home) else []
    # An install puts the `investigate-tray` gui-script next to its interpreter. It is the
    # windowless entry point, so a login start opens no console.
    script = exe.with_name(
        "investigate-tray.exe" if sys.platform == "win32" else "investigate-tray"
    )
    if script.is_file():
        return [str(script), *suffix]
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        windowless = exe.with_name("pythonw.exe")
        if windowless.is_file():
            exe = windowless
    return [str(exe), "-m", "investigate.cli", "tray", *suffix]


class WindowsRunKey(Autostart):
    def __init__(self, winreg: ModuleType | None = None, name: str = ENTRY_NAME) -> None:
        if winreg is None:
            import winreg  # type: ignore[no-redef]
        self._reg = winreg
        self._name = name

    def _read(self) -> str | None:
        reg = self._reg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = reg.QueryValueEx(key, self._name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def enabled(self) -> bool:
        return self._read() is not None

    def matches(self, command: Sequence[str]) -> bool:
        return self._read() == subprocess.list2cmdline(list(command))

    def enable(self, command: Sequence[str]) -> None:
        reg = self._reg
        with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            reg.SetValueEx(key, self._name, 0, reg.REG_SZ, subprocess.list2cmdline(list(command)))

    def disable(self) -> None:
        reg = self._reg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, self._name)
        except FileNotFoundError:
            pass


class _FileEntry(Autostart):
    """An entry that is one file; subclasses say where it goes and what it holds."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _render(self, command: Sequence[str]) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def enabled(self) -> bool:
        return self.path.is_file()

    def matches(self, command: Sequence[str]) -> bool:
        try:
            return self.path.read_bytes() == self._render(command)
        except OSError:
            return False

    def enable(self, command: Sequence[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_bytes(self._render(command))
            os.replace(temp, self.path)
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()

    def disable(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def _desktop_quote(arg: str) -> str:
    """One Exec argument as the Desktop Entry spec wants it: quoted when it has a space or a
    reserved character, with the characters that stay special inside quotes escaped."""
    reserved = " \t\n\"'\\><~|&;$*?#()`"
    if not arg or not any(c in reserved for c in arg):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped.replace("%", "%%") + '"'


class XdgAutostart(_FileEntry):
    def _render(self, command: Sequence[str]) -> bytes:
        exec_line = " ".join(_desktop_quote(a) for a in command)
        return (
            "[Desktop Entry]\nType=Application\nName=Investigate\n"
            f"Comment=Research desk tray\nExec={exec_line}\nTerminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        ).encode()


class MacLaunchAgent(_FileEntry):
    def __init__(self, path: Path, label: str = MAC_LABEL) -> None:
        super().__init__(path)
        self.label = label

    def _render(self, command: Sequence[str]) -> bytes:
        return plistlib.dumps(
            {"Label": self.label, "ProgramArguments": list(command), "RunAtLoad": True}
        )


def default_autostart(
    platform: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    suffix: str = "",
) -> Autostart | None:
    """The entry for this operating system, or None where there is no way to do it. `suffix` (from
    `entry_suffix`) gives a non-default data folder an entry of its own."""
    platform = platform or sys.platform
    home = home or Path.home()
    env = os.environ if env is None else env
    if platform == "win32":
        return WindowsRunKey(name=ENTRY_NAME + suffix)
    if platform == "darwin":
        label = MAC_LABEL + suffix
        return MacLaunchAgent(home / "Library" / "LaunchAgents" / f"{label}.plist", label)
    if platform.startswith("linux"):
        config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        return XdgAutostart(config / "autostart" / f"investigate{suffix}.desktop")
    return None

"""The OS seams: autostart entries against fakes and temp folders (never the real registry,
Startup folder or home), and the hotkey backends."""

from __future__ import annotations

import plistlib
import subprocess
import sys

import pytest

from investigate.autostart import (
    MacLaunchAgent,
    WindowsRunKey,
    XdgAutostart,
    default_autostart,
    startup_command,
)
from investigate.config import parse_hotkey
from investigate.hotkeys import (
    HotkeyError,
    PynputHotkeys,
    Win32Hotkeys,
    pynput_spec,
    win32_keycode,
    win32_modifiers,
)

COMMAND = ["C:\\Program Files\\uv tool\\pythonw.exe", "-m", "investigate.cli", "tray"]


class FakeWinreg:
    """Just enough of `winreg`, backed by a dict."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    class _Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        if (root, path) not in self.keys:
            raise FileNotFoundError(path)
        return self._Key(self.keys[(root, path)])

    def CreateKey(self, root, path):  # noqa: N802
        return self._Key(self.keys.setdefault((root, path), {}))

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        key.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]


def test_windows_run_key_adds_matches_and_removes_the_entry():
    reg = FakeWinreg()
    entry = WindowsRunKey(reg)
    assert not entry.enabled() and not entry.matches(COMMAND)
    entry.enable(COMMAND)
    assert entry.enabled() and entry.matches(COMMAND)
    stored = next(iter(reg.keys.values()))["Investigate"]
    assert stored == subprocess.list2cmdline(COMMAND)
    assert stored.startswith('"C:\\Program Files\\uv tool\\pythonw.exe" -m')  # spaces quoted
    assert not entry.matches(["other.exe"])
    entry.disable()
    assert not entry.enabled()
    entry.disable()  # nothing to remove is fine


def test_windows_disable_without_the_run_key_is_fine():
    WindowsRunKey(FakeWinreg()).disable()


def test_xdg_entry_is_a_desktop_file_with_a_quoted_exec(tmp_path):
    path = tmp_path / "cfg" / "autostart" / "investigate.desktop"
    entry = XdgAutostart(path)
    entry.enable(["/opt/my tools/python", "-m", "investigate.cli", "tray"])
    text = path.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in text and "Type=Application" in text
    assert 'Exec="/opt/my tools/python" -m investigate.cli tray' in text
    assert entry.enabled() and entry.matches(
        ["/opt/my tools/python", "-m", "investigate.cli", "tray"]
    )
    assert not entry.matches(["/usr/bin/python"])
    entry.disable()
    assert not path.exists() and not entry.enabled()
    entry.disable()


def test_mac_launch_agent_is_a_plist_that_runs_at_load(tmp_path):
    path = tmp_path / "LaunchAgents" / "x.plist"
    entry = MacLaunchAgent(path)
    entry.enable(["/usr/bin/python3", "-m", "investigate.cli", "tray"])
    data = plistlib.loads(path.read_bytes())
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"] == ["/usr/bin/python3", "-m", "investigate.cli", "tray"]
    assert entry.matches(["/usr/bin/python3", "-m", "investigate.cli", "tray"])
    assert not list(path.parent.glob("*.tmp"))


def test_default_autostart_picks_the_entry_for_the_platform_inside_the_given_home(tmp_path):
    linux = default_autostart("linux", tmp_path, {})
    assert linux.path == tmp_path / ".config" / "autostart" / "investigate.desktop"
    xdg = default_autostart("linux", tmp_path, {"XDG_CONFIG_HOME": str(tmp_path / "x")})
    assert xdg.path == tmp_path / "x" / "autostart" / "investigate.desktop"
    mac = default_autostart("darwin", tmp_path, {})
    assert mac.path.parent == tmp_path / "Library" / "LaunchAgents"
    assert default_autostart("plan9", tmp_path, {}) is None


def test_the_startup_command_runs_this_interpreter_as_a_module(tmp_path):
    # In a folder without the `investigate-tray` script (the installed one is in the real venv).
    python = tmp_path / "python.exe"
    command = startup_command(str(python))
    assert command[1:] == ["-m", "investigate.cli", "tray"]
    assert command[0].lower().endswith(("python.exe", "pythonw.exe", "python", "python3"))
    assert startup_command("C:\\x\\python3.12.exe")[0] == "C:\\x\\python3.12.exe"


# hotkeys


def test_win32_codes():
    hotkey = parse_hotkey("ctrl+alt+i")
    assert win32_keycode("i") == 0x49 and win32_keycode("5") == 0x35 and win32_keycode("f1") == 0x70
    assert win32_keycode("f12") == 0x7B
    assert win32_modifiers(hotkey) == 0x2 | 0x1 | 0x4000  # ctrl, alt, no auto-repeat


def test_pynput_spec_matches_what_pynput_parses():
    assert pynput_spec(parse_hotkey("ctrl+alt+i")) == "<alt>+<ctrl>+i"
    assert pynput_spec(parse_hotkey("win+shift+f5")) == "<shift>+<cmd>+<f5>"
    keyboard = pytest.importorskip("pynput.keyboard")
    for text in ("ctrl+alt+i", "win+shift+f5", "alt+9"):
        keys = keyboard.HotKey.parse(pynput_spec(parse_hotkey(text)))
        assert len(keys) == len(parse_hotkey(text).modifiers) + 1


def test_pynput_backend_reports_a_listener_that_cannot_start(monkeypatch):
    pytest.importorskip("pynput.keyboard")

    class Dead:
        def __init__(self, hotkeys):
            pass

        def start(self):
            raise RuntimeError("no display")

    monkeypatch.setattr("pynput.keyboard.GlobalHotKeys", Dead)
    with pytest.raises(HotkeyError, match="no display"):
        PynputHotkeys().register(parse_hotkey("ctrl+alt+i"), lambda: None)


@pytest.mark.skipif(sys.platform != "win32", reason="RegisterHotKey is Windows only")
def test_win32_hotkey_fires_the_callback_and_a_taken_combination_is_reported():
    import threading

    fired = threading.Event()
    hotkey = parse_hotkey("ctrl+alt+shift+f23")  # a combination nothing uses
    first, second = Win32Hotkeys(), Win32Hotkeys()
    first.register(hotkey, fired.set)
    try:
        first.press()
        assert fired.wait(5)
        with pytest.raises(HotkeyError, match="already uses"):
            second.register(hotkey, lambda: None)
    finally:
        first.unregister()
    second.register(hotkey, lambda: None)  # free again after unregister
    second.unregister()

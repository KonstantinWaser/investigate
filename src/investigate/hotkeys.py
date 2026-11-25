"""The global hotkey that opens quick capture.

`HotkeyBackend` is the seam the tray uses; tests use a fake. Registering raises `HotkeyError`
with a reason the tray shows as a notice.

- Windows: `RegisterHotKey` through ctypes, on a thread of its own with a message loop. The OS
  refuses a combination another program holds, so a conflict is reported instead of failing
  silently, and no keyboard hook is installed.
- macOS and Linux: `pynput` (the `tray` extra). It cannot tell a conflict from success. macOS
  needs the Accessibility permission for the app that runs it, and Wayland has no global hotkeys
  for it, so there it is reported only when the listener cannot start.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

from investigate.config import Hotkey

REGISTER_TIMEOUT = 5.0
_PYNPUT_START_CHECK = 0.4  # seconds to see whether the listener thread survives its start


class HotkeyError(Exception):
    """The hotkey could not be registered. The message is shown to the user."""


class HotkeyBackend:
    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        """Replace any earlier registration. Raises HotkeyError."""
        raise NotImplementedError  # pragma: no cover - interface

    def unregister(self) -> None:
        raise NotImplementedError  # pragma: no cover - interface


# Windows

_MOD = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409


def win32_keycode(key: str) -> int:
    """The virtual-key code of a `Hotkey.key`: a letter or digit is its upper-case ASCII, F1 is
    0x70."""
    if key.startswith("f") and len(key) > 1:
        return 0x70 + int(key[1:]) - 1
    return ord(key.upper())


def win32_modifiers(hotkey: Hotkey) -> int:
    return sum(_MOD[m] for m in hotkey.modifiers) | _MOD_NOREPEAT


class Win32Hotkeys(HotkeyBackend):
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        import ctypes
        from ctypes import wintypes

        self.unregister()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.PeekMessageW.argtypes = user32.GetMessageW.argtypes + (wintypes.UINT,)
        outcome: dict[str, int] = {}
        ready = threading.Event()

        def loop() -> None:
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's queue
            outcome["thread"] = kernel32.GetCurrentThreadId()
            ok = user32.RegisterHotKey(None, 1, win32_modifiers(hotkey), win32_keycode(hotkey.key))
            outcome["ok"] = int(bool(ok))
            outcome["error"] = ctypes.get_last_error()
            ready.set()
            if not ok:
                return
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == _WM_HOTKEY:
                        try:
                            callback()
                        except Exception:  # one failed press must not end the listener
                            pass
            finally:
                user32.UnregisterHotKey(None, 1)

        thread = threading.Thread(target=loop, name="hotkey", daemon=True)
        thread.start()
        if not ready.wait(REGISTER_TIMEOUT) or not outcome.get("ok"):
            error = outcome.get("error", 0)
            if error == _ERROR_HOTKEY_ALREADY_REGISTERED:
                raise HotkeyError("another program already uses it")
            raise HotkeyError(f"Windows refused it (error {error})")
        self._thread, self._thread_id = thread, outcome["thread"]

    def unregister(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        import ctypes

        user32 = ctypes.WinDLL("user32")
        user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        thread.join(2)

    def press(self) -> None:
        """Deliver a hotkey press to the listener, as the OS would. For tests."""
        import ctypes

        ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, _WM_HOTKEY, 1, 0)


# macOS and Linux


def pynput_spec(hotkey: Hotkey) -> str:
    """The key description `pynput.keyboard.GlobalHotKeys` takes, such as `<ctrl>+<alt>+i`."""
    names = {"win": "<cmd>"}
    mods = [names.get(m, f"<{m}>") for m in sorted(hotkey.modifiers)]
    key = f"<{hotkey.key}>" if hotkey.key.startswith("f") and len(hotkey.key) > 1 else hotkey.key
    return "+".join([*mods, key])


class PynputHotkeys(HotkeyBackend):
    def __init__(self) -> None:
        self._listener = None

    def register(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        self.unregister()
        try:
            from pynput import keyboard

            listener = keyboard.GlobalHotKeys({pynput_spec(hotkey): callback})
            listener.start()
        except Exception as exc:  # no display, missing backend, bad key
            raise HotkeyError(f"global hotkeys are not available here ({exc})") from exc
        time.sleep(_PYNPUT_START_CHECK)
        if not listener.is_alive():
            raise HotkeyError("the keyboard listener could not start")
        self._listener = listener

    def unregister(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()


def default_hotkeys() -> HotkeyBackend:
    return Win32Hotkeys() if sys.platform == "win32" else PynputHotkeys()

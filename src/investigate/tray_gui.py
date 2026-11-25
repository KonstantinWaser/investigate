"""The thin GUI layer: the pystray icon and menu, and the toasts. No logic lives here.

Needs the `tray` extra (`uv sync --extra tray`). The loop runs on the calling thread, which must
be the main thread on macOS. The server and the queue are on worker threads of `TrayApp`.

Toasts: pystray's own `notify` cannot report a click, so on Windows the click-through toast comes
from `winotify` (a toast whose click launches the Report URL in the default browser). Elsewhere
the toast is pystray's notification and has no click-through.
"""

from __future__ import annotations

import signal
import sys
import threading

from investigate.tray import Notifier, Toast, TrayApp

_POLL_SECONDS = 1.0


def _icon_image(working: bool):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=(31, 41, 55, 255))
    draw.ellipse((18, 16, 40, 38), outline=(255, 255, 255, 255), width=4)  # lens
    draw.line((38, 36, 48, 48), fill=(255, 255, 255, 255), width=5)  # handle
    if working:
        draw.ellipse((40, 4, 62, 26), fill=(245, 158, 11, 255), outline=(255, 255, 255, 255))
    return image


class _ToastNotifier(Notifier):
    def __init__(self, icon) -> None:
        self._icon = icon

    def show(self, toast: Toast) -> None:
        if sys.platform == "win32":
            try:
                from winotify import Notification

                Notification(
                    app_id="Investigate",
                    title=toast.title,
                    msg=toast.message,
                    launch=toast.url,
                ).show()
                return
            except Exception:  # fall back to the balloon, which has no click-through
                pass
        try:
            self._icon.notify(toast.message, toast.title)
        except Exception:
            pass


def run(app: TrayApp) -> None:
    """Show the icon and block until Quit (or Ctrl+C / SIGTERM).

    Known macOS limits, untested here and not fixable cleanly through pystray: `watch` below sets
    the icon and title from a worker thread, which AppKit wants on the main thread (pystray has
    no call to marshal onto it), and pystray installs its own SIGINT handler that terminates
    NSApp directly, so Ctrl+C there skips `run_tray`'s shutdown. Quit from the menu is the
    supported way to leave on macOS.
    """
    import pystray

    def entry(item):
        action = item.action
        # pystray calls an action with (icon, item); ours take nothing.
        callback = (lambda *_: action()) if action else None
        return pystray.MenuItem(item.label, callback, enabled=item.enabled, default=item.default)

    menu = pystray.Menu(*[entry(item) for item in app.menu_items()])
    icon = pystray.Icon("investigate", _icon_image(False), "Investigate", menu)
    app.notifier = _ToastNotifier(icon)
    done = threading.Event()

    def stop() -> None:
        done.set()
        icon.stop()

    app.bind_gui(stop)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: app.on_quit())
            except ValueError:  # not the main thread
                pass

    def watch() -> None:
        """Mirror the queue in the icon: a dot while Runs are going, a tooltip with the counts."""
        shown = None
        while not done.wait(_POLL_SECONDS):
            running, queued = app.working()
            working = bool(running or queued)
            if working != shown:
                icon.icon = _icon_image(working)
                shown = working
            icon.title = app.tooltip()

    def setup(icon) -> None:
        icon.visible = True
        threading.Thread(target=watch, daemon=True).start()

    icon.run(setup)

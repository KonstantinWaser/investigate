"""One tray per data folder, across processes.

The first `investigate tray` takes an OS file lock in the data folder and only then binds the
port, so two starts at once cannot both serve. The lock is released by the OS when the holder
exits or crashes, so it never goes stale. The holder publishes its URL in `tray.json` for the
second start to report.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from investigate.profile_lock import _try_lock, _unlock

LOCK_NAME = "tray.lock"
INFO_NAME = "tray.json"


class InstanceLock:
    def __init__(self, home: Path) -> None:
        self.home = home
        self._handle = None

    def acquire(self) -> bool:
        """True for the first instance; False when another tray holds the lock."""
        self.home.mkdir(parents=True, exist_ok=True)
        handle = open(self.home / LOCK_NAME, "a+b")  # noqa: SIM115
        if not _try_lock(handle):
            handle.close()
            return False
        self._handle = handle
        # A file left by a crashed holder must not be reported as the running instance.
        with contextlib.suppress(OSError):
            (self.home / INFO_NAME).unlink()
        return True

    def publish(self, url: str) -> None:
        (self.home / INFO_NAME).write_text(json.dumps({"url": url}), encoding="utf-8")

    def release(self) -> None:
        if self._handle is None:
            return
        with contextlib.suppress(OSError):
            (self.home / INFO_NAME).unlink()
        _unlock(self._handle)
        self._handle.close()
        self._handle = None


def running_url(home: Path) -> str | None:
    """The URL the running tray published, if it has got that far."""
    try:
        data = json.loads((home / INFO_NAME).read_text(encoding="utf-8"))
        url = data.get("url")
    except (OSError, ValueError, AttributeError):
        return None
    return url if isinstance(url, str) else None

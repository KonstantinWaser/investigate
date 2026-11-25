"""An exclusive, cross-process lock on the browser profile.

A Chromium profile can be open in one browser at a time. Whoever drives it (a Run, the login
flow, later the Inbox checks) holds this lock for as long as the browser may be open. The lock is
an OS file lock, so it is released when the holder exits or crashes and never goes stale.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = "profile.lock"


class ProfileBusy(RuntimeError):
    """The browser profile is in use and the wait ran out."""


def _try_lock(handle) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def profile_lock(home: Path, wait_seconds: float = 0.0) -> Iterator[None]:
    """Hold the profile lock, waiting up to `wait_seconds`; raise `ProfileBusy` otherwise.

    The lock file sits beside the profile folder, not inside it, so the browser never sees it.
    """
    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / LOCK_NAME, "a+b")  # noqa: SIM115
    deadline = time.monotonic() + wait_seconds
    try:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise ProfileBusy("The browser profile is in use by another Run or login.")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()

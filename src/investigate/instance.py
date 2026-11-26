"""One tray per data folder, across processes.

The first `investigate tray` takes an OS file lock in the data folder and only then binds the
port, so two starts at once cannot both serve. The lock is released by the OS when the holder
exits or crashes, so it never goes stale. The holder publishes its URL in `tray.json` for the
second start to report.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from investigate import history
from investigate.profile_lock import _try_lock, _unlock
from investigate.service import fail_run

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


def holds_lock(home: Path) -> bool:
    """True when another process holds the host lock (a tray or `serve` is running)."""
    lock = InstanceLock(home)
    if lock.acquire():
        lock.release()
        return False
    return True


def running_url(home: Path) -> str | None:
    """The URL the running tray published, if it has got that far."""
    try:
        data = json.loads((home / INFO_NAME).read_text(encoding="utf-8"))
        url = data.get("url")
    except (OSError, ValueError, AttributeError):
        return None
    return url if isinstance(url, str) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def _run_alive(home: Path, run_id: str) -> bool:
    try:
        pid = int((home / "runs" / run_id / "running.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    # Under the lock this process has started nothing yet, so a pid equal to ours is a dead
    # host's pid that this one reused, not a Run in progress.
    return pid != os.getpid() and _pid_alive(pid)


def recover_interrupted(home: Path) -> list[str]:
    """Mark Runs left `queued` or `running` by a hard kill (Task Manager, logoff, shutdown) as
    failed. Call it only while holding the instance lock: no host can still be working on a
    queued Run, and a running one is spared only when another live process (an `investigate
    run` in another terminal) wrote its pid. Call it before the server takes Runs. Returns the ids
    it fixed."""
    fixed = []
    for entry in history.load_entries(home):
        if entry.status == "queued" or (
            entry.status == "running" and not _run_alive(home, entry.id)
        ):
            fail_run(home, entry.id, "Interrupted: the app stopped before this Run finished.")
            fixed.append(entry.id)
    return fixed

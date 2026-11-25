"""The History index: `index.json` in the data folder, one entry per Run.

Runs overlap, so every write takes a cross-process lock around load, modify and write. Reads
never modify anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path

INDEX_NAME = "index.json"
LOCK_NAME = "index.lock"
_LOCK_TIMEOUT = 30.0


@dataclass(frozen=True)
class HistoryEntry:
    id: str  # also the run folder name under `runs/`
    kind: str
    depth: str
    status: str  # running | completed | partial | failed
    title: str
    created: str  # ISO 8601, UTC
    session_id: str | None


_FIELDS = tuple(f.name for f in fields(HistoryEntry))


def index_path(home: Path) -> Path:
    return home / INDEX_NAME


def _read(path: Path) -> tuple[list[HistoryEntry], bool]:
    """(entries, damaged). Unknown keys are ignored and a bad entry is skipped on its own."""
    if not path.exists():
        return [], False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["runs"]
        if not isinstance(items, list):
            raise TypeError("runs is not a list")
    except (ValueError, KeyError, TypeError, OSError, RecursionError):
        return [], True
    entries = []
    for item in items:
        if isinstance(item, dict) and all(k in item for k in _FIELDS):
            entries.append(HistoryEntry(**{k: item[k] for k in _FIELDS}))
    return entries, False


def load_entries(home: Path) -> list[HistoryEntry]:
    """Read-only. A damaged index reads as empty and is left where it is."""
    return _read(index_path(home))[0]


@contextlib.contextmanager
def _locked(home: Path) -> Iterator[None]:
    """An exclusive lock shared between processes (and threads) using this data folder."""
    home.mkdir(parents=True, exist_ok=True)
    with open(home / LOCK_NAME, "a+b") as handle:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Could not lock {home / LOCK_NAME}") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(handle)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _replace(tmp: Path, path: Path) -> None:
    """`os.replace`, retried: on Windows a reader holding the file open makes it fail briefly."""
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


def upsert(home: Path, entry: HistoryEntry) -> None:
    """Add the entry, or replace the one with the same id."""
    path = index_path(home)
    with _locked(home):
        entries, damaged = _read(path)
        if damaged:
            # Keep the unreadable file for inspection under a name nothing else uses.
            os.replace(path, path.with_name(f"{INDEX_NAME}.broken-{uuid.uuid4().hex[:8]}"))
        entries = [e for e in entries if e.id != entry.id]
        entries.append(entry)
        tmp = path.with_name(f"{INDEX_NAME}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps({"runs": [asdict(e) for e in entries]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _replace(tmp, path)

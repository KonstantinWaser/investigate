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
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

INDEX_NAME = "index.json"
LOCK_NAME = "index.lock"
_LOCK_TIMEOUT = 30.0


@dataclass(frozen=True)
class HistoryEntry:
    """One Run. Fields without a default are required; every later field must have a default so
    that old index files keep loading."""

    id: str  # also the run folder name under `runs/`
    kind: str
    depth: str
    status: str  # queued | running | completed | partial | failed
    title: str
    created: str  # ISO 8601, UTC
    session_id: str | None
    verdicts: dict[str, int] | None = None  # Fact-checks: count per Verdict, else None
    followup_of: str | None = None  # id of the Run this one continues as a new Run
    promoted_from: str | None = None  # an Investigation promoted from this Fact-check's id
    extra: dict = field(default_factory=dict)  # keys written by a newer version, kept on rewrite


_NAMES = tuple(f.name for f in fields(HistoryEntry) if f.name != "extra")
_REQUIRED = tuple(
    f.name for f in fields(HistoryEntry) if f.name != "extra" and f.default is MISSING
)
_READ_ATTEMPTS = 5


def index_path(home: Path) -> Path:
    return home / INDEX_NAME


def _to_json(entry: HistoryEntry) -> dict:
    data = {name: getattr(entry, name) for name in _NAMES}
    return {**entry.extra, **data}


def _read_text(path: Path) -> str | None:
    """The file's text, retried on a transient OS error (a writer swapping the file on Windows).
    None when the file is gone. A persistent error is raised, never mistaken for damage."""
    for attempt in range(_READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError:
            raise
        except OSError:
            if attempt == _READ_ATTEMPTS - 1:
                raise
            time.sleep(0.05)
    return None


def _read(path: Path) -> tuple[list[HistoryEntry], bool]:
    """(entries, damaged). Only a parse failure is damage. Entries need just the fields without
    defaults; keys this version does not know are kept in `extra`."""
    try:
        text = _read_text(path)
    except UnicodeDecodeError:
        return [], True
    if text is None:
        return [], False
    try:
        items = json.loads(text)["runs"]
        if not isinstance(items, list):
            raise TypeError("runs is not a list")
    except (ValueError, KeyError, TypeError, RecursionError):
        return [], True
    entries = []
    for item in items:
        if isinstance(item, dict) and all(k in item for k in _REQUIRED):
            known = {k: item[k] for k in _NAMES if k in item}
            extra = {k: v for k, v in item.items() if k not in _NAMES and k != "extra"}
            entries.append(HistoryEntry(**known, extra=extra))
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


def _write(path: Path, entries: list[HistoryEntry]) -> None:
    tmp = path.with_name(f"{INDEX_NAME}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps({"runs": [_to_json(e) for e in entries]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _replace(tmp, path)


def upsert(home: Path, entry: HistoryEntry) -> None:
    """Add the entry, or replace the one with the same id."""
    path = index_path(home)
    with _locked(home):
        entries, damaged = _read(path)
        if damaged:
            # Keep the unreadable file for inspection under a name nothing else uses.
            _replace(path, path.with_name(f"{INDEX_NAME}.broken-{uuid.uuid4().hex[:8]}"))
        entries = [e for e in entries if e.id != entry.id]
        entries.append(entry)
        _write(path, entries)


def remove(home: Path, run_id: str) -> bool:
    """Drop the entry with this id. False when there was none (the index is left untouched)."""
    path = index_path(home)
    with _locked(home):
        entries, damaged = _read(path)
        kept = [e for e in entries if e.id != run_id]
        if damaged or len(kept) == len(entries):
            return False
        _write(path, kept)
        return True

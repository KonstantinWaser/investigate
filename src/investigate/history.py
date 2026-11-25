"""The History index: `index.json` in the data folder, one entry per Run."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

INDEX_NAME = "index.json"


@dataclass(frozen=True)
class HistoryEntry:
    id: str  # also the run folder name under `runs/`
    kind: str
    depth: str
    status: str  # running | completed | partial | failed
    title: str
    created: str  # ISO 8601, UTC
    session_id: str | None


def index_path(home: Path) -> Path:
    return home / INDEX_NAME


def load_entries(home: Path) -> list[HistoryEntry]:
    path = index_path(home)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [HistoryEntry(**item) for item in raw["runs"]]
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        # A damaged index must not stop new Runs. Keep it for inspection and start over.
        os.replace(path, path.with_suffix(".json.broken"))
        return []


def upsert(home: Path, entry: HistoryEntry) -> None:
    """Add the entry, or replace the one with the same id. Written atomically."""
    entries = [e for e in load_entries(home) if e.id != entry.id]
    entries.append(entry)
    path = index_path(home)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"runs": [asdict(e) for e in entries]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)

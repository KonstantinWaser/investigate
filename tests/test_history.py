"""History index: forward compatibility, read errors, backup rename, deletion."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from investigate import history

BASE = {
    "id": "a",
    "kind": "investigation",
    "depth": "quick",
    "status": "completed",
    "title": "t",
    "created": "2026-01-01T00:00:00+00:00",
    "session_id": None,
}


def _write(home, runs):
    home.mkdir(exist_ok=True)
    (home / "index.json").write_text(json.dumps({"runs": runs}), encoding="utf-8")


def _entry(i):
    return history.HistoryEntry(f"run-{i}", "investigation", "quick", "completed", "t", "c", None)


def test_entry_without_optional_fields_still_loads(home):
    _write(home, [BASE])
    (entry,) = history.load_entries(home)
    assert entry.id == "a"
    assert entry.verdicts is None


def test_unknown_keys_survive_a_rewrite(home):
    _write(home, [{**BASE, "future_field": {"x": 1}}])
    history.upsert(home, _entry(1))
    raw = json.loads((home / "index.json").read_text(encoding="utf-8"))
    kept = next(r for r in raw["runs"] if r["id"] == "a")
    assert kept["future_field"] == {"x": 1}
    assert "extra" not in kept


def test_verdicts_round_trip(home):
    history.upsert(
        home,
        history.HistoryEntry("v", "fact-check", "quick", "completed", "t", "c", None, {"wrong": 2}),
    )
    assert history.load_entries(home)[0].verdicts == {"wrong": 2}


def test_read_error_does_not_set_the_index_aside(home, monkeypatch):
    _write(home, [BASE])
    real = Path.read_text

    def failing(self, *a, **k):
        if self.name == "index.json":
            raise PermissionError("locked by someone")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", failing)
    monkeypatch.setattr(history, "_READ_ATTEMPTS", 2)
    with pytest.raises(OSError):
        history.upsert(home, _entry(1))
    monkeypatch.undo()
    assert not list(home.glob("index.json.broken*"))
    assert [e.id for e in history.load_entries(home)] == ["a"]


def test_transient_read_error_is_retried(home, monkeypatch):
    _write(home, [BASE])
    real = Path.read_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        if self.name == "index.json" and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError("briefly locked")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    history.upsert(home, _entry(1))
    monkeypatch.undo()
    assert {e.id for e in history.load_entries(home)} == {"a", "run-1"}
    assert not list(home.glob("index.json.broken*"))


def test_backup_rename_retries_on_permission_error(home, monkeypatch):
    home.mkdir()
    (home / "index.json").write_text("{nope", encoding="utf-8")
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        if ".broken-" in str(dst) and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("briefly locked")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    history.upsert(home, _entry(1))
    assert calls["n"] == 1
    assert len(list(home.glob("index.json.broken*"))) == 1


def test_remove_deletes_only_that_entry(home):
    for i in range(3):
        history.upsert(home, _entry(i))
    assert history.remove(home, "run-1") is True
    assert [e.id for e in history.load_entries(home)] == ["run-0", "run-2"]
    assert history.remove(home, "run-1") is False

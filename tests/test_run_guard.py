"""A Run that crashes inside `execute_run` must not stay `running` or `queued`."""

from __future__ import annotations

import pytest

from investigate import history
from investigate.config import Depth
from investigate.service import execute_run, reserve_run
from tests.fakes import FakeRunner


def test_crash_after_the_running_upsert_marks_the_entry_failed(home, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("prompt exploded")

    monkeypatch.setattr("investigate.service.build_prompt", boom)
    with pytest.raises(RuntimeError, match="prompt exploded"):
        execute_run("topic", Depth.QUICK, home=home, runner=FakeRunner())
    (entry,) = history.load_entries(home)
    assert entry.status == "failed"
    log = (home / "runs" / entry.id / "agent.log").read_text(encoding="utf-8")
    assert "prompt exploded" in log


def test_reserved_run_is_listed_as_queued_then_runs_in_the_same_folder(home):
    folder = reserve_run("topic", Depth.DEEP, home=home)
    (entry,) = history.load_entries(home)
    assert (entry.id, entry.status, entry.depth) == (folder.name, "queued", "deep")
    assert (folder / "item.md").read_text(encoding="utf-8") == "topic"

    result = execute_run("topic", Depth.DEEP, home=home, runner=FakeRunner(), reserved=folder)
    assert result.folder == folder
    (entry,) = history.load_entries(home)
    assert entry.status == "completed"

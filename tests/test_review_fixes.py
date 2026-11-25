"""Regression tests for the PR review findings."""

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from investigate import history
from investigate.config import ConfigError, Depth, config_path, load_config
from investigate.result import parse_result
from investigate.runner import ClaudeCodeRunner
from investigate.service import execute_run

WORKER = str(Path(__file__).with_name("upsert_worker.py"))
NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _entry(i: int, status: str = "completed") -> history.HistoryEntry:
    return history.HistoryEntry(f"run-{i}", "investigation", "quick", status, f"t{i}", "c", None)


# 1. runner start failures leave the error in agent.log


def test_missing_binary_error_is_in_agent_log(home, tmp_path):
    runner = ClaudeCodeRunner(command=[str(tmp_path / "no-such-binary")])
    result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
    assert result.status == "failed"
    assert "Could not start" in (result.folder / "agent.log").read_text(encoding="utf-8")


def test_claude_not_on_path_error_is_in_agent_log(home, monkeypatch):
    monkeypatch.setattr("investigate.runner.shutil.which", lambda _: None)
    result = execute_run("topic", Depth.QUICK, home=home, runner=ClaudeCodeRunner(), now=NOW)
    assert result.status == "failed"
    assert "not found on PATH" in (result.folder / "agent.log").read_text(encoding="utf-8")


# 3. concurrent upserts keep every entry


def test_concurrent_thread_upserts_lose_nothing(home):
    home.mkdir()
    errors = []

    def work(worker: int) -> None:
        try:
            for i in range(10):
                history.upsert(home, _entry(worker * 100 + i))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(history.load_entries(home)) == 60


def test_concurrent_process_upserts_lose_nothing(home):
    home.mkdir()
    procs = [subprocess.Popen([sys.executable, WORKER, str(home), str(w), "10"]) for w in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    assert len(history.load_entries(home)) == 40


def test_upsert_replaces_same_id(home):
    history.upsert(home, _entry(1, "running"))
    history.upsert(home, _entry(1, "completed"))
    (entry,) = history.load_entries(home)
    assert entry.status == "completed"


# 5. relative Context paths resolve against the data folder


def test_relative_context_path_resolved_against_data_folder(home):
    home.mkdir()
    config_path(home).write_text('context_files = ["ctx/me.md"]', encoding="utf-8")
    (resolved,) = load_config(home).context_files
    assert Path(resolved) == (home / "ctx" / "me.md").resolve()


def test_context_path_with_tilde_is_expanded(home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    home.mkdir()
    config_path(home).write_text('context_files = ["~/me.md"]', encoding="utf-8")
    (resolved,) = load_config(home).context_files
    assert Path(resolved) == (home / "me.md").resolve()


def test_prompt_and_permissions_use_the_same_resolved_path(home):
    from tests.fakes import FakeRunner

    home.mkdir()
    config_path(home).write_text('context_files = ["ctx/me.md"]', encoding="utf-8")
    runner = FakeRunner()
    execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
    (request,) = runner.requests
    expected = (home / "ctx" / "me.md").resolve()
    assert request.context_files == (expected,)
    assert str(expected) in request.prompt


# 6. malformed [depths]


@pytest.mark.parametrize(
    "text",
    [
        "depths = 5",
        "[depths]\nquick = 5",
        '[depths]\nquick = "fast"',
        "[depths]\nquick = []",
    ],
)
def test_malformed_depths_is_config_error(home, text):
    home.mkdir()
    config_path(home).write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(home)


# 7. tolerant, read-only-safe index reads


def _write_index(home, runs):
    home.mkdir(exist_ok=True)
    (home / "index.json").write_text(json.dumps({"runs": runs}), encoding="utf-8")


def test_unknown_keys_are_ignored_and_bad_entries_skipped(home):
    good = {
        "id": "a",
        "kind": "investigation",
        "depth": "quick",
        "status": "completed",
        "title": "t",
        "created": "c",
        "session_id": None,
        "future_field": 1,
    }
    _write_index(home, [good, {"id": "broken"}, "nonsense", {**good, "id": "b"}])
    assert [e.id for e in history.load_entries(home)] == ["a", "b"]


def test_reading_a_damaged_index_does_not_rename_it(home):
    home.mkdir()
    path = home / "index.json"
    path.write_text("{nope", encoding="utf-8")
    assert history.load_entries(home) == []
    assert path.read_text(encoding="utf-8") == "{nope"
    assert not list(home.glob("index.json.broken*"))


def test_writing_over_a_damaged_index_keeps_a_unique_backup(home):
    home.mkdir()
    for n in range(2):
        (home / "index.json").write_text(f"{{nope{n}", encoding="utf-8")
        history.upsert(home, _entry(n))
    backups = sorted(home.glob("index.json.broken*"))
    assert len(backups) == 2
    assert {b.read_text(encoding="utf-8") for b in backups} == {"{nope0", "{nope1"}


def test_cli_history_does_not_touch_a_damaged_index(home, capsys):
    from investigate.cli import main

    home.mkdir()
    (home / "index.json").write_text("{nope", encoding="utf-8")
    assert main(["history"]) == 0
    assert (home / "index.json").read_text(encoding="utf-8") == "{nope"


# 8. deeply nested JSON


def test_deeply_nested_json_is_malformed_not_a_crash():
    text = "```json\n" + "[" * 200000 + "\n```"
    parsed = parse_result(text)
    assert parsed.structured is None
    assert parsed.problems

"""`docs/runner.md` is the contract for a new agent CLI adapter. These tests keep it true to the
code: the documented fields are the dataclass fields, the documented methods are the Protocol's,
and the runners we ship satisfy it."""

from __future__ import annotations

import dataclasses
import re
import shutil
from pathlib import Path

import pytest

from investigate.config import PermissionMode
from investigate.runner import ClaudeCodeRunner, Runner, RunnerOutcome, RunRequest
from tests.fakes import FakeRunner

DOC = Path(__file__).resolve().parent.parent / "docs" / "runner.md"


def _section(heading: str) -> str:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, f"docs/runner.md has no '## {heading}' section"
    return match.group(1)


def _documented_names(heading: str) -> list[str]:
    """First-column names of the table rows in a section, in the order written."""
    return re.findall(r"^\| `([A-Za-z_]+)` \|", _section(heading), re.M)


def test_documented_run_request_fields_are_the_dataclass_fields():
    assert _documented_names("RunRequest") == [f.name for f in dataclasses.fields(RunRequest)]


def test_documented_outcome_fields_are_the_dataclass_fields():
    assert _documented_names("RunnerOutcome") == [f.name for f in dataclasses.fields(RunnerOutcome)]


def test_documented_methods_are_the_protocol_members():
    members = set(Runner.__annotations__) | {
        n for n, v in vars(Runner).items() if callable(v) and not n.startswith("_")
    }
    assert members == {"name", "run", "resume"}
    assert set(_documented_names("Runner")) == members | {"stop"}


def test_every_permission_mode_is_documented():
    section = _section("Permission modes")
    for mode in PermissionMode:
        assert f"`{mode.value}`" in section


def test_shipped_runners_satisfy_the_protocol():
    assert isinstance(ClaudeCodeRunner(), Runner)
    assert isinstance(FakeRunner(), Runner)


def test_a_class_missing_resume_does_not_satisfy_the_protocol():
    class Partial:
        name = "partial"

        def run(self, request):  # pragma: no cover
            raise NotImplementedError

    assert not isinstance(Partial(), Runner)


def test_claude_runner_reports_a_missing_cli_in_the_outcome_instead_of_raising(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    request = RunRequest(
        prompt="p",
        workdir=tmp_path,
        session_id="s",
        timeout_seconds=1,
        model=None,
        permission_mode=PermissionMode.ALLOWLIST,
    )
    outcome = ClaudeCodeRunner().run(request)
    assert outcome.ok is False and outcome.error
    assert outcome.session_id == "s"


@pytest.mark.parametrize("runner", [ClaudeCodeRunner(), FakeRunner()])
def test_runner_name_is_a_non_empty_string(runner):
    assert isinstance(runner.name, str) and runner.name

import json
import sys
import time
from pathlib import Path

import pytest

from investigate.config import PermissionMode
from investigate.runner import ClaudeCodeRunner, RunRequest, build_command

STUB = str(Path(__file__).with_name("stub_claude.py"))


def _request(workdir, **overrides):
    values = dict(
        prompt="hello",
        workdir=workdir,
        session_id="11111111-1111-1111-1111-111111111111",
        timeout_seconds=20,
        model=None,
        permission_mode=PermissionMode.BYPASS,
    )
    values.update(overrides)
    return RunRequest(**values)


def _runner():
    return ClaudeCodeRunner(command=[sys.executable, STUB])


def test_command_bypass_isolates_mcp_and_settings(tmp_path):
    cmd = build_command(["claude"], _request(tmp_path))
    assert cmd[:2] == ["claude", "-p"]
    assert "--bare" not in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--session-id") + 1] == "11111111-1111-1111-1111-111111111111"
    assert cmd[cmd.index("--mcp-config") + 1] == "mcp.json"
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--model" not in cmd
    assert "hello" not in cmd  # the prompt goes in on stdin


def test_command_allowlist_and_model(tmp_path):
    ctx = tmp_path / "ctx" / "me.md"
    cmd = build_command(
        ["claude"],
        _request(
            tmp_path,
            model="opus",
            permission_mode=PermissionMode.ALLOWLIST,
            context_files=(ctx,),
        ),
    )
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    allowed = cmd[cmd.index("--allowedTools") + 1 :]
    assert "WebSearch" in allowed and "WebFetch" in allowed
    assert any(a.startswith("Read(//") and a.endswith("me.md)") for a in allowed)
    assert "Bash" not in allowed
    assert str(ctx.parent.resolve()) in cmd  # --add-dir


def test_runner_success_parses_json_and_session(tmp_path):
    outcome = _runner().run(_request(tmp_path))
    assert outcome.ok and not outcome.timed_out
    assert outcome.text == "prompt chars: 5"
    assert outcome.session_id == "s-1"
    assert json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_runner_nonzero_exit_is_failure_with_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "fail")
    outcome = _runner().run(_request(tmp_path))
    assert not outcome.ok
    assert "code 3" in outcome.error and "boom" in outcome.error
    assert "boom" in outcome.log


def test_runner_agent_error_is_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "agent_error")
    outcome = _runner().run(_request(tmp_path))
    assert not outcome.ok
    assert "rate limited" in outcome.error


def test_runner_timeout_kills_process_and_keeps_files(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang")
    started = time.monotonic()
    outcome = _runner().run(_request(tmp_path, timeout_seconds=2))
    assert time.monotonic() - started < 30
    assert outcome.ok and outcome.timed_out
    assert outcome.session_id == "11111111-1111-1111-1111-111111111111"
    assert (tmp_path / "result.md").read_text(encoding="utf-8") == "partial text"
    assert "Depth timeout" in outcome.log


def test_runner_missing_command_is_failure(tmp_path):
    outcome = ClaudeCodeRunner(command=[str(tmp_path / "no-such-binary")]).run(_request(tmp_path))
    assert not outcome.ok
    assert "Could not start" in outcome.error


@pytest.mark.parametrize("found", [None])
def test_runner_claude_not_on_path(tmp_path, monkeypatch, found):
    monkeypatch.setattr("investigate.runner.shutil.which", lambda _: found)
    outcome = ClaudeCodeRunner().run(_request(tmp_path))
    assert not outcome.ok
    assert "not found on PATH" in outcome.error

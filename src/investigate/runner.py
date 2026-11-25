"""The runner interface and the Claude Code headless runner.

A runner executes one prompt in a working directory and reports what happened. It enforces the
wall-clock timeout, never raises (failures come back in the outcome), and does not touch the
History or render anything. A new agent CLI is one new class implementing `Runner`.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from investigate import browser
from investigate.config import PermissionMode

MCP_CONFIG_NAME = "mcp.json"


@dataclass(frozen=True)
class RunRequest:
    prompt: str
    workdir: Path  # the run folder; the agent runs here and writes only here
    session_id: str  # a UUID the runner passes to the CLI so it is known even after a kill
    timeout_seconds: float
    model: str | None
    permission_mode: PermissionMode
    context_files: tuple[Path, ...] = ()
    browser_profile: Path | None = None  # set: Playwright MCP drives this persistent profile


@dataclass(frozen=True)
class RunnerOutcome:
    ok: bool  # False when the runner itself failed (not started, non-zero exit, agent error)
    timed_out: bool
    text: str  # the agent's final message; may be empty or partial
    session_id: str | None
    log: str  # raw transcript for `agent.log`
    error: str | None = None


class Runner(Protocol):
    name: str

    def run(self, request: RunRequest) -> RunnerOutcome: ...


def _rule_path(path: Path) -> str:
    """A path in Claude Code permission-rule form (`//c/Users/...` on Windows)."""
    posix = path.resolve().as_posix()
    if len(posix) > 1 and posix[1] == ":":
        posix = f"/{posix[0].lower()}{posix[2:]}"
    return "/" + posix


def build_command(command: list[str], request: RunRequest) -> list[str]:
    """The full CLI command. The prompt goes in on stdin, not on the command line."""
    cmd = [
        *command,
        "-p",
        "--output-format",
        "json",
        "--session-id",
        request.session_id,
        # Our own MCP config and only user-level settings: do not pick up hooks or MCP servers
        # from wherever the user launched us.
        "--mcp-config",
        MCP_CONFIG_NAME,
        "--strict-mcp-config",
        "--setting-sources",
        "user",
        # User-level hooks still load with `--setting-sources user`; switch every hook off.
        "--settings",
        json.dumps({"disableAllHooks": True}),
    ]
    if request.model:
        cmd += ["--model", request.model]
    if request.permission_mode is PermissionMode.BYPASS:
        cmd += ["--permission-mode", "bypassPermissions"]
    else:
        run_folder = _rule_path(request.workdir)
        # Writes are anchored to the run folder with an absolute `//` pattern. Context files are
        # named one by one, no --add-dir, so the agent cannot read their sibling files. "Agent"
        # (the subagent tool) is allowed so deep Runs can fan out per source class.
        # `Edit` rules cover every file-editing tool, Write included; a separate `Write(...)`
        # rule did not match in practice. The pattern is anchored to the run folder.
        allowed = ["WebSearch", "WebFetch", "Agent", f"Edit({run_folder}/**)"]
        allowed += [f"Read({_rule_path(p)})" for p in request.context_files]
        allowed.append(f"Read({run_folder}/attachments/**)")  # the Item's attached files
        if request.browser_profile is not None:
            allowed += browser.ALLOWED_TOOLS
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", *allowed]
    if request.browser_profile is not None:
        cmd += ["--disallowedTools", *browser.DENIED_TOOLS]
    return cmd


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    proc.kill()


class ClaudeCodeRunner:
    """Claude Code headless: `claude -p` with JSON output. No `--bare` (it ignores login)."""

    name = "claude-code"

    def __init__(self, command: list[str] | None = None) -> None:
        self._command = command

    def _resolve(self) -> list[str] | None:
        if self._command is not None:
            return self._command
        found = shutil.which("claude")
        return [found] if found else None

    def run(self, request: RunRequest) -> RunnerOutcome:
        command = self._resolve()
        if command is None:
            return RunnerOutcome(
                ok=False,
                timed_out=False,
                text="",
                session_id=request.session_id,
                log="",
                error="The 'claude' command was not found on PATH. Install Claude Code and log in.",
            )
        servers = {}
        if request.browser_profile is not None:
            servers[browser.SERVER_NAME] = browser.playwright_server(request.browser_profile)
        (request.workdir / MCP_CONFIG_NAME).write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )
        argv = build_command(command, request)
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=request.workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            return RunnerOutcome(
                False, False, "", request.session_id, "", f"Could not start the runner: {exc}"
            )

        timed_out = False
        try:
            stdout, stderr = proc.communicate(request.prompt, timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            stdout, stderr = proc.communicate()
        stdout, stderr = stdout or "", stderr or ""
        log = f"$ {' '.join(argv)}\n\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"

        if timed_out:
            return RunnerOutcome(
                ok=True,
                timed_out=True,
                text=_final_text(stdout),
                session_id=request.session_id,
                log=log + f"\nStopped after {request.timeout_seconds:g} s (Depth timeout).\n",
            )
        text, session_id, agent_error = _parse_stdout(stdout)
        error = agent_error
        if proc.returncode != 0 and not error:
            error = f"The runner exited with code {proc.returncode}. {stderr.strip()[:500]}".strip()
        return RunnerOutcome(
            ok=error is None,
            timed_out=False,
            text=text,
            session_id=session_id or request.session_id,
            log=log + (f"\nERROR: {error}\n" if error else ""),
            error=error,
        )


def _parse_stdout(stdout: str) -> tuple[str, str | None, str | None]:
    """(text, session id, error) from `--output-format json` stdout."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip(), None, None
    if not isinstance(data, dict):
        return stdout.strip(), None, None
    text = data.get("result")
    text = text if isinstance(text, str) else ""
    session_id = data.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None
    error = text or "The agent reported an error." if data.get("is_error") else None
    return text, session_id, error


def _final_text(stdout: str) -> str:
    return _parse_stdout(stdout)[0] if stdout.strip().startswith("{") else ""

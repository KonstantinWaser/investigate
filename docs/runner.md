# Runner interface

A runner executes one prompt with an agent CLI and reports what happened. A new CLI (Codex, Gemini and so on) is one class, in `src/investigate/runner.py` or a file next to it, that meets the contract below. The code is the source of truth: `Runner`, `RunRequest` and `RunnerOutcome` in `runner.py`. `tests/test_runner_contract.py` fails when this page and those definitions disagree, so change both together.

A runner does not touch the History, does not render anything and does not parse the result. The caller (`service.execute_run`, `followup`) does all of that.

## Runner

A class with a `name` and two methods. `stop` is optional. A runner is passed to the host (`investigate tray`, `serve`, `run`, `inbox`) in place of `ClaudeCodeRunner()`; `Runner` is a runtime-checkable Protocol, so `isinstance(obj, Runner)` checks the names.

| Member | Contract |
|---|---|
| `name` | A short, constant string such as `claude-code`. |
| `run` | `run(request) -> RunnerOutcome`. Starts a new agent session with `request.session_id`. |
| `resume` | `resume(request) -> RunnerOutcome`. Continues the session `request.session_id` with `request.prompt` (a Follow-up). |
| `stop` | Optional, `stop() -> None`. Ends every running agent and its child processes and refuses to start more. Called once when the app quits and a Run is still going. A Run stopped this way must come back as partial (`ok=True`, `timed_out=True`). |

Rules for every method that runs the agent:

- It never raises. A missing CLI, a start failure or an agent error comes back as `RunnerOutcome(ok=False, error=...)`. (A runner that raises anyway is caught by the caller and the Run is marked failed, but that is the last resort.)
- It blocks until the agent is done, and may be called from several threads at once (the queue runs up to `parallel_runs` Runs together). Keep per-call state out of the instance, or lock it.
- It enforces `request.timeout_seconds` as a wall-clock limit, kills the agent and its children on expiry, and returns what the agent had written so far with `timed_out=True` and `ok=True`.
- It runs the agent with `request.workdir` as the working directory, passes the prompt on stdin or in another way that does not put it on a visible command line, and uses `request.model` when it is set (None means the CLI's default model).

## RunRequest

A frozen dataclass the caller builds once per call.

| Field | Meaning |
|---|---|
| `prompt` | The full instruction text. For `resume`, only the follow-up question and its instructions. |
| `workdir` | The run folder. The agent runs here, and writes only here. The prompt tells it to write its result to `result.md` in this folder (and, for a Follow-up, to `followup-<n>.md`). |
| `session_id` | A UUID the caller makes. For `run`, the runner starts the session under this id if the CLI allows choosing one, so the id is known even after a kill. For `resume`, it is the id of the session to continue. |
| `timeout_seconds` | Wall-clock limit in seconds, from the Depth. |
| `model` | Model name for the CLI, or None for its default. |
| `permission_mode` | A `PermissionMode`, see below. |
| `context_files` | Absolute paths of the user's Context files. The agent may read these and nothing else outside the run folder. |
| `browser_profile` | When set, the agent gets a browser driven through Playwright MCP on this persistent profile. Hold no lock on it: the caller holds the profile lock for the whole call. |

## RunnerOutcome

A frozen dataclass the runner returns.

| Field | Meaning |
|---|---|
| `ok` | False when the runner itself failed: not started, non-zero exit, agent error. A timeout is not a failure: it is `ok=True` with `timed_out=True`. |
| `timed_out` | True when the Run was cut short (timeout or `stop`). The Report is marked partial. |
| `text` | The agent's final message. May be empty or partial. Used as the result when the agent did not write `result.md`. |
| `session_id` | The session id the CLI reports, or the request's id when it reports none. Stored so a Follow-up can resume the session. |
| `log` | The raw transcript (command line without the prompt, stdout, stderr). Saved as `agent.log`. |
| `error` | A one-line reason when `ok` is False, else None. |
| `resume_unavailable` | True only for a failed `resume` whose session is unknown or gone. The caller then starts a new Run with the question instead. |

## Permission modes

Every runner maps both modes. The user chooses the mode in `config.toml` (`permission_mode`).

| Mode | Meaning for the agent |
|---|---|
| `bypass` | No permission prompts at all. The agent can run any tool, read and write anywhere the user can, and use the network. This is the default and the setting that needs trust in the model and the content it reads. |
| `allowlist` | Web search, web fetch, subagents, reading the Context files (by exact path) and the Item's attachments, and writing inside `workdir` only. Everything else is denied without asking, because a headless run cannot answer a prompt. If the CLI cannot express this, the runner must refuse the mode with `ok=False` and an error, never fall back to `bypass`. |

In both modes the browser tools that run code or read files on the host (`evaluate`, `run_code_unsafe`, `file_upload`) stay denied, and every hook of the user's CLI settings is switched off, so nothing from the machine's configuration changes what a Run does.

## Where output goes

- The agent writes `result.md` (a Markdown body plus one JSON block, schema in `result.py`) in `workdir`. The caller parses it. If the file is missing or empty, `RunnerOutcome.text` is used.
- The runner writes nothing else into the run folder except what the CLI needs (the Claude Code runner writes `mcp.json`). `agent.log`, `run.json`, `result.json` and `report.html` belong to the caller.
- Anything that belongs to the user stays out of `log` where the CLI allows it (do not echo environment variables or credentials).

## A minimal adapter

```python
class MyCliRunner:
    name = "my-cli"

    def run(self, request: RunRequest) -> RunnerOutcome: ...

    def resume(self, request: RunRequest) -> RunnerOutcome: ...

    def stop(self) -> None:  # optional
        ...
```

To try it, pass it to `main(argv, runner=MyCliRunner())` in `cli.py`, or wire it to a config choice. Selecting a runner by config is not built yet: today `ClaudeCodeRunner` is the only one the command line constructs. Check a new adapter with the same asserts as `tests/test_runner_contract.py` (`isinstance(runner, Runner)`), and with a Run at `quick` depth.

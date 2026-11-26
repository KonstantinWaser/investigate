# investigate

A personal research desk. Vocabulary is in `GLOSSARY.md`, the agreed design in `docs/design.md`. Use those terms in code and docs.

## Verify

```
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests use a fake runner and an `INVESTIGATE_HOME` pointing at a temp folder. They need no network and no Claude login. Never let a test touch the real home folder.

`tests/test_no_personal_data.py` scans tracked files for user-home paths and email addresses. Set `INVESTIGATE_DENYLIST` to a file outside the repo (one word per line) to also check your own name; never commit such words.

## Layout

- `src/investigate/config.py`: data folder (`INVESTIGATE_HOME`, default `~/.investigate`) and `config.toml`.
- `src/investigate/runner.py`: the `Runner` interface and the Claude Code runner. A new agent CLI is one new class here (or one file).
- `src/investigate/prompt.py`: the research prompt that enforces the method in `docs/design.md`.
- `src/investigate/result.py`: parses the agent's structured result (Markdown body plus a JSON block). Never raises.
- `src/investigate/report.py`: renders the self-contained HTML Report. Escapes all agent text.
- `src/investigate/attachments.py`: copies, sanitises and size-checks an Item's files and images into `<run folder>/attachments/`. Takes paths or in-memory `Upload`s.
- `src/investigate/browser.py`: the persistent browser profile (`browser-profile/` in the data folder), the Playwright MCP server entry and `login`.
- `src/investigate/profile_lock.py`: the cross-process lock on that profile. Anything that opens the profile holds it.
- `src/investigate/history.py`: the History index (`index.json` in the data folder). New `HistoryEntry` fields need a default so old indexes keep loading; unknown keys are preserved on rewrite.
- `src/investigate/server.py`, `webpages.py`: the localhost History and Report server (stdlib `http.server`; `HistoryServer` starts and stops from code; `investigate serve`). Loopback only unless `--allow-remote`; POST and DELETE check Host and Origin. `POST /runs/<id>/promote` (the Fact-check Report's form, no script) starts a linked Investigation; asking twice returns the first.
- `src/investigate/followup.py`: Follow-ups. Resumes the Run's agent session (`Runner.resume`), stores answers in `followups.json`, mirrors them into `result.json` and renders them into `report.html` between comment markers; one at a time per Run (409 otherwise). A Follow-up is a `RunQueue` job and takes a parallel slot (resuming spawns an agent); its fallback Run is reserved with `reserve_run` (linked by `followup_of` from then on) and runs inside that slot. The in-progress state is in memory only, so a hard kill leaves nothing stuck; `recover_interrupted` fails a half-made fallback Run and keeps its link. Without a session id, or when the resume fails with an unknown session, the question becomes a new Run (`followup_of` links it). The served Report gets its ask box and a meta-refresh progress notice injected at serve time, so the file view stays plain.
- `src/investigate/service.py`: `execute_run`, the one path that turns an Item into a run folder. `reserve_run` makes the folder and lists the Run as `queued` first. A crash after the Run is listed leaves it `failed`.
- `src/investigate/runqueue.py`: FIFO queue with the parallel limit (`parallel_runs` in `config.toml`, 1 to 3). `HistoryServer.submit` goes through it, so `serve` and the tray share it.
- `src/investigate/capture.py`: the quick-capture page (`/capture`). Paste and drop are handled by the page, submit goes to `POST /api/runs`.
- `src/investigate/instance.py`: the cross-process host lock (`tray.lock`, `tray.json`) in the data folder; the tray and `serve` both take it. `recover_interrupted` runs under it at start-up, before the server listens, and fails Runs a hard kill left `queued` or `running` (a live `running.pid` of another process in the run folder spares a Run; this host's own pid does not, and `execute_run` removes the file when a Run ends).
- `src/investigate/tray.py`: `TrayApp` (menu as data, toasts, quit) and `run_tray`. No GUI imports; tested headless.
- `src/investigate/tray_gui.py`: the only file that imports pystray, Pillow and winotify (the `tray` extra). Keep it thin.
- `src/investigate/settings.py`, `settingspage.py`: the Settings page (`/settings`). `settings.py` has the field list (`SECTIONS`), form parsing and the comment-preserving, atomic write (`tomlkit`). Validation is `config.validate_raw`, the same code `load_config` runs. A new setting: its rule in `validate_raw`, its default in `DEFAULT_CONFIG_TOML`, one `Field` in `SECTIONS`. The page is script-free (CSP by hash for its style).
- `src/investigate/hotkeys.py`: the `HotkeyBackend` seam; `RegisterHotKey` on Windows, `pynput` elsewhere. `autostart.py`: the `Autostart` seam (Run registry key, LaunchAgent, XDG file). Tests use fakes or temp folders and never the real registry, Startup folder or home.
- `src/investigate/factcheck.py`: the Fact-check kind's prompt text. A Fact-check is `execute_run(kind="fact-check")`; `result.py` parses `claims` for it and the Verdict counts go into the HistoryEntry `verdicts`.
- `src/investigate/inbox.py`: the generic Inbox. An `InboxSource` lists and fetches items, `check_inbox` turns unseen ones into Fact-checks through an enqueuer, and an id is recorded in `inbox/<source>.json` only after its Run exists. Also the `[inbox]` config, the processed ids, the last-check time and the cross-process check lock. A new source (the Instagram Collection) is one `InboxSource` plus a line in `default_sources`.
- `src/investigate/instagram.py`: the Instagram source: Collection parsing and paging (`InstagramApi` on a `get_json` seam, tested on fixtures in `tests/fixtures/instagram/`), `browser_reader` (profile lock, `LoginRequired`, `SourceBusy`) and `InstagramSource`. Playwright is the optional `instagram` extra.
- `src/investigate/youtube.py`, `transcribe.py`: the YouTube source behind a `YouTubeClient` seam (yt-dlp; tests use a fake and never touch the network) and the optional ffmpeg plus faster-whisper `Transcriber`.
- `src/investigate/inbox_timer.py`: `InboxScheduler`, the tray's twice-a-day timer with catch-up and an injected clock.
- `src/investigate/setupweb.py`, `setuppage.py`: the browser setup page (`/setup`, `POST /setup/<action>`). `setupweb.py` is the logic: `gather_status`, Context uploads into `<data folder>/contexts/` (sanitised names, size cap, an existing file is kept unless `replace`), `set_inbox` (checked by `inbox.parse_inbox_config`, the code `load_inbox_config` runs), the toggles, and starting `claude auth login`, `winget install` and the browser login without waiting. Every outside effect is a `SetupActions` field (`HistoryServer(setup=...)`); tests use fakes. `setuppage.py` is script-free HTML with its style allowed by hash. Config writes go through `setup.edit_config` and, from the server, `HistoryServer.edit_setup_config` (Settings lock, then `on_config_changed`).
- `src/investigate/setup.py`: `investigate setup`, the guided first run. `agent_status` is the shared install and login check (`claude auth status` with stdin closed and a timeout). Every outside effect is a `SetupIO` field so tests use fakes; it never overwrites an existing config or Context file.
- `src/investigate/firstrun.py`: `investigate first-run` and `uninstall`. Every outside effect (Start menu shortcuts through PowerShell `WScript.Shell`, the login entry, detached start, browser, stopping the tray through `POST /api/quit`, uv) is an `InstallIO` field; tests use fakes and never touch the real Start menu, registry or `uv`. The tray's start command is the windowless `investigate-tray` gui-script (`autostart.startup_command` finds it next to the interpreter). Detached processes use `CREATE_NO_WINDOW` without `DETACHED_PROCESS` (together they stop a child from running).
- `install.cmd` (repo root): the file a Windows user double-clicks (uv, optional Claude Code, `uv tool install` from the repository zip, `investigate first-run`). Stored with CRLF (`.gitattributes` says `-text`, so the blob keeps them and a raw download works). Keep it small; `tests/test_install_cmd.py` checks it against `pyproject.toml` and the CLI.
- `src/investigate/cli.py`: the `investigate` entry point.
- `docs/runner.md`: the contract for a new agent CLI adapter. `tests/test_runner_contract.py` fails when it drifts from `runner.py`; change both together.

## Rules

- Run folder layout: `item.md`, `result.md` (as the agent wrote it), `result.json` (parsed, only when valid), `report.html`, `agent.log`, `run.json`, `mcp.json`, and `attachments/` when the Item has files, and after Follow-ups `followups.json` and `followup-N.md`. An Inbox Fact-check with notes (a missing transcript, say) also gets `notes.json`, written when the Run is reserved so the notes survive a Run that never starts; the same notes go into `run.json` and the Report once it runs.
- The structured result schema is versioned in `result.py` (`SCHEMA_VERSION`). Extend it additively.
- Allowlist mode (`runner.py`) allows WebSearch, WebFetch, the `Agent` subagent tool (deep Runs fan out per source class), an `Edit(//<run folder>/**)` rule (Edit covers the Write tool; a `Write(...)` rule did not match when tried), and Read of each Context file by exact path. It does not use `--add-dir`. Every Run also passes `--settings '{"disableAllHooks": true}'`.
- Nothing personal goes into the repo: no names, user paths or Context content.

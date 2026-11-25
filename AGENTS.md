# investigate

A personal research desk. Vocabulary is in `GLOSSARY.md`, the agreed design in `docs/design.md`. Use those terms in code and docs.

## Verify

```
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests use a fake runner and an `INVESTIGATE_HOME` pointing at a temp folder. They need no network and no Claude login. Never let a test touch the real home folder.

## Layout

- `src/investigate/config.py`: data folder (`INVESTIGATE_HOME`, default `~/.investigate`) and `config.toml`.
- `src/investigate/runner.py`: the `Runner` interface and the Claude Code runner. A new agent CLI is one new class here (or one file).
- `src/investigate/prompt.py`: the research prompt that enforces the method in `docs/design.md`.
- `src/investigate/result.py`: parses the agent's structured result (Markdown body plus a JSON block). Never raises.
- `src/investigate/report.py`: renders the self-contained HTML Report. Escapes all agent text.
- `src/investigate/attachments.py`: copies, sanitises and size-checks an Item's files and images into `<run folder>/attachments/`. Takes paths or in-memory `Upload`s.
- `src/investigate/history.py`: the History index (`index.json` in the data folder).
- `src/investigate/browser.py`: the persistent browser profile (`browser-profile/` in the data folder), the Playwright MCP server entry and `login`.
- `src/investigate/profile_lock.py`: the cross-process lock on that profile. Anything that opens the profile holds it.
- `src/investigate/service.py`: `execute_run`, the one path that turns an Item into a run folder.
- `src/investigate/cli.py`: the `investigate` entry point.

## Rules

- Run folder layout: `item.md`, `result.md` (as the agent wrote it), `result.json` (parsed, only when valid), `report.html`, `agent.log`, `run.json`, `mcp.json`, and `attachments/` when the Item has files.
- The structured result schema is versioned in `result.py` (`SCHEMA_VERSION`). Extend it additively.
- Allowlist mode (`runner.py`) allows WebSearch, WebFetch, the `Agent` subagent tool (deep Runs fan out per source class), an `Edit(//<run folder>/**)` rule (Edit covers the Write tool; a `Write(...)` rule did not match when tried), and Read of each Context file by exact path. It does not use `--add-dir`. Every Run also passes `--settings '{"disableAllHooks": true}'`.
- Nothing personal goes into the repo: no names, user paths or Context content.

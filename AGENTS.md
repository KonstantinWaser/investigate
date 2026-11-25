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
- `src/investigate/history.py`: the History index (`index.json` in the data folder).
- `src/investigate/service.py`: `execute_run`, the one path that turns an Item into a run folder.
- `src/investigate/cli.py`: the `investigate` entry point.

## Rules

- Run folder layout: `item.md`, `result.md` (as the agent wrote it), `result.json` (parsed, only when valid), `report.html`, `agent.log`, `run.json`, `mcp.json`.
- The structured result schema is versioned in `result.py` (`SCHEMA_VERSION`). Extend it additively.
- Nothing personal goes into the repo: no names, user paths or Context content.

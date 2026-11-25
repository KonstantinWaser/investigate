# investigate

A tray app that takes anything you paste (text, links, files, images) or save (an Instagram Collection, a YouTube playlist), has an agent CLI research or fact-check it in depth, and gives you a cited HTML report placed in the context of your own projects.

Work in progress. The agreed design is in `docs/design.md`, the vocabulary in `GLOSSARY.md`, and what already exists elsewhere in `docs/prior-art.md`.

MIT licensed.

## Use

```
uv run investigate run "text and/or URLs" --depth quick|standard|deep
uv run investigate history
```

Each Run gets a folder under `~/.investigate/runs/` with the Report (`report.html`). Set `INVESTIGATE_HOME` to use another data folder. Settings are in `config.toml` in that folder. Needs the `claude` CLI on PATH and logged in.

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

## Tray

```
uv sync --extra tray
uv run investigate tray
```

Shows a tray icon and serves the History on `http://127.0.0.1:8765/`. Left-click opens the quick-capture window (type, paste text, links or images, drop files, pick a Depth). Finished Runs raise a toast; on Windows, clicking it opens the Report. A second `investigate tray` exits and says where the first one is. Runs go through a queue; `parallel_runs` in `config.toml` (1 to 3, default 2) sets how many run at once. Quit gives running Runs 10 seconds, then stops them and marks them partial.

The tray menu's Settings item opens `http://127.0.0.1:8765/settings`: models and timeouts per Depth, the parallel limit, permission mode, Contexts, report language, attachment limit, the logged-in browser, the quick-capture hotkey (default Ctrl+Alt+I, empty turns it off) and start at login. Saving writes `config.toml` (comments and keys it does not know are kept) and applies at once: no setting needs a restart, and Runs already started keep the settings they began with. Editing `config.toml` by hand works too, but the parallel limit, hotkey and start at login only follow the Settings page (or a restart). If a hotkey cannot be registered, a notice says so.

Start at login registers the interpreter that runs the tray, as `<python> -m investigate.cli tray` (`pythonw.exe` on Windows, so no console window opens): a value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` on Windows, a LaunchAgent in `~/Library/LaunchAgents` on macOS, an autostart `.desktop` file in `~/.config/autostart` on Linux. The entry is checked at every tray start and rewritten if the install moved. It starts the tray with the default data folder, so a custom `INVESTIGATE_HOME` has to be set for your whole login session. The hotkey uses `RegisterHotKey` on Windows (an already used combination is reported) and `pynput` elsewhere (needs the Accessibility permission on macOS, no Wayland, and cannot tell a conflict).

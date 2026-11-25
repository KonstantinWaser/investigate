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

Start at login registers the interpreter that runs the tray, as `<python> -m investigate.cli tray` (`pythonw.exe` on Windows, so no console window opens): a value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` on Windows, a LaunchAgent in `~/Library/LaunchAgents` on macOS, an autostart `.desktop` file in `~/.config/autostart` on Linux. The entry belongs to one data folder: the default folder uses the name `Investigate`, any other folder (`INVESTIGATE_HOME` or `investigate tray --home <path>`) gets `Investigate-<hash>` and its command carries `--home`, so a tray on another folder never touches your real entry. The entry is checked at every tray start and rewritten if the install moved, except that an install inside the temp folder or a git worktree never takes over an existing entry (a notice says so). The hotkey uses `RegisterHotKey` on Windows (an already used combination is reported) and `pynput` elsewhere (needs the Accessibility permission on macOS, no Wayland, and cannot tell a conflict).
## Inbox: Fact-checks of a YouTube playlist

Save videos into a playlist. `investigate inbox` reads it, and each video it has not seen before becomes a Fact-check Run: every claim gets a Verdict (Confirmed, Partly true, Outdated, Unverified, Wrong) with a one-sentence reason and Citations, plus the Relevance to each Context. Each Report has a "Promote to Investigation" button that starts an Investigation on the same Item, linked to the Fact-check. Nothing is ever changed on YouTube. Processed video ids are kept in `inbox/youtube.json` in the data folder, so running it again does nothing new.

```toml
# config.toml
[inbox]
check_times = ["08:00", "18:00"]   # the tray checks at these local times (default)
depth = "standard"                 # Depth of the Fact-checks

[inbox.youtube]
playlist = "PLxxxxxxxxxxxxxxxx"    # a playlist id, or a URL with ?list=
cookies = "youtube-cookies.txt"    # only for a private playlist (optional)
subtitle_languages = ["en", "de"]  # preferred subtitle tracks (optional)
```

The playlist is an id or a URL, not a name: looking a playlist up by name needs a logged-in account and the same name can exist twice. A public or unlisted playlist needs nothing more. A private playlist needs `cookies`, a Netscape `cookies.txt` exported from a browser that is logged in to YouTube (yt-dlp cannot read Chrome's cookies on Windows). The file is read, never changed. Keep it out of any repo.

The transcript comes from the video's subtitles. Without subtitles the audio is transcribed locally, which needs ffmpeg and the `transcribe` extra (`uv sync --extra transcribe`; faster-whisper `small` on the CPU, about 460 MB of model downloaded on first use). Without them the Report and the Run log say so, and the Fact-check runs on the title and description.

The tray runs the check at the `check_times`, once at start-up when a slot was missed while it was off, and on "Check inbox now" in its menu; a toast sums up the new Fact-checks. Fact-checks wait in the same queue as every other Run (`parallel_runs`). `investigate inbox` does one check from the command line and waits for the Fact-checks to finish. Only one check runs at a time.

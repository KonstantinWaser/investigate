# investigate

A personal research desk. You paste text, links, files or images (or save an Instagram Collection or a YouTube playlist), an agent CLI researches or fact-checks it in depth, and you get a cited HTML Report that relates the findings to your own projects.

**[Download install.cmd](https://github.com/KonstantinWaser/investigate/raw/main/install.cmd)**, double-click it, and follow the setup page that opens. Windows 10 or 11, no command line, no administrator rights. Details in section 1.

Runs happen on your machine through the Claude Code CLI. It has no server of its own. Traffic comes from the CLI and the agent (model calls, web search and fetch), the Inbox sources you configure (yt-dlp for YouTube, the browser profile for Instagram), and first-use downloads: the Playwright MCP package through npx and the Whisper model from Hugging Face. MIT licensed. The vocabulary (Item, Run, Report, Depth, Context, Inbox, Verdict) is in `GLOSSARY.md`, the agreed design in `docs/design.md`, what exists elsewhere in `docs/prior-art.md`, and how to add another agent CLI in `docs/runner.md`.

## Contents

1. Install on Windows (no command line)
2. First run: `investigate setup`
3. Bring your own model
4. Using it: command line, tray, quick capture, Reports, Follow-ups
5. Settings: Depth and permission modes
6. Context files
7. Inboxes: YouTube playlist, Instagram Collection
8. Browser login
9. Start at login and the hotkey
10. macOS and Linux notes
11. Terms of service risk
12. Development, and install from a terminal

## 1. Install on Windows (no command line)

1. Click **[Download install.cmd](https://github.com/KonstantinWaser/investigate/raw/main/install.cmd)**. If your browser shows the text instead of saving a file, press Ctrl+S and save it as `install.cmd` (not `install.cmd.txt`).
2. Double-click the saved file. Windows SmartScreen may say "Windows protected your PC" because the file is not signed: click **More info**, then **Run anyway**. The file is short and readable, open it in Notepad first if you like.
3. Wait a few minutes. The window shows each step in plain words:
   1. It installs [uv](https://docs.astral.sh/uv/) if it is missing (the official installer).
   2. It checks for the Claude Code CLI. If it is missing, it asks Y or N, and on Y runs the official Claude Code installer.
   3. It installs investigate (with the tray, transcription and Instagram extras) from this repository.
   4. It runs `investigate first-run`: Start menu shortcuts, start at login, the tray (no console window) and the setup page in your browser.
4. On the setup page, log in to Claude Code, add Context files, and optionally log in the browser profile, set an Inbox, or install ffmpeg. Then paste something into quick capture (the tray icon, or `ctrl+alt+i`) and open your first Report.

Everything is installed for your Windows user, in your user profile, with no administrator rights. The installer does not install Git, Chrome or ffmpeg; the setup page links or installs what you choose. If a step fails, the window stays open with the message; running `install.cmd` again is safe.

**Update:** run `install.cmd` again. It reinstalls the newest version, leaves your data folder and `config.toml` alone, and does not start a second tray.

**Uninstall:** Start menu, **Uninstall investigate**. It quits the tray, removes the shortcuts and start at login, removes the program, and keeps your data folder (Reports, Context files, browser login) unless you answer yes to the second question. uv and Claude Code stay installed.

The installed copy comes from the `main` branch of this repository as a zip file (so Git is not needed). Terminal install and development are in section 12.

## 2. First run: `investigate setup`

```
uv run investigate setup
```

Setup walks through, in order: whether the `claude` CLI is installed and logged in, the data folder and `config.toml`, Context files, the optional extras (transcription needs ffmpeg), the browser login, start at login, and on Windows a hint to drag the tray icon out of the overflow area. Every question defaults to no, an existing `config.toml` or Context file is never overwritten, and running it again changes nothing unless you answer differently. The login check runs `claude auth status`, which makes no model call. If that command gives no answer, setup says "login not verified" and does not spend a model call to find out (it runs with stdin closed and a timeout).

The same steps are on a page in the browser, no terminal needed: the tray menu's Setup item opens `http://127.0.0.1:8765/setup`. Each step shows its status and one action: log in to Claude Code, upload Context files (copied into `contexts/` in the data folder; a file that already exists is kept unless you tick Replace), log in the browser profile, set the YouTube playlist and the Instagram Collection, install ffmpeg with winget, and switch start at login. A button that starts a program (`claude auth login`, `winget install`, the Chrome login) opens it in its own window and the page reloads until it is done. After the ffmpeg install, restart Investigate so it sees the new PATH.

Then try a first Run and open the Report it prints:

```
uv run investigate run "What changed in Python 3.13?" --depth quick
```

## 3. Bring your own model

`investigate` starts the `claude` CLI, so it works with whatever that CLI is set up for. Nothing in this repo holds a key.

- Subscription: run `claude` once and sign in (or `claude auth login`).
- Anthropic API key: set `ANTHROPIC_API_KEY` in the environment that starts `investigate`.
- Amazon Bedrock or Google Vertex AI: set the provider variables the Claude Code documentation names (for example `CLAUDE_CODE_USE_BEDROCK=1` or `CLAUDE_CODE_USE_VERTEX=1`, plus the region and credentials).
- A proxy or gateway: set `ANTHROPIC_BASE_URL` (and `ANTHROPIC_AUTH_TOKEN` when it wants one).

The model per Depth is the `model` value in `config.toml` (an alias such as `opus` or `sonnet`, or a full model name for your provider); empty means the CLI's default. A tray started at login only sees the environment of your login session, so set provider variables as user-level environment variables, not in a terminal. Other agent CLIs need an adapter, see `docs/runner.md`.

## 4. Using it

```
uv run investigate run "text and/or URLs" --depth quick|standard|deep
uv run investigate run --attach notes.pdf "what does this claim?"
uv run investigate history
```

Each Run gets a folder under the data folder, `~/.investigate/runs/` by default (set `INVESTIGATE_HOME` to use another one). It holds the Item (`item.md`), the agent's result (`result.md`, and `result.json` when it parsed), the Report (`report.html`), the agent log, and `attachments/` when the Item had files. History is the index of all Runs.

### Tray and quick capture

```
uv run investigate tray
```

Shows a tray icon and serves the History on `http://127.0.0.1:8765/` (loopback only). Left-click, or the hotkey, opens quick capture: type or paste text, links or images, drop files, pick a Depth. Finished Runs raise a toast; on Windows, clicking it opens the Report. A second `investigate tray` exits and says where the first one is. Runs go through a queue; `parallel_runs` in `config.toml` (1 to 3, default 2) sets how many run at once. Quit gives running Runs 10 seconds, then stops them and marks them partial. `investigate serve` runs the same server without the tray; it refuses a non-local address unless you pass `--allow-remote`, because anyone who can reach it can read and delete your Reports.

### Reports and Follow-ups

A Report states the answer, the findings with a confidence and Citations, and a "Relevance to your work" subsection for each Context file. A Follow-up box at the bottom resumes the same agent session and appends the answer. If the session is gone, the question starts a new Run instead and says so. A Run that hits its Depth time limit keeps what it found and is marked partial.

## 5. Settings: Depth and permission modes

The tray menu's Settings item opens `http://127.0.0.1:8765/settings`. It edits `config.toml` (comments and keys it does not know are kept) and applies at once; Runs already started keep the settings they began with. You can edit the file by hand too, but the parallel limit, hotkey and start at login then only follow the Settings page or a restart.

**Depth** sets the effort of a Run:

| Depth | Default limit | Default model | Meant for |
|---|---|---|---|
| quick | 5 minutes | the CLI's default | a fast look |
| standard | 15 minutes | the CLI's default | the usual |
| deep | 45 minutes | `opus`, with subagents per source class | the careful version |

Limit and model are per Depth in `config.toml` (`[depths.quick]` and so on).

**Permission mode** (`permission_mode`) decides what the agent may do while nobody can answer a prompt:

- `bypass` (the default): the agent runs with `--permission-mode bypassPermissions`, which means no permission prompts at all. It can run shell commands, read and write any file your user can, and use the network. What it reads on the web is untrusted, so a malicious page could try to steer it into doing something harmful with those powers. Use this only on a machine and account where you accept that risk, ideally not one holding credentials or data you cannot lose.
- `allowlist`: web search, web fetch, subagents, reading your Context files by exact path and the Item's attached files, and writing inside the Run's own folder. Everything else is denied. Safer, and the right choice if you are unsure; the cost is that the agent cannot run code or install anything to check a claim.

In both modes the browser tools that run code or upload files stay denied, and every hook from your own Claude Code settings is switched off for Runs.

## 6. Context files

A Context file is a Markdown file describing something you work on (an employer, studies, a side project). Reports relate their findings to each one, in its own subsection, never blended. List them in `config.toml` (or on the Settings page, or in `investigate setup`):

```toml
context_files = [
    {name = "work", path = "~/notes/work.md"},
    {name = "studies", path = "~/notes/studies.md"},
]
```

Keep anything private to one area out of the others, since each name is shown in its Report. The Context files stay where they are; Runs read them by exact path.

## 7. Inboxes

An Inbox is a place you save things to be fact-checked: a YouTube playlist or an Instagram Collection. `investigate inbox` (and the tray's timer) reads them, and each item it has not seen becomes a Fact-check Run: every claim gets a Verdict (Confirmed, Partly true, Outdated, Unverified, Wrong) with a one-sentence reason and Citations, plus the Relevance to each Context. A Report has a "Promote to Investigation" button that starts an Investigation on the same Item. Nothing is ever changed on YouTube or Instagram.

```toml
[inbox]
check_times = ["08:00", "18:00"]   # the tray checks at these local times (default)
depth = "standard"                 # Depth of the Fact-checks
max_attempts = 3                   # tries per item before it is given up on (optional)
max_new_per_check = 10             # new Fact-checks per check; the rest wait (optional)

[inbox.youtube]
playlist = "PLxxxxxxxxxxxxxxxx"    # a playlist id, or a URL with ?list=
cookies = "youtube-cookies.txt"    # only for a private playlist (optional)
subtitle_languages = ["en", "de"]  # preferred subtitle tracks (optional)
max_duration_minutes = 90          # longer videos are never transcribed from audio (optional)

[inbox.instagram]
collection = "To Check"            # the Collection's name; case and spacing are ignored
max_duration_minutes = 10          # longer reels are never transcribed from audio (optional)
```

Processed ids are kept in `inbox/youtube.json` and `inbox/instagram.json` in the data folder, so a second check does nothing new. The tray runs a check at the `check_times`, once at start-up when a slot was missed while it was off, and on "Check inbox now"; a toast sums up the new Fact-checks. When a source cannot be reached the tray retries after 10, 20, then 40 minutes (never later than the next slot) and says so once. Fact-checks wait in the same queue as every other Run. `investigate inbox` does one check from the command line and waits for the Fact-checks to finish. Only one check runs at a time.

### YouTube playlist

The playlist is an id or a URL, not a name. A public or unlisted playlist needs nothing more. A private playlist needs `cookies`, a Netscape `cookies.txt` exported from a browser that is logged in to YouTube. Each check hands yt-dlp a temporary copy, so your file is never changed. Keep it out of any repo. Live and upcoming videos are skipped and looked at again later. An item that cannot be fetched is tried `max_attempts` times, then dropped with a line in `inbox/inbox.log`. A Fact-check Run that failed without a Report is queued again, up to `max_attempts` Runs in all.

### Instagram Collection

Instagram has no official API for saved Collections, so the logged-in browser profile is the only route: run `investigate login` once, sign in to Instagram, and install the `instagram` extra (Chrome must be installed). The check opens a static instagram.com page, blocks every request that is not a GET or HEAD, and asks Instagram's own web endpoints through that profile, with a 30 second limit per request. Only read requests are made, never an unsave, like, follow or comment. The endpoints are not a public API and may change. If you are logged out, the check says so, the tray toasts it once, and the YouTube source runs as usual. A busy browser profile (a deep Run, a login, an open Chrome window on it) skips the Instagram part of that check and the tray retries soon.

### Transcription

The transcript comes from the video's subtitles. Without subtitles the audio is transcribed locally with ffmpeg and faster-whisper (`small` on the CPU, about 460 MB of model downloaded on first use). Install ffmpeg from your system and `uv sync --extra transcribe`. Without them the item is checked on its title, description, caption and frames, and the Report says so.

## 8. Browser login

```
uv run investigate login
```

Opens a dedicated Chrome profile (`browser-profile/` in the data folder, never in the repo). Sign in to YouTube, Instagram and X, then close the window. With `browser = true` in `config.toml`, Runs drive that profile through Playwright MCP for logged-in search. The profile holds your sessions: protect the data folder like a password store. Only one thing uses the profile at a time (a lock enforces it), so a deep Run, an Inbox check and `investigate login` take turns.

## 9. Start at login and the hotkey

`start_at_login = true` (a switch in Settings) makes the tray register itself: a value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` on Windows (`pythonw.exe`, so no console window), a LaunchAgent in `~/Library/LaunchAgents` on macOS, an autostart `.desktop` file in `~/.config/autostart` on Linux. The entry belongs to one data folder: the default folder uses the name `Investigate`, any other folder gets its own entry, so a tray on another folder never touches yours. It is checked at every tray start and rewritten if the install moved; an install inside the temp folder or a git worktree never takes over an existing entry. On Windows 11 a new tray icon starts in the overflow area: click the arrow in the taskbar and drag the icon out once.

The quick-capture hotkey is `ctrl+alt+i` by default (`hotkey` in `config.toml`, modifiers ctrl, alt, shift, win plus a letter, digit or F1 to F24; empty turns it off). On Windows it uses `RegisterHotKey`, which reports a combination another program already holds. Elsewhere it uses `pynput`, which cannot tell a conflict.

### Windows without a console: `first-run` and `uninstall`

`investigate first-run` is what an installer runs after `uv tool install "investigate[tray]"`. It creates two Start menu shortcuts (`investigate`, which starts the tray, and `Uninstall investigate`), turns on start at login, starts the tray with no console window (nothing happens when a tray already runs) and opens the setup page in your browser. Running it again creates nothing new, starts no second tray and never replaces your `config.toml`. If the tray does not come up within 20 seconds it says so instead of opening a dead page.

The tray starts through `investigate-tray`, a Windows GUI program (a `[project.gui-scripts]` entry), so the shortcut and the login entry open no console. `uv tool install` puts it next to `investigate.exe` in the tool's `Scripts` folder and links both into uv's bin folder; the shortcuts and the login entry point at the `Scripts` copy, which does not move.

`investigate uninstall` (or the `Uninstall investigate` shortcut) asks once, quits the tray through its own Quit, removes the login entry and both shortcuts, and keeps your data folder unless you answer yes to a second, separate question (default no; it only deletes a folder that looks like an investigate data folder). The program itself is removed by `uv tool uninstall investigate`, run by a detached process after the command exits, because uv cannot delete an `investigate.exe` that is still running. `--yes` skips the first question only.

## 10. macOS and Linux notes

The author uses and tests Windows 11. The macOS and Linux paths are written to the platform documentation and covered by headless tests with fakes, but have not been run on real machines. Expect rough edges and please report them.

- **macOS tray**: `pystray` must run on the main thread there, and `investigate tray` runs it on the main thread. Known limits: the icon and title are updated from a worker thread, which AppKit prefers on the main thread, and Ctrl+C skips the orderly shutdown, so leave with Quit from the menu. Toasts are pystray's plain notification, with no click-through to the Report (only Windows has that); open the Report from the History page.
- **macOS hotkey**: `pynput` needs Accessibility and Input Monitoring permission for the app that starts it (Terminal, or your Python). Grant them under System Settings, Privacy and Security, then restart the tray. Without them the listener can start yet receive no keys, and no notice appears.
- **Linux tray**: needs an AppIndicator or StatusNotifier host. GNOME shows no tray icons without an extension such as AppIndicator support; KDE and most other desktops do.
- **Linux hotkey**: global hotkeys through `pynput` work on X11. On Wayland the compositor does not allow applications to grab global keys, so the hotkey will not fire; use the tray icon, or bind a desktop shortcut to a command that opens `http://127.0.0.1:8765/capture`.
- **Chrome login**: needs Google Chrome in a standard location; `investigate login` says so when it is missing.
- **Start at login**: LaunchAgents are read at the next login, XDG autostart needs a desktop that honours it.

## 11. Terms of service risk

Automated access to Instagram and X is against their terms of service. The Instagram Inbox and logged-in browser search on those sites do exactly that, and other sites may have similar terms. Platforms may rate limit, challenge or suspend an account that they see automating. You use these features at your own risk, with your own accounts, and you accept that risk: the authors give no assurance that an account will not be restricted. Consider a secondary account, and leave `[inbox.instagram]` and `browser = false` unset if you are unwilling to take it. Text, URL and YouTube-playlist Runs do not need any logged-in access.

## 12. Development, and install from a terminal

### Install from a terminal

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code/setup) on your PATH (`claude`).
- Optional: Google Chrome (browser login, Instagram Inbox), ffmpeg (transcription).

```
git clone <this repository>
cd investigate
uv sync --extra tray --extra transcribe --extra instagram
```

Each extra is optional: `tray` (icon, quick capture, toasts, hotkey), `transcribe` (faster-whisper, local speech to text), `instagram` (Playwright, for the Instagram Inbox). Plain `uv sync` is enough for the command line. As a tool without a clone: `uv tool install "investigate[tray] @ https://github.com/KonstantinWaser/investigate/archive/refs/heads/main.zip"`, then `investigate first-run`. The commands in this README are written `uv run investigate ...` for a clone.

### Tests

```
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests use a fake runner and an `INVESTIGATE_HOME` in a temp folder: no network, no Claude login. A test scans every tracked file for personal data (user-home paths with a real-looking name, email addresses). To also check words only you know (your name, employer), put them one per line in a file outside the repo and point `INVESTIGATE_DENYLIST` at it before running the tests. `install.cmd` is read by `tests/test_install_cmd.py`, which fails when its extras, entry points, repository URL or subcommand drift from the package. `AGENTS.md` has the layout of the code, and `docs/runner.md` the interface a new agent CLI adapter implements.

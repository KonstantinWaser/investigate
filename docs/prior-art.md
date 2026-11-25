# Prior art and feasibility (researched 2026-11-24)

Three researcher passes (web search plus first-party pages). Key repos verified with the GitHub API on the same day. Entries marked *snippet* were seen only in search results.

## Verdict

Nothing found combines the whole thing: tray quick-capture, a background headless Claude Code run with the user's Context files, a cited HTML Report, resumable Follow-ups in one History, and scheduled Fact-checks of a saved Instagram Collection and YouTube playlist. Every part exists somewhere, so the value is the combination, and several projects are worth borrowing from.

## Closest overlaps

| Project | What it is | Overlap | Gap |
|---|---|---|---|
| [neuronection/desktop-assistant](https://github.com/neuronection/desktop-assistant) | Apache-2.0 tray launcher, global hotkey, `/research` with cited report, local SQLite history, any OpenAI-compatible model. 2 stars, active today | High on the host | Own LLM loop (no Claude Code), Markdown/JSON export, no Context, no social Inboxes |
| [mauriporte/instagram-saved-to-obsidian](https://github.com/mauriporte/instagram-saved-to-obsidian) | MIT. Reads named Instagram saved Collections daily via instagrapi session cookie, faster-whisper, Claude Code headless, notes into Obsidian | High on the Instagram Inbox | Summarizes only, no Verdicts; uses the private API |
| [199-biotechnologies/claude-deep-research-skill](https://github.com/199-biotechnologies/claude-deep-research-skill) | Claude Code skill, 8-phase research pipeline, source credibility scoring. 1.2k stars | High on the research method | Interactive skill, no host, no History |
| [24601/agent-deep-research](https://github.com/24601/agent-deep-research) | MIT CLI on the Gemini Interactions API, `--context PATH`, `--follow-up ID`, HTML output | Medium (Context, Follow-up) | Gemini only, no host |
| [Smiky0/Factube](https://github.com/Smiky0/Factube) | YouTube URL to JSON claims with verdicts and a source each | High on Verdict shape | Needs captions, YouTube only |
| [Libr-AI/OpenFactVerification (Loki)](https://github.com/Libr-AI/OpenFactVerification) | MIT claim extraction and verification pipeline, 1.2k stars, last push Oct 2024 | Medium on the Fact-check pipeline | No fetching, no schedule, stale |
| GPT Researcher, Khoj, Onyx, Vane (Perplexica), dzhng/deep-research, STORM | Big open-source deep research or answer engines | Medium to low | Web apps or CLIs, no tray capture, no Claude Code runner, no social Inboxes |
| ChatGPT / Gemini Deep Research | Background research with notification | High on output | Closed, no tray capture, no local Context files |
| Grok and @AskPerplexity on X, BSmeter, Factiverse, Full Fact AI | Social or video fact-checkers | Medium | On-demand or enterprise; none reads your saved items |
| ShortCheck ([arXiv 2509.20467](https://arxiv.org/abs/2509.20467)) | Research pipeline: transcription, OCR, claim verification for short video | Pipeline shape | No public code linked |

The open_deep_research repo by LangChain was archived on 2026-08-21 (per its page).

## Feasibility findings that change the build

- **Instagram saved Collections have no official route.** The Basic Display API is dead since 2024-12-04, the Graph API has no saved-list endpoint, `yt-dlp` has no saved or collection extractor, and `instaloader` supports `:saved` but not collections. So the logged-in Playwright profile (design decision) is the right call. instagrapi (what mauriporte uses) is the alternative, at a higher ban risk.
- **YouTube Watch Later is unreachable via the Data API** (empty since 2016). Use a normal playlist, read through `yt-dlp` with cookies or the Playwright profile.
- **`yt-dlp --cookies-from-browser chrome` is broken on Windows** (Chrome App-Bound Encryption, issue #10927 open). Another reason to own the browser profile.
- **Playwright MCP supports `--user-data-dir`** for a persistent profile. Without it, every run gets a temporary profile.
- **Claude Code headless**: `-p`, `--output-format json|stream-json` (returns `session_id`), `--resume`, `--session-id`, `--permission-mode bypassPermissions`, `--allowedTools`, `--mcp-config`, `--max-turns`, `--model`, `--max-budget-usd` all exist. There is no whole-run timeout flag, so the tray app enforces the Depth timeout on the subprocess. `--bare` ignores subscription login and needs an API key, so do not use it by default. Without `--bare`, a `-p` run loads the working directory's hooks and `.mcp.json`; run each Run in its own run folder with our own `--mcp-config`.
- **pystray** works on Windows, macOS and Linux (Xorg/GNOME). On macOS the icon must run on the main thread. Global hotkeys via `pynput` need the macOS accessibility permission and do not work under Wayland.
- **Windows 11 tray overflow**: no supported API to promote an icon; the user drags it out once.
- **Terms**: Instagram and X both forbid automated collection without permission (read at secondary sources only; the primary pages did not load). Documentation must say users accept that risk.

## Name

`investigate` is taken on PyPI (OpenDNS client, 2021) and npm (2016). The GitHub repo can still be called `investigate` under a personal account; it only matters if we publish a PyPI package. Candidates checked so far: `dossier` and `fieldnote` taken; `deepdig` free on npm, PyPI unresolved.

## Open

- Read the Instagram and X terms at source.
- Confirm whether the YouTube Data API reads private playlists via OAuth.

# Design (agreed 2026-11-24)

Settled in a grilling session before any code. Vocabulary is in `GLOSSARY.md`.

## Distribution

Public GitHub repo under Konstantin's personal account, MIT. Anyone can clone it and use their own models.

## Host and input

- A tray app (`pystray`) starts at login and hosts the local web server (Python, `uv`) and the scheduler. Menu: New, History, Check inbox now, Log in to sites, Settings, Quit.
- Left click on the tray icon, or `Ctrl+Alt+I` (configurable), opens a quick-capture popup: one box taking text, URLs, pasted images and dropped files, a Depth picker, Submit. It closes on submit.
- History and Reports open in a full window.
- On Windows 11 the new tray icon lands in the overflow; the user drags it out once.

## Runs

- Runs go in the background, 2 to 3 at once, the rest queued, with a desktop toast when each finishes.
- A Run is a headless agent CLI session behind a documented runner interface. v1 ships the Claude Code runner only; Codex or Gemini adapters are one file each later.
- Permissions: bypass mode by default, configurable per user to a narrow allowlist.
- Depth defaults: quick 5 min and standard 15 min on the default model; deep 45 min on the strongest model with parallel subagents. A Run that times out keeps what it found and is marked partial. All configurable.
- Method: split into sub-questions, search official docs, vendor repos, GitHub, papers and social sources (logged-in search on YouTube, Instagram and X), read primary sources, check contradictions and dates, cite every claim with a confidence.

## Logged-in browser

A dedicated persistent Playwright Chromium profile, logged into once per site via `investigate login`, driven by the agent through Playwright MCP. Used for logged-in search and to read the user's Instagram Collection and YouTube playlist. Read-only on every platform; processed IDs are tracked locally.

## Fact-checks from saved items

- The user saves reels to an Instagram Collection and videos to a YouTube playlist (names configurable). Same mechanism for every user.
- The tray app's own timer checks the Inboxes twice a day, plus once at startup when a check was missed. No OS scheduler entries.
- Audio is transcribed with ffmpeg plus faster-whisper (optional extra; text and URL Investigations work without it).
- Each Item becomes a Fact-check; one click promotes it to an Investigation.

## Reports

- The agent writes a structured result (Markdown body plus JSON: claims, Verdicts, Citations, confidence, relevance per Context); our template renders the HTML.
- English by default, configurable.
- A "Relevance to your work" section with one subsection per Context file, never blended.
- A Follow-up box resumes the same CLI session and appends the answer.

## Storage

`~/.investigate/` outside the repo: config, Context file paths, browser profile, `runs/<date>-<slug>/` per Run (Report, raw Citations, agent log) and an index for the History (filter by Kind, Verdict, date).

## Related

`instagram-digest` (same machine) stays separate. Its `yt_fetch` and `transcribe` tools get copied in; folding it in as a later Inbox stays open.

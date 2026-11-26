"""`investigate setup`: a guided first run, safe to repeat.

Every outside effect goes through `SetupIO` (questions, output, program lookup, running a
program, module lookup, the browser login), so tests drive the whole walk with fakes and never
start a browser or call the real `claude`. Rules the steps keep:

- Nothing existing is overwritten. A config file that is there is only read (and edited in place
  by one question's answer); a Context file that is there is only listed in the config.
- Every question defaults to the safe answer: no. End of input counts as no.
- A second run with no new answers changes nothing.
- The agent CLI's login is checked with `claude auth status`, which makes no model call. It runs
  with stdin closed and a timeout, so even an older CLI that treats the unknown subcommand as a
  prompt cannot wait for input or start a session.

The checks (`agent_status`, the install links, the ffmpeg command) are shared with the browser
setup page in `setupweb.py`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from investigate import browser
from investigate.config import Config, ConfigError, config_path, load_config, validate_raw
from investigate.settings import _write_atomically

STATUS_TIMEOUT_SECONDS = 20
CLAUDE_INSTALL_URL = "https://code.claude.com/docs/en/setup"
CHROME_URL = "https://www.google.com/chrome/"
FFMPEG_URL = "https://ffmpeg.org/download.html"
FFMPEG_WINGET_ID = "Gyan.FFmpeg"  # `winget show --id Gyan.FFmpeg`: the gyan.dev full build
STARTER_CONTEXT = """\
# About my work

Describe what you work on here: projects, employer, studies, interests, the tools and
decisions you care about. Reports relate their findings to this file and say what matters
for it. Keep one file per area, and keep anything private to one area out of the others.
"""


def _run(argv: list[str]) -> tuple[int, str]:
    from investigate.runner import console_flags

    done = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=STATUS_TIMEOUT_SECONDS,
        check=False,
        **console_flags(),
    )
    return done.returncode, done.stdout


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


@dataclass(frozen=True)
class SetupIO:
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str]], tuple[int, str]] = _run
    has_module: Callable[[str], bool] = _has_module
    login: Callable[[Path], int] = field(default=browser.login)
    chrome_found: Callable[[], bool] = lambda: browser._find_chrome() is not None  # noqa: E731
    platform: str = sys.platform


def _confirm(io: SetupIO, question: str) -> bool:
    try:
        answer = io.ask(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _ask_text(io: SetupIO, question: str) -> str:
    try:
        return io.ask(question).strip()
    except EOFError:
        return ""


def _step(io: SetupIO, title: str) -> None:
    io.say(f"\n== {title}")


@dataclass(frozen=True)
class AgentStatus:
    installed: bool
    path: str | None = None
    logged_in: bool | None = None  # None: could not be verified
    code: int = 0  # exit code of `claude auth status`, when it ran


def agent_status(io: SetupIO) -> AgentStatus:
    """Is the CLI installed, and is it logged in (`claude auth status`, JSON by default)."""
    found = io.which("claude")
    if found is None:
        return AgentStatus(False)
    try:
        code, out = io.run([found, "auth", "status"])
    except (OSError, subprocess.SubprocessError):  # includes a timeout
        code, out = -1, ""
    try:
        data = json.loads(out)
        logged_in = data.get("loggedIn") if isinstance(data, dict) else None
    except json.JSONDecodeError:
        logged_in = None
    return AgentStatus(True, found, logged_in if isinstance(logged_in, bool) else None, code)


def ffmpeg_install_command() -> list[str]:
    return [
        "winget",
        "install",
        "--id",
        FFMPEG_WINGET_ID,
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]


def _check_agent_cli(io: SetupIO) -> bool:
    """True when the CLI is installed. The login is read from `claude auth status`."""
    _step(io, "Agent CLI")
    status = agent_status(io)
    if not status.installed:
        io.say(
            f"The 'claude' command was not found on PATH. Install Claude Code "
            f"({CLAUDE_INSTALL_URL}), then run setup again."
        )
        return False
    io.say(f"Found Claude Code at {status.path}.")
    if status.logged_in is True:
        io.say("Installed and logged in.")
    elif status.logged_in is False:
        io.say(
            "Installed, but not logged in. Run `claude auth login`. If you use an API key or a "
            "cloud provider (Bedrock, Vertex) or a proxy through environment variables, this "
            "message can be ignored; see the README."
        )
    else:
        io.say(
            f"Installed, login not verified (`claude auth status` gave no answer, exit code "
            f"{status.code}). Run `claude` once on its own to check that you can sign in."
        )
    return True


def _check_config(io: SetupIO, home: Path) -> Config | None:
    _step(io, "Data folder and config")
    path = config_path(home)
    existed = path.exists()
    try:
        config = load_config(home)  # creates the default file only when it is absent
    except ConfigError as exc:
        io.say(f"error: {exc}")
        io.say("Fix the file by hand (or delete it to start over); setup does not touch it.")
        return None
    io.say(f"Data folder: {home}")
    io.say(
        f"{path} already exists and is kept as it is."
        if existed
        else f"Created {path} with the defaults."
    )
    return config


def edit_config(
    home: Path,
    edit: Callable[[tomlkit.TOMLDocument], None],
    check: Callable[[dict], None] | None = None,
) -> Config:
    """Apply `edit` to the file's text (comments and layout kept), check the result is valid, and
    replace the file atomically. `check` gets the edited file as data and raises ConfigError for
    a rule `validate_raw` does not cover (the `[inbox]` section)."""
    path = config_path(home)
    doc = tomlkit.parse(path.read_bytes().decode("utf-8"))
    edit(doc)
    out = tomlkit.dumps(doc)
    raw = tomllib.loads(out)
    config, problems = validate_raw(raw, home)
    if problems:
        raise ConfigError(f"refusing to write an invalid config: {next(iter(problems.values()))}")
    if check is not None:
        check(raw)
    _write_atomically(path, out)
    return config


def _add_context(home: Path, name: str, path: Path) -> Config:
    def edit(doc: tomlkit.TOMLDocument) -> None:
        entries = doc.get("context_files")
        if entries is None:
            entries = tomlkit.array()
            doc["context_files"] = entries
        entries.multiline(True)
        row = tomlkit.inline_table()
        row["name"] = name
        row["path"] = str(path)
        entries.append(row)

    return edit_config(home, edit)


def _contexts(io: SetupIO, home: Path, config: Config) -> Config:
    _step(io, "Context files")
    if config.contexts:
        io.say("Context files already listed:")
        for c in config.contexts:
            io.say(f"  {c.name}: {c.path}")
    else:
        io.say(
            "A Context file is a Markdown file about what you work on. Reports relate their "
            "findings to it. You can add them later in Settings."
        )
    while True:
        text = _ask_text(io, "Path to a Context file to add (Enter to finish): ")
        if not text:
            return config
        path = Path(text.strip("\"'")).expanduser().resolve()  # Explorer quotes copied paths
        if any(Path(c.path) == path for c in config.contexts):
            io.say(f"{path} is already listed.")
            continue
        name = path.stem
        if any(c.name == name for c in config.contexts):
            io.say(f"A Context named {name!r} exists. Rename the file or edit config.toml.")
            continue
        try:
            if not path.exists():
                if not _confirm(io, f"{path} does not exist. Create a starter file there?"):
                    io.say("Not added.")
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(path, "x", encoding="utf-8") as handle:  # never replaces a file
                        handle.write(STARTER_CONTEXT)
                except FileExistsError:
                    pass
                io.say(f"Created {path}. Edit it to describe your work.")
            elif not path.is_file():
                io.say(f"{path} is not a file.")
                continue
            config = _add_context(home, name, path)
        except (OSError, ConfigError) as exc:
            io.say(f"Could not add {path}: {exc}")
            continue
        io.say(f"Added Context {name!r}.")


def _extras(io: SetupIO) -> None:
    _step(io, "Optional extras")
    ffmpeg = io.which("ffmpeg") is not None
    whisper = io.has_module("faster_whisper")
    if ffmpeg and whisper:
        io.say("Transcription is ready (ffmpeg and faster-whisper).")
    else:
        if not ffmpeg:
            io.say("ffmpeg is not installed. Install it to transcribe audio of videos and reels.")
        if not whisper:
            io.say("faster-whisper is not installed: uv sync --extra transcribe")
        io.say("Without them, videos are checked on subtitles, title and description only.")
    if not io.has_module("playwright"):
        io.say("Instagram Inbox needs: uv sync --extra instagram")


def _set_flag(home: Path, key: str) -> Config:
    return edit_config(home, lambda doc: doc.__setitem__(key, True))


def _browser(io: SetupIO, home: Path, config: Config) -> Config:
    _step(io, "Browser login")
    io.say("Needed for the Instagram Inbox and pages that need your account.")
    if browser.is_logged_in(home):
        io.say("A login is already recorded for the browser profile.")
    elif not io.chrome_found():
        io.say("Google Chrome was not found. Install it, then run `investigate login`.")
        return config
    elif _confirm(io, "Open Chrome now to log in to YouTube, Instagram and X?"):
        if io.login(home) != 0:
            io.say("The login did not finish. Run `investigate login` to try again.")
            return config
    else:
        io.say("Skipped. Run `investigate login` later.")
        return config
    if not config.browser and _confirm(io, "Let Runs use the logged-in browser (browser = true)?"):
        config = _set_flag(home, "browser")
        io.say("Set browser = true.")
    return config


def _start_at_login(io: SetupIO, home: Path, config: Config) -> Config:
    _step(io, "Start at login")
    if config.start_at_login:
        io.say("Already on: the tray starts when you log in.")
    elif not io.has_module("pystray"):
        io.say("The tray is not installed: uv sync --extra tray")
    elif _confirm(io, "Start the tray when you log in?"):
        config = _set_flag(home, "start_at_login")
        io.say("Turned on. The tray registers the entry the next time it starts.")
    else:
        io.say("Skipped. It is a switch in Settings.")
    return config


def _platform_notes(io: SetupIO) -> None:
    _step(io, "Tray")
    if io.platform == "win32":
        io.say(
            "Windows hides new tray icons in the overflow. Click the ^ arrow at the right of the "
            "taskbar and drag the Investigate icon out onto the taskbar so it stays visible."
        )
    elif io.platform == "darwin":
        io.say("macOS: the hotkey needs the Accessibility permission. See the README.")
    else:
        io.say(
            "Linux: the tray and hotkey depend on your desktop; Wayland is limited. See the README."
        )


def run_setup(home: Path, io: SetupIO | None = None) -> int:
    """Walk through setup. 0 when done, 1 when the agent CLI is missing, 2 on a broken config."""
    io = io or SetupIO()
    installed = _check_agent_cli(io)
    config = _check_config(io, home)
    if config is None:
        return 2
    config = _contexts(io, home, config)
    _extras(io)
    config = _browser(io, home, config)
    config = _start_at_login(io, home, config)
    _platform_notes(io)
    _step(io, "Next")
    io.say('Try it: investigate run "your question or a link" --depth quick')
    io.say("Start the tray: investigate tray (needs the tray extra).")
    return 0 if installed else 1

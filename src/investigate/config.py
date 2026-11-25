"""Data folder location and the small config file inside it."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

HOME_ENV = "INVESTIGATE_HOME"


MAX_PARALLEL_RUNS = 3
DEFAULT_HOTKEY = "ctrl+alt+i"


class ConfigError(ValueError):
    """The config file is unreadable or holds an invalid value."""


class Depth(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class PermissionMode(StrEnum):
    BYPASS = "bypass"
    ALLOWLIST = "allowlist"


_MODIFIERS = ("ctrl", "alt", "shift", "win")
_MODIFIER_ALIASES = {
    "control": "ctrl",
    "cmd": "win",
    "super": "win",
    "meta": "win",
    "windows": "win",
}
_FUNCTION_KEY = re.compile(r"f([1-9]|1\d|2[0-4])")


@dataclass(frozen=True)
class Hotkey:
    modifiers: frozenset[str]  # of "ctrl", "alt", "shift", "win"
    key: str  # one lower-case letter or digit, or "f1" to "f24"

    def __str__(self) -> str:
        return "+".join([*(m for m in _MODIFIERS if m in self.modifiers), self.key])


def parse_hotkey(text: str) -> Hotkey | None:
    """`"ctrl+alt+i"` as a `Hotkey`; None for an empty string (off). Raises ValueError when it is
    not modifiers plus one key, or has none of ctrl, alt and win (shift alone would take over
    ordinary typing)."""
    if not text.strip():
        return None
    *mods, key = (p.strip().lower() for p in text.split("+"))
    modifiers = {_MODIFIER_ALIASES.get(m, m) for m in mods}
    unknown = sorted(m for m in modifiers if m not in _MODIFIERS)
    if unknown:
        raise ValueError(f"has an unknown modifier {unknown[0]!r}; use ctrl, alt, shift or win")
    if not modifiers & {"ctrl", "alt", "win"}:
        raise ValueError("needs at least one of ctrl, alt or win, for example ctrl+alt+i")
    if not (len(key) == 1 and key.isascii() and key.isalnum()) and not _FUNCTION_KEY.fullmatch(key):
        raise ValueError(f"has the key {key!r}; use a letter, a digit or F1 to F24")
    return Hotkey(frozenset(modifiers), key)


@dataclass(frozen=True)
class DepthSettings:
    timeout_minutes: float
    model: str | None  # None means the CLI's default model


DEFAULT_DEPTHS: dict[Depth, DepthSettings] = {
    Depth.QUICK: DepthSettings(timeout_minutes=5, model=None),
    Depth.STANDARD: DepthSettings(timeout_minutes=15, model=None),
    Depth.DEEP: DepthSettings(timeout_minutes=45, model="opus"),
}


@dataclass(frozen=True)
class ContextFile:
    name: str  # unique, shown as the subsection heading in the Report
    path: str  # absolute, resolved by `load_config`


@dataclass(frozen=True)
class Config:
    permission_mode: PermissionMode = PermissionMode.BYPASS
    language: str = "English"
    contexts: tuple[ContextFile, ...] = ()  # in config order
    depths: dict[Depth, DepthSettings] = field(default_factory=lambda: dict(DEFAULT_DEPTHS))
    max_attachment_mb: float = 25.0  # size limit per attached file or image
    browser: bool = False  # give Runs the logged-in browser profile
    parallel_runs: int = 2  # Runs at once; the rest wait as `queued`
    hotkey: str = DEFAULT_HOTKEY  # global shortcut for quick capture; "" means off
    start_at_login: bool = False  # start the tray when the user logs in

    @property
    def context_files(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.contexts)


DEFAULT_CONFIG_TOML = """\
# investigate config. Delete a line to get its default back.

# "bypass": the agent runs without permission prompts (default).
# "allowlist": web search, web fetch, reading Context files and writing inside
# the run folder only.
permission_mode = "bypass"

# Language of the Report.
language = "English"

# Your Context files: Markdown files describing what you work on. Each has a name
# and a path. Reports relate their findings to each one, kept strictly separate.
# A plain path string also works and is named after the file.
# context_files = [
#     {name = "employer", path = "~/notes/employer.md"},
#     {name = "studies", path = "~/notes/studies.md"},
# ]
context_files = []

# Largest file or image you can attach to an Item, in megabytes.
max_attachment_mb = 25

# Give Runs the logged-in browser (run `investigate login` once first).
browser = false

# How many Runs go at once (1 to 3). More wait in the queue.
parallel_runs = 2

# Global shortcut that opens quick capture, such as "ctrl+alt+i" (modifiers ctrl, alt,
# shift, win, then a letter, digit or F1 to F24). Empty turns it off.
hotkey = "ctrl+alt+i"

# Start the tray app when you log in.
start_at_login = false

# Wall-clock limit and model per Depth. An empty model means the CLI default.
[depths.quick]
timeout_minutes = 5
model = ""

[depths.standard]
timeout_minutes = 15
model = ""

[depths.deep]
timeout_minutes = 45
model = "opus"
"""


def data_home() -> Path:
    """The data folder: `$INVESTIGATE_HOME`, else `~/.investigate`."""
    override = os.environ.get(HOME_ENV)
    base = Path(override) if override else Path.home() / ".investigate"
    # Resolved so the run folder is spelled the way permission rules are (no 8.3 short names).
    return base.expanduser().resolve()


def config_path(home: Path) -> Path:
    return home / "config.toml"


def load_config(home: Path) -> Config:
    """Read `config.toml` from the data folder, creating it with defaults when absent."""
    path = config_path(home)
    if not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return _parse(raw, path)


def _resolve(value: str, base: Path) -> str:
    """Context file path made absolute once: `~` expanded, relative to the data folder."""
    expanded = Path(value).expanduser()
    return str((expanded if expanded.is_absolute() else base / expanded).resolve())


def _parse_contexts(raw: object, base: Path) -> tuple[ContextFile, ...]:
    """Entries are `{name = "...", path = "..."}` tables or plain path strings (named by stem).
    Raises ConfigError with the problem, without a file prefix."""
    problem = "context_files must be a list of {name, path} tables or path strings"
    if not isinstance(raw, list):
        raise ConfigError(problem)
    contexts: list[ContextFile] = []
    for entry in raw:
        if isinstance(entry, str):
            name, value = Path(entry).stem, entry
        elif isinstance(entry, dict):
            name, value = entry.get("name"), entry.get("path")
        else:
            raise ConfigError(problem)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("every Context needs a non-empty path")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Context {value!r} needs a non-empty name")
        name = name.strip()
        if any(c.name == name for c in contexts):
            raise ConfigError(f"Context name {name!r} is used twice")
        contexts.append(ContextFile(name, _resolve(value, base)))
    return tuple(contexts)


def validate_raw(raw: dict, base: Path) -> tuple[Config, dict[str, str]]:
    """The one set of rules for config values, shared by `load_config` and the Settings page.

    Returns the Config (an invalid field keeps its default) and the problems by field key, such
    as `parallel_runs` or `depths.deep.model`, in the order they were found. `base` is the data
    folder that relative Context paths resolve against."""
    errors: dict[str, str] = {}
    defaults = Config()

    try:
        mode = PermissionMode(raw.get("permission_mode", "bypass"))
    except ValueError:
        errors["permission_mode"] = (
            f"permission_mode must be 'bypass' or 'allowlist', got {raw.get('permission_mode')!r}"
        )
        mode = defaults.permission_mode
    language = raw.get("language", "English")
    if not isinstance(language, str) or not language.strip():
        errors["language"] = "language must be a non-empty string"
        language = defaults.language
    try:
        contexts = _parse_contexts(raw.get("context_files", []), base)
    except ConfigError as exc:
        errors["context_files"] = str(exc)
        contexts = ()

    max_mb = raw.get("max_attachment_mb", 25)
    if isinstance(max_mb, bool) or not isinstance(max_mb, int | float) or max_mb <= 0:
        errors["max_attachment_mb"] = "max_attachment_mb must be a positive number"
        max_mb = defaults.max_attachment_mb
    browser = raw.get("browser", False)
    if not isinstance(browser, bool):
        errors["browser"] = "browser must be true or false"
        browser = defaults.browser

    parallel = raw.get("parallel_runs", 2)
    if isinstance(parallel, bool) or not isinstance(parallel, int):
        errors["parallel_runs"] = "parallel_runs must be a whole number"
        parallel = defaults.parallel_runs
    elif not 1 <= parallel <= MAX_PARALLEL_RUNS:
        errors["parallel_runs"] = f"parallel_runs must be between 1 and {MAX_PARALLEL_RUNS}"
        parallel = defaults.parallel_runs

    hotkey = raw.get("hotkey", DEFAULT_HOTKEY)
    try:
        if not isinstance(hotkey, str):
            raise ValueError('must be text such as "ctrl+alt+i", or empty to turn it off')
        parsed = parse_hotkey(hotkey)
        hotkey = str(parsed) if parsed else ""
    except ValueError as exc:
        errors["hotkey"] = f"hotkey {exc}"
        hotkey = defaults.hotkey
    login = raw.get("start_at_login", False)
    if not isinstance(login, bool):
        errors["start_at_login"] = "start_at_login must be true or false"
        login = defaults.start_at_login

    depths = dict(DEFAULT_DEPTHS)
    raw_depths = raw.get("depths", {})
    if not isinstance(raw_depths, dict):
        errors["depths"] = "depths must be a table of [depths.quick] and so on"
        raw_depths = {}
    for name, values in raw_depths.items():
        if not isinstance(values, dict):
            errors[f"depths.{name}"] = f"depths.{name} must be a table"
            continue
        try:
            depth = Depth(name)
        except ValueError:
            errors[f"depths.{name}"] = f"unknown depth {name!r}"
            continue
        preset = DEFAULT_DEPTHS[depth]
        minutes = values.get("timeout_minutes", preset.timeout_minutes)
        if isinstance(minutes, bool) or not isinstance(minutes, int | float) or minutes <= 0:
            errors[f"depths.{name}.timeout_minutes"] = (
                f"depths.{name}.timeout_minutes must be a positive number"
            )
            minutes = preset.timeout_minutes
        model = values.get("model", preset.model)
        if model is not None and not isinstance(model, str):
            errors[f"depths.{name}.model"] = f"depths.{name}.model must be a string"
            model = preset.model
        depths[depth] = DepthSettings(float(minutes), (model.strip() if model else None) or None)
    config = Config(
        permission_mode=mode,
        language=language.strip(),
        contexts=contexts,
        depths=depths,
        max_attachment_mb=float(max_mb),
        browser=browser,
        parallel_runs=parallel,
        hotkey=hotkey,
        start_at_login=login,
    )
    return config, errors


def _parse(raw: dict, path: Path) -> Config:
    config, errors = validate_raw(raw, path.parent)
    if errors:
        raise ConfigError(f"{path}: {next(iter(errors.values()))}")
    return config

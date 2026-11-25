"""Data folder location and the small config file inside it."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

HOME_ENV = "INVESTIGATE_HOME"


class ConfigError(ValueError):
    """The config file is unreadable or holds an invalid value."""


class Depth(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class PermissionMode(StrEnum):
    BYPASS = "bypass"
    ALLOWLIST = "allowlist"


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


def _parse_contexts(raw: object, path: Path) -> tuple[ContextFile, ...]:
    """Entries are `{name = "...", path = "..."}` tables or plain path strings (named by stem)."""
    problem = f"{path}: context_files must be a list of {{name, path}} tables or path strings"
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
            raise ConfigError(f"{path}: every Context needs a non-empty path")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{path}: Context {value!r} needs a non-empty name")
        name = name.strip()
        if any(c.name == name for c in contexts):
            raise ConfigError(f"{path}: Context name {name!r} is used twice")
        contexts.append(ContextFile(name, _resolve(value, path.parent)))
    return tuple(contexts)


def _parse(raw: dict, path: Path) -> Config:
    try:
        mode = PermissionMode(raw.get("permission_mode", "bypass"))
    except ValueError:
        raise ConfigError(
            f"{path}: permission_mode must be 'bypass' or 'allowlist', "
            f"got {raw.get('permission_mode')!r}"
        ) from None
    language = raw.get("language", "English")
    if not isinstance(language, str) or not language.strip():
        raise ConfigError(f"{path}: language must be a non-empty string")
    contexts = _parse_contexts(raw.get("context_files", []), path)

    depths = dict(DEFAULT_DEPTHS)
    raw_depths = raw.get("depths", {})
    if not isinstance(raw_depths, dict):
        raise ConfigError(f"{path}: depths must be a table of [depths.quick] and so on")
    for name, values in raw_depths.items():
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: depths.{name} must be a table")
        try:
            depth = Depth(name)
        except ValueError:
            raise ConfigError(f"{path}: unknown depth {name!r}") from None
        base = DEFAULT_DEPTHS[depth]
        minutes = values.get("timeout_minutes", base.timeout_minutes)
        if isinstance(minutes, bool) or not isinstance(minutes, int | float) or minutes <= 0:
            raise ConfigError(f"{path}: depths.{name}.timeout_minutes must be a positive number")
        model = values.get("model", base.model)
        if model is not None and not isinstance(model, str):
            raise ConfigError(f"{path}: depths.{name}.model must be a string")
        depths[depth] = DepthSettings(float(minutes), model or None)
    return Config(mode, language.strip(), contexts, depths)

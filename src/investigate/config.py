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
class Config:
    permission_mode: PermissionMode = PermissionMode.BYPASS
    language: str = "English"
    context_files: tuple[str, ...] = ()
    depths: dict[Depth, DepthSettings] = field(default_factory=lambda: dict(DEFAULT_DEPTHS))


DEFAULT_CONFIG_TOML = """\
# investigate config. Delete a line to get its default back.

# "bypass": the agent runs without permission prompts (default).
# "allowlist": web search, web fetch, reading Context files and writing inside
# the run folder only.
permission_mode = "bypass"

# Language of the Report.
language = "English"

# Your Context files: paths of files describing what you work on. Reports relate
# their findings to them.
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
    return Path(override) if override else Path.home() / ".investigate"


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
    context = raw.get("context_files", [])
    if not isinstance(context, list) or not all(isinstance(c, str) for c in context):
        raise ConfigError(f"{path}: context_files must be a list of strings")

    depths = dict(DEFAULT_DEPTHS)
    for name, values in (raw.get("depths") or {}).items():
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
    return Config(mode, language.strip(), tuple(context), depths)

"""What the Settings page edits, and how it reads and writes `config.toml`.

`SECTIONS` lists the fields the page shows. A new config setting becomes a field by adding its
rule to `config.validate_raw`, its default to `DEFAULT_CONFIG_TOML` and one `Field` here; the page,
the form parsing and the file write need nothing more.

Validation is `config.validate_raw`, the same code `load_config` runs. Saving edits the file with
`tomlkit`, so comments, layout and keys this page does not know survive, and replaces it
atomically. Only the fields that changed are touched, and a field the file never mentioned stays
out of it when the user left the default.
"""

from __future__ import annotations

import contextlib
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from investigate.config import (
    DEFAULT_CONFIG_TOML,
    Config,
    ConfigError,
    config_path,
    load_config,
    validate_raw,
)


@dataclass(frozen=True)
class Field:
    key: str  # dotted path in config.toml, and the name of the form control
    label: str
    kind: str  # "text", "number", "bool" or "choice"
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (value, label), for "choice"


@dataclass(frozen=True)
class Section:
    title: str
    fields: tuple[Field, ...]
    intro: str = ""


CONTEXTS_KEY = "context_files"  # edited as rows of name and path, not as a Field
CONTEXT_BLANK_ROWS = 2

_DEPTH_FIELDS = tuple(
    f
    for depth in ("quick", "standard", "deep")
    for f in (
        Field(
            f"depths.{depth}.model", f"{depth.capitalize()} model", "text", "Empty: CLI default."
        ),
        Field(
            f"depths.{depth}.timeout_minutes", f"{depth.capitalize()} timeout (minutes)", "number"
        ),
    )
)

SECTIONS: tuple[Section, ...] = (
    Section(
        "Runs",
        (
            Field(
                "parallel_runs",
                "Runs at once",
                "number",
                "1 to 3. More wait in the queue. Applies to the queue straight away.",
            ),
            Field(
                "permission_mode",
                "Permission mode",
                "choice",
                "Bypass runs the agent with no permission prompts. Allowlist limits it to web "
                "search, web fetch, your Context files and its own run folder.",
                (("bypass", "Bypass"), ("allowlist", "Allowlist")),
            ),
            Field("language", "Report language", "text"),
            Field(
                "browser",
                "Use the logged-in browser",
                "bool",
                "Run `investigate login` once first.",
            ),
            Field("max_attachment_mb", "Largest attachment (MB)", "number"),
        ),
        "Runs already started keep the settings they began with.",
    ),
    Section("Depths", _DEPTH_FIELDS),
    Section(
        "Tray",
        (
            Field(
                "hotkey",
                "Quick-capture hotkey",
                "text",
                "For example ctrl+alt+i: ctrl, alt, shift or win, then a letter, digit or F1 to "
                "F24. Empty turns it off.",
            ),
            Field(
                "start_at_login",
                "Start the tray when I log in",
                "bool",
            ),
        ),
        "The hotkey and start at login apply as soon as you save, while the tray app is "
        "running. Under `investigate serve` they take effect the next time the tray starts.",
    ),
)

FIELDS = tuple(f for section in SECTIONS for f in section.fields)


class _Missing:
    pass


MISSING = _Missing()


def default_raw() -> dict:
    return tomllib.loads(DEFAULT_CONFIG_TOML)


def get_path(raw: dict, key: str, default: Any = MISSING) -> Any:
    node: Any = raw
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _set_path(raw: dict, key: str, value: Any) -> None:
    *parents, last = key.split(".")
    node = raw
    for part in parents:
        node = node.setdefault(part, {})
    node[last] = value


@dataclass
class FormState:
    """What the page shows: a string or bool per field key, and the Context rows."""

    values: dict[str, str | bool] = field(default_factory=dict)
    contexts: list[tuple[str, str]] = field(default_factory=list)


def _shown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _context_rows(raw_contexts: Any) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(raw_contexts, list):
        for entry in raw_contexts:
            if isinstance(entry, str):
                rows.append((Path(entry).stem, entry))
            elif isinstance(entry, dict):
                rows.append((_shown(entry.get("name")), _shown(entry.get("path"))))
    return rows


def state_from_raw(raw: dict) -> FormState:
    """The file's values, with a default for every key it does not mention."""
    defaults = default_raw()
    state = FormState()
    for f in FIELDS:
        value = get_path(raw, f.key, MISSING)
        if value is MISSING:
            value = get_path(defaults, f.key, "")
        state.values[f.key] = bool(value) if f.kind == "bool" else _shown(value)
    state.contexts = _context_rows(raw.get(CONTEXTS_KEY, []))
    return state


def _number(text: str) -> int | float | str:
    """A number if the text is one, else the text itself so the validator names the problem."""
    text = text.strip()
    for convert in (int, float):
        try:
            value = convert(text)
        except ValueError:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            return text
        return value
    return text


def _form_value(form: dict[str, list[str]], name: str) -> str:
    return (form.get(name) or [""])[0]


def state_from_form(form: dict[str, list[str]]) -> FormState:
    state = FormState()
    for f in FIELDS:
        state.values[f.key] = f.key in form if f.kind == "bool" else _form_value(form, f.key)
    indexes = sorted(
        {
            int(k.rsplit("_", 1)[1])
            for k in form
            if k.startswith(("ctx_name_", "ctx_path_")) and k.rsplit("_", 1)[1].isdigit()
        }
    )
    for i in indexes:
        name, path = _form_value(form, f"ctx_name_{i}"), _form_value(form, f"ctx_path_{i}")
        if name.strip() or path.strip():
            state.contexts.append((name, path))
    return state


def raw_from_state(state: FormState) -> dict:
    """The config.toml shape of what the form holds, typed the way the validator expects."""
    raw: dict = {}
    for f in FIELDS:
        value = state.values.get(f.key, "")
        if f.kind == "number":
            value = _number(str(value))
        _set_path(raw, f.key, value)
    raw[CONTEXTS_KEY] = [{"name": n.strip(), "path": p.strip()} for n, p in state.contexts]
    return raw


def read_raw(home: Path) -> dict:
    """`config.toml` as plain data, creating it with the defaults when it does not exist yet."""
    path = config_path(home)
    if not path.exists():
        load_config(home)
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def current_problems(home: Path, raw: dict) -> dict[str, str]:
    """Problems already in the file (a hand edit), to show next to the fields."""
    return validate_raw(raw, home)[1]


@dataclass
class SaveResult:
    config: Config | None  # set when saved
    errors: dict[str, str]  # by field key; "context_files" for the Context rows
    state: FormState


def _context_array(rows: list[dict]) -> Any:
    array = tomlkit.array()
    array.multiline(True)
    for row in rows:
        table = tomlkit.inline_table()
        table["name"] = row["name"]
        table["path"] = row["path"]
        array.append(table)
    return array


def save_settings(home: Path, form: dict[str, list[str]]) -> SaveResult:
    """Validate the submitted form and, when it is valid, write only the changes to the file.

    Raises ConfigError when the file on disk cannot be read as TOML, since there is nothing safe
    to merge into."""
    state = state_from_form(form)
    new_raw = raw_from_state(state)
    _, errors = validate_raw(new_raw, home)
    if errors:
        return SaveResult(None, errors, state)

    path = config_path(home)
    read_raw(home)  # creates the default file when missing; raises ConfigError when unreadable
    text = path.read_bytes().decode("utf-8")  # keeps CRLF as it is
    file_raw = tomllib.loads(text)
    doc = tomlkit.parse(text)
    defaults = default_raw()

    for f in FIELDS:
        new = get_path(new_raw, f.key)
        old = get_path(file_raw, f.key, MISSING)
        if old is MISSING and new == get_path(defaults, f.key):
            continue  # the user left a default the file never set
        if old is not MISSING and old == new and type(old) is type(new):
            continue
        node = doc
        *parents, last = f.key.split(".")
        for part in parents:
            if part not in node:
                node[part] = tomlkit.table()
            node = node[part]
        node[last] = new
    old_contexts = _context_rows(file_raw.get(CONTEXTS_KEY, []))
    if old_contexts != [(n.strip(), p.strip()) for n, p in state.contexts]:
        doc[CONTEXTS_KEY] = _context_array(new_raw[CONTEXTS_KEY])

    out = tomlkit.dumps(doc)
    # What load_config will read back must be valid and equal what was validated above.
    config, problems = validate_raw(tomllib.loads(out), home)
    if problems:
        raise ConfigError(f"refusing to write an invalid config: {next(iter(problems.values()))}")
    _write_atomically(path, out)
    return SaveResult(config, {}, state)


def _write_atomically(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()

"""Reading, validating and writing config.toml for the Settings page, and the hotkey rules."""

from __future__ import annotations

import tomllib

import pytest

from investigate.config import (
    Config,
    ConfigError,
    Depth,
    PermissionMode,
    config_path,
    load_config,
    parse_hotkey,
    validate_raw,
)
from investigate.settings import FIELDS, read_raw, save_settings, state_from_raw


def _form(home, **changes):
    """The form the page would send for the file as it is now, with `changes` applied. A dotted
    key is passed with `__` for the dot."""
    state = state_from_raw(read_raw(home))
    form = {}
    for f in FIELDS:
        value = state.values[f.key]
        if f.kind == "bool":
            if value:
                form[f.key] = ["on"]
        else:
            form[f.key] = [value]
    for i, (name, path) in enumerate(state.contexts):
        form[f"ctx_name_{i}"], form[f"ctx_path_{i}"] = [name], [path]
    for key, value in changes.items():
        key = key.replace("__", ".")
        if value is False or value is None:
            form.pop(key, None)
        else:
            form[key] = ["on"] if value is True else [value]
    return form


# hotkey rules


@pytest.mark.parametrize(
    ("text", "normal"),
    [
        ("ctrl+alt+i", "ctrl+alt+i"),
        ("Ctrl + Alt + I", "ctrl+alt+i"),
        ("alt+ctrl+shift+f12", "ctrl+alt+shift+f12"),
        ("cmd+shift+9", "shift+win+9"),
        ("control+alt+i", "ctrl+alt+i"),
    ],
)
def test_hotkeys_are_normalised(text, normal):
    assert str(parse_hotkey(text)) == normal


def test_an_empty_hotkey_means_off():
    assert parse_hotkey("") is None
    assert parse_hotkey("   ") is None


@pytest.mark.parametrize(
    "text", ["i", "shift+i", "ctrl+alt", "ctrl+", "ctrl+alt+ii", "ctrl+f25", "hyper+i", "ctrl+é"]
)
def test_bad_hotkeys_are_refused(text):
    with pytest.raises(ValueError):
        parse_hotkey(text)


# validation, shared with load_config


def test_validate_raw_reports_every_bad_field_at_once(home):
    raw = {
        "permission_mode": "yolo",
        "parallel_runs": 9,
        "hotkey": "i",
        "start_at_login": "yes",
        "depths": {"deep": {"timeout_minutes": 0, "model": 3}},
    }
    _, errors = validate_raw(raw, home)
    assert set(errors) == {
        "permission_mode",
        "parallel_runs",
        "hotkey",
        "start_at_login",
        "depths.deep.timeout_minutes",
        "depths.deep.model",
    }


def test_load_config_and_validate_raw_agree(home):
    config_path(home).parent.mkdir(parents=True)
    config_path(home).write_text('hotkey = "alt+shift"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="hotkey"):
        load_config(home)


def test_new_settings_have_defaults(home):
    config = load_config(home)
    assert config.hotkey == "ctrl+alt+i"
    assert config.start_at_login is False
    assert Config().hotkey == "ctrl+alt+i"


def test_the_hotkey_can_be_turned_off_in_the_file(home):
    config_path(home).parent.mkdir(parents=True)
    config_path(home).write_text('hotkey = ""\nstart_at_login = true\n', encoding="utf-8")
    config = load_config(home)
    assert config.hotkey == "" and config.start_at_login is True


# saving


def test_saving_changes_applies_to_the_next_load(home):
    load_config(home)
    result = save_settings(
        home,
        _form(
            home,
            parallel_runs="3",
            permission_mode="allowlist",
            language="German",
            hotkey="Ctrl+Shift+Y",
            start_at_login=True,
            browser=True,
            max_attachment_mb="7.5",
            depths__quick__model="haiku",
            depths__deep__timeout_minutes="60",
        ),
    )
    assert result.errors == {} and result.config is not None
    config = load_config(home)
    assert config == result.config
    assert config.parallel_runs == 3
    assert config.permission_mode is PermissionMode.ALLOWLIST
    assert config.language == "German"
    assert config.hotkey == "ctrl+shift+y"
    assert config.start_at_login and config.browser
    assert config.max_attachment_mb == 7.5
    assert config.depths[Depth.QUICK].model == "haiku"
    assert config.depths[Depth.DEEP].timeout_minutes == 60
    assert config.depths[Depth.DEEP].model == "opus"  # untouched


def test_invalid_values_are_rejected_per_field_and_nothing_is_written(home):
    load_config(home)
    before = config_path(home).read_bytes()
    result = save_settings(
        home,
        _form(
            home,
            parallel_runs="7",
            language="  ",
            max_attachment_mb="lots",
            hotkey="f5",
            depths__standard__timeout_minutes="-1",
        ),
    )
    assert result.config is None
    assert set(result.errors) == {
        "parallel_runs",
        "language",
        "max_attachment_mb",
        "hotkey",
        "depths.standard.timeout_minutes",
    }
    assert "between 1 and 3" in result.errors["parallel_runs"]
    assert config_path(home).read_bytes() == before
    assert result.state.values["parallel_runs"] == "7"  # the page shows what was typed


def test_non_numbers_and_nan_are_not_accepted_as_numbers(home):
    load_config(home)
    for bad in ("nan", "inf", "", "1e999", "2,5"):
        result = save_settings(home, _form(home, max_attachment_mb=bad))
        assert "max_attachment_mb" in result.errors, bad


def test_comments_and_unknown_keys_survive_a_save(home):
    home.mkdir(parents=True)
    config_path(home).write_text(
        '# my own note about the language\nlanguage = "English"\n'
        'future_setting = "keep me"\n\n[depths.quick]\n# quick is for glances\n'
        "timeout_minutes = 5\n\n[inbox]\ninterval = 12\n",
        encoding="utf-8",
    )
    result = save_settings(home, _form(home, language="German", hotkey="ctrl+alt+j"))
    assert result.config is not None
    text = config_path(home).read_text(encoding="utf-8")
    assert "# my own note about the language" in text
    assert "# quick is for glances" in text
    assert 'future_setting = "keep me"' in text
    raw = tomllib.loads(text)
    assert raw["inbox"] == {"interval": 12}
    assert raw["language"] == "German"
    assert raw["hotkey"] == "ctrl+alt+j"  # a new root key is not swallowed by a table
    assert "hotkey" not in raw["depths"]["quick"]


def test_a_default_the_file_never_set_stays_out_of_the_file(home):
    home.mkdir(parents=True)
    config_path(home).write_text('language = "English"\n', encoding="utf-8")
    save_settings(home, _form(home, language="French"))
    assert tomllib.loads(config_path(home).read_text(encoding="utf-8")) == {"language": "French"}


def test_an_unchanged_save_leaves_the_file_byte_identical(home):
    load_config(home)
    before = config_path(home).read_bytes()
    assert save_settings(home, _form(home)).config is not None
    assert config_path(home).read_bytes() == before


def test_contexts_are_added_edited_and_removed(home):
    load_config(home)
    form = _form(home)
    form["ctx_name_0"], form["ctx_path_0"] = ["work"], ["~/notes/work.md"]
    form["ctx_name_1"], form["ctx_path_1"] = ["studies"], ["notes/uni.md"]
    form["ctx_name_2"], form["ctx_path_2"] = [""], [""]  # a blank row is ignored
    assert save_settings(home, form).errors == {}
    config = load_config(home)
    assert [c.name for c in config.contexts] == ["work", "studies"]
    assert tomllib.loads(config_path(home).read_text(encoding="utf-8"))["context_files"][0] == {
        "name": "work",
        "path": "~/notes/work.md",
    }

    form = _form(home)
    form["ctx_name_0"], form["ctx_path_0"] = [""], [""]  # clearing a row removes the Context
    assert save_settings(home, form).errors == {}
    assert [c.name for c in load_config(home).contexts] == ["studies"]


def test_context_problems_are_reported_on_the_contexts(home):
    load_config(home)
    form = _form(home)
    form["ctx_name_0"], form["ctx_path_0"] = ["a"], ["x.md"]
    form["ctx_name_1"], form["ctx_path_1"] = ["a"], ["y.md"]
    assert "used twice" in save_settings(home, form).errors["context_files"]
    form["ctx_name_1"], form["ctx_path_1"] = ["b"], [""]
    assert "non-empty path" in save_settings(home, form).errors["context_files"]


def test_an_unreadable_file_is_an_error_not_overwritten(home):
    form = _form(home, language="German")
    config_path(home).write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        save_settings(home, form)
    assert config_path(home).read_text(encoding="utf-8") == "this is = = not toml"


def test_no_temp_file_is_left_behind(home):
    load_config(home)
    save_settings(home, _form(home, language="Dutch"))
    assert sorted(p.name for p in home.iterdir()) == ["config.toml"]


def test_a_file_edited_by_hand_with_a_problem_still_shows_its_values(home):
    home.mkdir(parents=True)
    config_path(home).write_text("parallel_runs = 9\n", encoding="utf-8")
    state = state_from_raw(read_raw(home))
    assert state.values["parallel_runs"] == "9"
    assert state.values["hotkey"] == "ctrl+alt+i"

import pytest

from investigate.config import (
    ConfigError,
    Depth,
    PermissionMode,
    config_path,
    data_home,
    load_config,
)
from investigate.result import parse_result
from tests.fakes import GOOD_RESULT


def test_data_home_uses_env_override(home):
    assert data_home() == home


def test_data_home_defaults_to_dot_investigate(monkeypatch):
    monkeypatch.delenv("INVESTIGATE_HOME")
    assert data_home().name == ".investigate"


def test_load_config_creates_defaults(home):
    config = load_config(home)
    assert config_path(home).exists()
    assert config.permission_mode is PermissionMode.BYPASS
    assert config.language == "English"
    assert config.depths[Depth.QUICK].timeout_minutes == 5
    assert config.depths[Depth.STANDARD].timeout_minutes == 15
    assert config.depths[Depth.STANDARD].model is None
    assert config.depths[Depth.DEEP].timeout_minutes == 45
    assert config.depths[Depth.DEEP].model == "opus"


def test_load_config_overrides(home):
    home.mkdir()
    config_path(home).write_text(
        'permission_mode = "allowlist"\nlanguage = "German"\n[depths.quick]\ntimeout_minutes = 2\n',
        encoding="utf-8",
    )
    config = load_config(home)
    assert config.permission_mode is PermissionMode.ALLOWLIST
    assert config.language == "German"
    assert config.depths[Depth.QUICK].timeout_minutes == 2
    assert config.depths[Depth.DEEP].timeout_minutes == 45


@pytest.mark.parametrize(
    "text",
    [
        'permission_mode = "yolo"',
        "language = 3",
        "context_files = 'x'",
        "[depths.quick]\ntimeout_minutes = 0",
        "[depths.huge]\ntimeout_minutes = 1",
        "not toml [",
    ],
)
def test_load_config_rejects_bad_values(home, text):
    home.mkdir()
    config_path(home).write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(home)


def test_parse_valid_result():
    parsed = parse_result(GOOD_RESULT)
    assert not parsed.problems
    assert parsed.structured.title == "Found a thing"
    assert parsed.structured.findings[0].citations[0].url == "https://example.com/docs"
    assert "Something was found." in parsed.body
    assert "```" not in parsed.body


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   \n",
        "just prose, no block",
        "```json\n{not json}\n```",
        '```json\n["a"]\n```',
        '```json\n{"title": "t", "summary": "s"}\n```',
        '```json\n{"title": "", "summary": "s", "findings": []}\n```',
        '```json\n{"title":"t","summary":"s","findings":[{"claim":"c","confidence":"sure"}]}\n```',
        '```json\n{"title":"t","summary":"s","findings":[1]}\n```',
        '```json\n{"title":"t","summary":"s","findings":'
        '[{"claim":"c","confidence":"low","citations":[{"title":"x"}]}]}\n```',
    ],
)
def test_parse_bad_result_never_raises(text):
    parsed = parse_result(text)
    assert parsed.structured is None
    assert parsed.problems


def test_parse_uses_last_json_block():
    text = '```json\n{"a": 1}\n```\ntext\n' + GOOD_RESULT
    assert parse_result(text).structured is not None

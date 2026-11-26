"""Fixes from the PR #20 review: per-home startup entries, no console windows, config.toml
replace retry, and non-finite numbers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from investigate.autostart import (
    WindowsRunKey,
    default_autostart,
    entry_suffix,
    is_ephemeral,
    startup_command,
)
from investigate.config import ConfigError, config_path, default_home, load_config, validate_raw
from investigate.settings import save_settings
from tests.fakes import FakeRunner
from tests.test_autostart_hotkeys import FakeWinreg
from tests.test_settings import _form
from tests.test_settings_server import _tray
from tests.test_tray import RecordingNotifier

REAL = ["C:\\real\\pythonw.exe", "-m", "investigate.cli", "tray"]
REAL_VALUE = subprocess.list2cmdline(REAL)


def _real_entry(reg):
    WindowsRunKey(reg, "Investigate").enable(REAL)


def _stored(reg):
    return next(iter(reg.keys.values()))


# 1. the startup entry belongs to one data folder


def test_a_tray_on_another_home_with_the_flag_off_leaves_the_real_entry_alone(tmp_path):
    reg = FakeWinreg()
    _real_entry(reg)
    other = tmp_path / "other-home"
    app = _tray(other, autostart=WindowsRunKey(reg, "Investigate" + entry_suffix(other)))
    app.start()
    try:
        app.activate()  # start_at_login is false in the new home's config
    finally:
        app.shutdown()
    assert _stored(reg) == {"Investigate": REAL_VALUE}


def test_each_home_gets_its_own_entry_name_and_the_default_keeps_the_plain_one(tmp_path):
    assert entry_suffix(None) == ""
    a, b = entry_suffix(tmp_path / "a"), entry_suffix(tmp_path / "b")
    assert a.startswith("-") and b.startswith("-") and a != b
    assert entry_suffix(tmp_path / "a") == a  # stable
    assert entry_suffix(default_home()) == ""


def test_default_autostart_names_files_per_home(tmp_path):
    plain = default_autostart("linux", tmp_path, {})
    custom = default_autostart("linux", tmp_path, {}, suffix="-abc123")
    assert plain.path.name == "investigate.desktop"
    assert custom.path.name == "investigate-abc123.desktop"
    mac = default_autostart("darwin", tmp_path, {}, suffix="-abc123")
    assert mac.path.name == "app.investigate.tray-abc123.plist"


def test_the_command_carries_a_non_default_home(tmp_path):
    custom = tmp_path / "data"
    python = tmp_path / "bin" / "python"  # no gui-script next to it: the module form
    assert startup_command(str(python), home=custom)[-3:] == ["tray", "--home", str(custom)]
    assert "--home" not in startup_command(str(python), home=default_home())
    assert "--home" not in startup_command(str(python))


def test_tray_takes_a_home_option_and_names_its_entry_after_it(tmp_path, monkeypatch):
    import investigate.cli as cli

    seen = {}
    monkeypatch.setattr(
        cli, "run_tray", lambda home, runner, gui, **kw: seen.update(home=home, **kw) or 0
    )
    monkeypatch.setattr(cli, "default_autostart", lambda suffix="": ("auto", suffix))
    assert cli.main(["tray", "--home", str(tmp_path / "h")], runner=FakeRunner()) == 0
    chosen = (tmp_path / "h").resolve()
    assert seen["home"] == chosen
    assert seen["autostart"] == ("auto", entry_suffix(chosen))


def test_the_default_home_keeps_the_plain_entry_name(monkeypatch):
    import investigate.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "run_tray", lambda home, runner, gui, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(cli, "default_autostart", lambda suffix="": ("auto", suffix))
    monkeypatch.setattr(cli, "data_home", default_home)
    cli.main(["tray"], runner=FakeRunner())
    assert seen["autostart"] == ("auto", "")


def test_self_repair_does_not_move_an_entry_to_a_temporary_interpreter(home, tmp_path):
    reg = FakeWinreg()
    _real_entry(reg)
    home.mkdir(parents=True, exist_ok=True)
    config_path(home).write_text("start_at_login = true\n", encoding="utf-8")
    scratch = tmp_path / "wt" / ".venv" / "Scripts" / "pythonw.exe"
    notifier = RecordingNotifier()
    app = _tray(
        home,
        autostart=WindowsRunKey(reg, "Investigate"),
        notifier=notifier,
        get_startup_command=lambda: [str(scratch), "-m", "investigate.cli", "tray"],
    )
    app.start()
    try:
        app.activate()
    finally:
        app.shutdown()
    assert _stored(reg)["Investigate"] == REAL_VALUE  # unchanged
    assert [t.title for t in notifier.toasts] == ["Start at login not updated"]


def test_self_repair_still_fixes_an_entry_when_the_new_interpreter_is_a_real_install(
    home, monkeypatch
):
    reg = FakeWinreg()
    _real_entry(reg)
    home.mkdir(parents=True, exist_ok=True)
    config_path(home).write_text("start_at_login = true\n", encoding="utf-8")
    monkeypatch.setattr("investigate.tray.is_ephemeral", lambda path: False)
    app = _tray(
        home,
        autostart=WindowsRunKey(reg, "Investigate"),
        get_startup_command=lambda: ["D:\\new\\pythonw.exe", "-m", "investigate.cli", "tray"],
    )
    app.start()
    try:
        app.activate()
    finally:
        app.shutdown()
    assert _stored(reg)["Investigate"].startswith("D:\\new\\pythonw.exe")


def test_turning_it_on_from_a_temporary_interpreter_still_works_but_warns(home, tmp_path):
    reg = FakeWinreg()
    notifier = RecordingNotifier()
    scratch = tmp_path / "wt" / "python.exe"
    app = _tray(
        home,
        autostart=WindowsRunKey(reg, "Investigate"),
        notifier=notifier,
        get_startup_command=lambda: [str(scratch), "-m", "investigate.cli", "tray"],
    )
    from investigate.config import Config

    app.apply_config(Config(start_at_login=True))
    assert "Investigate" in _stored(reg)  # the user asked for it
    assert [t.title for t in notifier.toasts] == ["Start at login uses a temporary install"]


def test_is_ephemeral_flags_temp_folders_and_git_worktrees(tmp_path):
    elsewhere = tmp_path / "not-temp"
    worktree = tmp_path / "wt"
    (worktree / ".venv").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../.git/worktrees/wt", encoding="utf-8")
    main = tmp_path / "main"
    (main / ".venv").mkdir(parents=True)
    (main / ".git").mkdir()
    assert is_ephemeral(tmp_path / "x" / "python.exe", temp=tmp_path)
    assert is_ephemeral(worktree / ".venv" / "python.exe", temp=elsewhere)
    assert not is_ephemeral(main / ".venv" / "python.exe", temp=elsewhere)
    assert not is_ephemeral(Path("C:/Python/python.exe"), temp=elsewhere)


# 2. no console windows


def test_console_flags():
    from investigate import runner as runner_module

    assert runner_module.console_flags("linux") == {}
    assert runner_module.console_flags("win32") == {"creationflags": 0x08000000}


@pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows matter")
def test_the_agent_and_taskkill_are_started_without_a_console_window(tmp_path, monkeypatch):
    from investigate import runner as runner_module
    from tests.test_runner import _request

    seen = []

    def fake_popen(*args, **kwargs):
        seen.append(kwargs)
        raise OSError("stop here")

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    runner_module.ClaudeCodeRunner(command=["claude"]).run(_request(tmp_path))
    assert seen[0]["creationflags"] & 0x08000000

    calls = []
    monkeypatch.setattr(runner_module.subprocess, "run", lambda *a, **kw: calls.append(kw))

    class Proc:
        pid = 1

        def kill(self):
            pass

    runner_module._kill_tree(Proc())
    assert calls[0]["creationflags"] & 0x08000000


# 3. replacing config.toml while another thread has it open


def test_the_config_replace_is_retried_on_a_sharing_violation(home, monkeypatch):
    load_config(home)
    form = _form(home, language="Dutch")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    assert save_settings(home, form).config is not None
    assert len(calls) == 3
    assert load_config(home).language == "Dutch"


# 4. nan and inf


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("line", ["max_attachment_mb = {}", "[depths.deep]\ntimeout_minutes = {}"])
def test_non_finite_numbers_in_the_file_are_rejected_like_in_the_form(home, bad, line):
    home.mkdir(parents=True, exist_ok=True)
    config_path(home).write_text(line.format(bad) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="positive number"):
        load_config(home)


def test_validate_raw_rejects_non_finite_numbers(home):
    _, errors = validate_raw(
        {"max_attachment_mb": float("nan"), "depths": {"quick": {"timeout_minutes": float("inf")}}},
        home,
    )
    assert set(errors) == {"max_attachment_mb", "depths.quick.timeout_minutes"}

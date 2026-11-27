from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from investigate import browser, cli
from investigate.config import Config, Depth, PermissionMode, load_config
from investigate.profile_lock import ProfileBusy, profile_lock
from investigate.prompt import build_prompt
from investigate.runner import ClaudeCodeRunner, RunRequest, build_command
from investigate.service import execute_run
from tests.fakes import FakeRunner

STUB = str(Path(__file__).with_name("stub_claude.py"))


def _request(workdir, **overrides):
    values = dict(
        prompt="hi",
        workdir=workdir,
        session_id="11111111-1111-1111-1111-111111111111",
        timeout_seconds=20,
        model=None,
        permission_mode=PermissionMode.ALLOWLIST,
    )
    values.update(overrides)
    return RunRequest(**values)


def test_profile_lives_in_the_data_folder(home):
    assert browser.profile_dir(home) == home / "browser-profile"


def test_browser_is_disabled_by_default(home):
    assert load_config(home).browser is False
    assert load_config(home).browser is False  # the written default file parses the same


def test_config_enables_browser(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text("browser = true\n", encoding="utf-8")
    assert load_config(home).browser is True


def test_mcp_config_has_playwright_on_the_profile(tmp_path):
    profile = tmp_path / "prof"
    runner = ClaudeCodeRunner(command=[sys.executable, STUB])
    runner.run(_request(tmp_path, browser_profile=profile))
    server = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["playwright"]
    assert any(x.startswith("@playwright/mcp@0.") for x in server["args"])
    assert server["args"][server["args"].index("--user-data-dir") + 1] == str(profile)
    assert "--isolated" not in server["args"]
    assert "--headless" not in server["args"]


def test_mcp_config_is_empty_without_browser(tmp_path):
    ClaudeCodeRunner(command=[sys.executable, STUB]).run(_request(tmp_path))
    assert json.loads((tmp_path / "mcp.json").read_text()) == {"mcpServers": {}}


def test_allowlist_allows_playwright_tools_only_with_browser(tmp_path):
    with_browser = build_command(["claude"], _request(tmp_path, browser_profile=tmp_path))
    without = build_command(["claude"], _request(tmp_path))
    allowed = with_browser[
        with_browser.index("--allowedTools") + 1 : with_browser.index("--disallowedTools")
    ]
    assert "mcp__playwright__*" not in allowed
    assert "mcp__playwright__browser_navigate" in allowed
    assert "mcp__playwright__browser_snapshot" in allowed
    for risky in ("run_code_unsafe", "evaluate", "file_upload"):
        assert f"mcp__playwright__browser_{risky}" not in allowed
    assert not any(a.startswith("mcp__playwright") for a in without)
    assert "--strict-mcp-config" in with_browser


def test_prompt_states_the_read_only_rule_and_logged_out_handling():
    prompt = build_prompt("x", Depth.QUICK, "English", browser=True)
    assert "read-only on every platform" in prompt
    for verb in ("like", "follow", "comment", "save", "post", "message"):
        assert verb in prompt
    assert "Not logged in to <site>" in prompt


def test_prompt_without_browser_has_no_browser_instructions():
    assert "Playwright" not in build_prompt("x", Depth.QUICK, "English")
    assert "not available" in build_prompt("x", Depth.QUICK, "English", browser=False)


def test_run_gets_the_profile_when_enabled(home):
    browser.mark_logged_in(home)
    runner = FakeRunner()
    execute_run("q", Depth.QUICK, home=home, runner=runner, config=Config(browser=True))
    assert runner.requests[0].browser_profile == browser.profile_dir(home)
    assert "read-only on every platform" in runner.requests[0].prompt


def test_run_has_no_profile_when_disabled(home):
    runner = FakeRunner()
    execute_run("q", Depth.QUICK, home=home, runner=runner, config=Config())
    assert runner.requests[0].browser_profile is None


def test_lock_is_exclusive(home):
    with profile_lock(home):
        with pytest.raises(ProfileBusy):
            with profile_lock(home):
                pass
    with profile_lock(home):  # free again after release
        pass


def test_lock_is_released_when_the_holder_raises(home):
    with pytest.raises(ValueError):
        with profile_lock(home):
            raise ValueError
    with profile_lock(home):
        pass


class _BlockingRunner(FakeRunner):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered, self.release = entered, release

    def run(self, request):
        self.entered.set()
        assert self.release.wait(10)
        return super().run(request)


def test_two_concurrent_browser_runs_never_share_the_profile(home):
    browser.mark_logged_in(home)
    entered, release = threading.Event(), threading.Event()
    first = _BlockingRunner(entered, release)
    second = FakeRunner()
    config = Config(browser=True)
    t = threading.Thread(
        target=lambda: execute_run("one", Depth.QUICK, home=home, runner=first, config=config)
    )
    t.start()
    try:
        assert entered.wait(10)
        execute_run("two", Depth.QUICK, home=home, runner=second, config=config)
    finally:
        release.set()
        t.join(10)
    assert first.requests[0].browser_profile is not None
    assert second.requests[0].browser_profile is None  # proceeded without the browser
    assert "not available" in second.requests[0].prompt
    # and the lock is free once the first Run finished
    third = FakeRunner()
    execute_run("three", Depth.QUICK, home=home, runner=third, config=config)
    assert third.requests[0].browser_profile is not None


def test_login_launches_on_the_profile_and_holds_the_lock(home):
    seen = {}

    def launch(profile, urls):
        seen["profile"], seen["urls"] = profile, urls
        with pytest.raises(ProfileBusy):
            with profile_lock(home):
                pass
        return 0

    assert browser.login(home, launch) == 0
    assert seen["profile"] == browser.profile_dir(home) and seen["profile"].is_dir()
    assert any("youtube" in u for u in seen["urls"])
    with profile_lock(home):
        pass


def test_login_refuses_when_profile_is_in_use(home, capsys):
    with profile_lock(home):
        assert browser.login(home, lambda p, u: 0) == 1
    assert "in use" in capsys.readouterr().err


def test_cli_login(home, monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "login", lambda h: calls.append(h) or 0)
    assert cli.main(["login"]) == 0
    assert calls == [home]


RISKY = [
    "mcp__playwright__browser_run_code_unsafe",
    "mcp__playwright__browser_evaluate",
    "mcp__playwright__browser_file_upload",
]


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_dangerous_playwright_tools_are_denied_in_both_modes(tmp_path, mode):
    cmd = build_command(
        ["claude"], _request(tmp_path, permission_mode=mode, browser_profile=tmp_path)
    )
    denied = cmd[cmd.index("--disallowedTools") + 1 :]
    for tool in RISKY:
        assert tool in denied


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_no_deny_rules_without_the_browser(tmp_path, mode):
    assert "--disallowedTools" not in build_command(
        ["claude"], _request(tmp_path, permission_mode=mode)
    )


def test_playwright_mcp_is_pinned():
    args = browser.playwright_server(Path("p"))["args"]
    spec = next(a for a in args if a.startswith("@playwright/mcp"))
    assert spec != "@playwright/mcp@latest"
    assert spec.split("@")[-1][0].isdigit()


def test_flag_without_login_marker_runs_without_the_browser(home):
    runner = FakeRunner()
    result = execute_run("q", Depth.QUICK, home=home, runner=runner, config=Config(browser=True))
    assert runner.requests[0].browser_profile is None
    assert "not available" in runner.requests[0].prompt
    assert "investigate login" in (result.folder / "agent.log").read_text(encoding="utf-8")


def test_login_writes_the_marker_after_the_browser_closes(home):
    assert not browser.is_logged_in(home)
    assert browser.login(home, lambda p, u: 0) == 0
    assert browser.is_logged_in(home)


def test_failed_login_leaves_no_marker(home):
    assert browser.login(home, lambda p, u: 3) == 3
    assert not browser.is_logged_in(home)


def test_login_needs_chrome(home, monkeypatch, capsys):
    monkeypatch.setattr(browser, "_find_chrome", lambda: None)
    assert browser.login(home) == 1
    assert "Chrome" in capsys.readouterr().err
    assert not browser.is_logged_in(home)


def _hold_profile_open(profile):
    """Make the profile look open in a browser; returns a closer."""
    profile.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        handle = open(profile / "lockfile", "wb")  # noqa: SIM115
        return handle.close
    link = profile / "SingletonLock"
    link.symlink_to(f"somehost-{os.getpid()}")
    return link.unlink


def test_profile_in_use_is_detected(home):
    profile = browser.profile_dir(home)
    assert not browser.profile_in_use(profile)
    close = _hold_profile_open(profile)
    try:
        assert browser.profile_in_use(profile)
    finally:
        close()
    assert not browser.profile_in_use(profile)


def test_login_refuses_a_profile_already_open_in_a_browser(home, capsys):
    close = _hold_profile_open(browser.profile_dir(home))
    try:
        launched = []
        assert browser.login(home, lambda p, u: launched.append(p) or 0) == 1
    finally:
        close()
    assert not launched
    assert "already open" in capsys.readouterr().err


def test_launcher_kills_the_browser_tree_when_interrupted(home, monkeypatch):
    killed = []

    class Proc:
        def wait(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(browser, "_find_chrome", lambda: "chrome")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(browser, "_kill_tree", lambda proc: killed.append(proc))
    with pytest.raises(KeyboardInterrupt):
        browser._launch_and_wait(browser.profile_dir(home), [])
    assert len(killed) == 1


def test_run_end_cleans_up_orphaned_posix_browsers(home, monkeypatch):
    calls = []
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(browser.subprocess, "run", lambda argv, **k: calls.append(argv))
    browser.kill_profile_browsers(browser.profile_dir(home))
    assert calls and calls[0][0] == "pkill"
    assert f"--user-data-dir={browser.profile_dir(home)}" in calls[0]


def test_service_cleans_up_the_profile_after_a_browser_run(home, monkeypatch):
    browser.mark_logged_in(home)
    cleaned = []
    monkeypatch.setattr("investigate.service.kill_profile_browsers", cleaned.append)
    execute_run("q", Depth.QUICK, home=home, runner=FakeRunner(), config=Config(browser=True))
    assert cleaned == [browser.profile_dir(home)]


def test_allowlist_keeps_attachments_rule_with_the_browser(tmp_path):
    cmd = build_command(["claude"], _request(tmp_path, browser_profile=tmp_path))
    assert any(a.startswith("Read(") and a.endswith("/attachments/**)") for a in cmd)
    assert "mcp__playwright__browser_navigate" in cmd


@pytest.mark.skipif(sys.platform != "win32", reason="the lockfile marker is Windows only")
def test_profile_in_use_never_deletes_chromes_lockfile(home):
    profile = browser.profile_dir(home)
    profile.mkdir(parents=True)
    lockfile = profile / "lockfile"
    lockfile.write_bytes(b"")  # left behind, nobody holds it
    assert not browser.profile_in_use(profile)
    assert lockfile.exists()  # a probe must not race a Chrome that is about to open it
    handle = open(lockfile, "wb")  # noqa: SIM115
    try:
        assert browser.profile_in_use(profile)
        assert lockfile.exists()
    finally:
        handle.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the lockfile marker is Windows only")
def test_profile_in_use_treats_a_vanishing_lockfile_as_not_in_use(home, monkeypatch):
    profile = browser.profile_dir(home)
    profile.mkdir(parents=True)
    (profile / "lockfile").write_bytes(b"")
    real_exists = type(profile).exists
    monkeypatch.setattr(
        type(profile), "exists", lambda self: True if self.name == "lockfile" else real_exists(self)
    )
    (profile / "lockfile").unlink()  # Chrome exits between the check and the probe
    assert not browser.profile_in_use(profile)

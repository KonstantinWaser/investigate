from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

from investigate import cli
from investigate.config import config_path, load_config
from investigate.setup import SetupIO, run_setup

LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})


class FakeIO:
    """Scripted answers and canned lookups; records everything it was asked and told."""

    def __init__(
        self,
        answers: list[str] | None = None,
        *,
        claude: str | None = "/bin/claude",
        status: tuple[int, str] = (0, LOGGED_IN),
        tools: set[str] | None = None,
        modules: set[str] | None = None,
        chrome: bool = True,
        platform: str = "win32",
        login_code: int = 0,
    ) -> None:
        self.answers = list(answers or [])
        self.said: list[str] = []
        self.asked: list[str] = []
        self.commands: list[list[str]] = []
        self.logins: list[Path] = []
        self.claude, self.status, self.chrome = claude, status, chrome
        self.tools = {"ffmpeg"} if tools is None else tools
        self.modules = {"pystray"} if modules is None else modules
        self.platform, self.login_code = platform, login_code

    def build(self) -> SetupIO:
        def ask(prompt: str) -> str:
            self.asked.append(prompt)
            return self.answers.pop(0) if self.answers else ""

        def which(name: str) -> str | None:
            if name == "claude":
                return self.claude
            return "/bin/" + name if name in self.tools else None

        def run(argv: list[str]) -> tuple[int, str]:
            self.commands.append(argv)
            return self.status

        def login(home: Path) -> int:
            self.logins.append(home)
            return self.login_code

        return SetupIO(
            ask=ask,
            say=self.said.append,
            which=which,
            run=run,
            has_module=lambda name: name in self.modules,
            login=login,
            chrome_found=lambda: self.chrome,
            platform=self.platform,
        )

    @property
    def output(self) -> str:
        return "\n".join(self.said)


# Prompt order: Context path (blank ends), browser login, start at login.


def test_first_run_creates_the_data_folder_and_default_config(home):
    io = FakeIO()
    assert run_setup(home, io.build()) == 0
    assert config_path(home).is_file()
    assert load_config(home).context_files == ()
    assert "logged in" in io.output


def test_login_check_uses_the_cheap_status_command_only(home):
    io = FakeIO()
    run_setup(home, io.build())
    assert io.commands == [["/bin/claude", "auth", "status"]]


def test_logged_out_cli_is_reported_with_the_other_ways_in(home):
    io = FakeIO(status=(1, json.dumps({"loggedIn": False})))
    run_setup(home, io.build())
    assert "not logged in" in io.output
    assert "claude auth login" in io.output
    assert "API key" in io.output


def test_status_command_that_cannot_be_read_says_login_not_verified(home):
    io = FakeIO(status=(2, "unknown command"))
    assert run_setup(home, io.build()) == 0
    assert "login not verified" in io.output


def test_missing_cli_is_an_error_but_the_other_steps_still_run(home):
    io = FakeIO(claude=None)
    assert run_setup(home, io.build()) == 1
    assert "not found" in io.output
    assert io.commands == []
    assert config_path(home).is_file()


def test_existing_config_is_left_byte_for_byte(home):
    home.mkdir(parents=True)
    path = config_path(home)
    path.write_text('# mine\nlanguage = "German"\n', encoding="utf-8")
    before = path.read_bytes()
    io = FakeIO()
    run_setup(home, io.build())
    assert path.read_bytes() == before
    assert "already exists" in io.output


def test_second_run_changes_nothing(home, tmp_path):
    notes = tmp_path / "notes.md"
    first = FakeIO([str(notes), "y", "", "n", "n"])
    run_setup(home, first.build())
    files = [config_path(home), notes]
    snapshot = {p: p.read_bytes() for p in files}
    run_setup(home, FakeIO().build())
    assert {p: p.read_bytes() for p in files} == snapshot
    assert sorted(p.name for p in home.iterdir()) == ["config.toml"]


def test_context_file_is_registered_and_a_starter_is_created_on_yes(home, tmp_path):
    notes = tmp_path / "work.md"
    io = FakeIO([str(notes), "y", ""])
    run_setup(home, io.build())
    config = load_config(home)
    assert [(c.name, c.path) for c in config.contexts] == [("work", str(notes.resolve()))]
    assert notes.read_text(encoding="utf-8").startswith("#")


def test_missing_context_file_is_not_created_without_a_yes(home, tmp_path):
    notes = tmp_path / "work.md"
    run_setup(home, FakeIO([str(notes), "", ""]).build())
    assert not notes.exists()
    assert load_config(home).contexts == ()


def test_existing_context_file_is_registered_and_never_rewritten(home, tmp_path):
    notes = tmp_path / "work.md"
    notes.write_text("my own notes", encoding="utf-8")
    io = FakeIO([str(notes), ""])
    run_setup(home, io.build())
    assert notes.read_text(encoding="utf-8") == "my own notes"
    assert len(load_config(home).contexts) == 1
    assert not any("Create" in q for q in io.asked)


def test_existing_config_comments_survive_adding_a_context(home, tmp_path):
    home.mkdir(parents=True)
    config_path(home).write_text(
        '# keep me\nlanguage = "German"\ncontext_files = []\n', encoding="utf-8"
    )
    notes = tmp_path / "a.md"
    notes.write_text("x", encoding="utf-8")
    run_setup(home, FakeIO([str(notes), ""]).build())
    text = config_path(home).read_text(encoding="utf-8")
    assert "# keep me" in text
    assert tomllib.loads(text)["language"] == "German"
    assert len(load_config(home).contexts) == 1


def test_context_already_listed_is_not_added_twice(home, tmp_path):
    notes = tmp_path / "a.md"
    notes.write_text("x", encoding="utf-8")
    run_setup(home, FakeIO([str(notes), ""]).build())
    io = FakeIO([str(notes), ""])
    run_setup(home, io.build())
    assert len(load_config(home).contexts) == 1
    assert "already" in io.output


def test_extras_are_reported_with_how_to_install_them(home):
    io = FakeIO(tools=set(), modules={"pystray"})
    run_setup(home, io.build())
    assert "ffmpeg" in io.output
    assert "uv sync --extra transcribe" in io.output
    ok = FakeIO(tools={"ffmpeg"}, modules={"pystray", "faster_whisper"})
    run_setup(home, ok.build())
    assert "Transcription is ready" in ok.output


def test_browser_login_only_runs_on_a_yes(home):
    declined = FakeIO(["", ""])
    run_setup(home, declined.build())
    assert declined.logins == []
    accepted = FakeIO(["", "y"])
    run_setup(home, accepted.build())
    assert accepted.logins == [home]


def test_browser_login_is_skipped_when_already_done_or_no_chrome(home, tmp_path):
    home.mkdir(parents=True)
    (home / "logged_in_at").write_text("now", encoding="utf-8")
    done = FakeIO(["", "y", "y"])
    run_setup(home, done.build())
    assert done.logins == []
    nochrome = FakeIO(["", "y"], chrome=False)
    run_setup(tmp_path / "other", nochrome.build())
    assert nochrome.logins == []
    assert "Chrome" in nochrome.output


def test_start_at_login_is_written_to_config_only_on_a_yes(home):
    run_setup(home, FakeIO().build())
    assert load_config(home).start_at_login is False
    run_setup(home, FakeIO(["", "", "y"]).build())
    assert load_config(home).start_at_login is True


def test_start_at_login_needs_the_tray_extra(home):
    io = FakeIO(["", "", "y"], modules=set())
    run_setup(home, io.build())
    assert load_config(home).start_at_login is False
    assert "uv sync --extra tray" in io.output


def test_windows_gets_the_overflow_hint_and_other_systems_do_not(home):
    win = FakeIO(platform="win32")
    run_setup(home, win.build())
    assert "drag the Investigate icon" in win.output
    mac = FakeIO(platform="darwin")
    run_setup(home, mac.build())
    assert "drag the Investigate icon" not in mac.output


def test_end_of_input_takes_the_safe_default(home):
    io = FakeIO()

    def eof(prompt: str) -> str:
        raise EOFError

    run_setup(home, replace(io.build(), ask=eof))
    assert load_config(home).contexts == ()
    assert io.logins == []


def test_invalid_existing_config_stops_with_a_clear_error(home):
    home.mkdir(parents=True)
    config_path(home).write_text("parallel_runs = 99\n", encoding="utf-8")
    io = FakeIO()
    assert run_setup(home, io.build()) == 2
    assert "parallel_runs" in io.output
    assert config_path(home).read_text(encoding="utf-8") == "parallel_runs = 99\n"


def test_cli_setup_command_is_wired(home, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_setup", lambda h, io=None: seen.append(h) or 0)
    assert cli.main(["setup"]) == 0
    assert seen == [home]


def test_quoted_context_path_from_explorer_is_accepted(home, tmp_path):
    notes = tmp_path / "work.md"
    notes.write_text("x", encoding="utf-8")
    run_setup(home, FakeIO([f'"{notes}"', ""]).build())
    assert [c.path for c in load_config(home).contexts] == [str(notes.resolve())]


def test_unusable_context_path_is_reported_and_setup_carries_on(home, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    bad = blocker / "sub" / "work.md"  # a folder cannot be made under a file
    io = FakeIO([str(bad), "y", ""])
    assert run_setup(home, io.build()) == 0
    assert "Could not" in io.output
    assert load_config(home).contexts == ()
    assert "Next" in io.output


def test_context_file_with_a_blank_name_is_reported_not_raised(home, tmp_path):
    odd = tmp_path / " .md"
    odd.write_text("x", encoding="utf-8")
    io = FakeIO([str(odd), ""])
    assert run_setup(home, io.build()) == 0
    assert load_config(home).contexts == ()


# the login check can never start a model session


class _FakeProc:
    """A stand-in for Popen: records how it was started and can hang on the first read."""

    instances: list = []

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs, self.pid, self.returncode = argv, kwargs, 4242, 0
        self.hang, self.communicates = _FakeProc.hang, []
        _FakeProc.instances.append(self)

    hang = False

    def communicate(self, timeout=None):
        import subprocess

        self.communicates.append(timeout)
        if self.hang and len(self.communicates) == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return LOGGED_IN, None

    def kill(self):
        pass


def test_status_check_closes_stdin_and_has_a_timeout(monkeypatch):
    import subprocess

    from investigate import setup

    _FakeProc.instances, _FakeProc.hang = [], False
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    assert setup._run(["claude", "auth", "status"]) == (0, LOGGED_IN)
    proc = _FakeProc.instances[0]
    assert proc.kwargs["stdin"] is subprocess.DEVNULL
    assert proc.communicates == [setup.STATUS_TIMEOUT_SECONDS]


def test_status_that_hangs_is_killed_with_its_children_and_never_waited_on_again(monkeypatch):
    import subprocess

    import pytest

    from investigate import runner, setup

    _FakeProc.instances, _FakeProc.hang = [], True
    killed = []
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(runner, "_kill_tree", killed.append)
    with pytest.raises(subprocess.TimeoutExpired):
        setup._run(["claude", "auth", "status"])
    proc = _FakeProc.instances[0]
    assert killed == [proc]  # the whole tree, so a claude.cmd shim cannot keep the pipes open
    assert all(t is not None for t in proc.communicates)  # no second unbounded wait


def test_status_that_times_out_or_is_not_json_is_unverified():
    import subprocess

    from investigate.setup import agent_status

    def hangs(argv):
        raise subprocess.TimeoutExpired(argv, 20)

    for run in (hangs, lambda argv: (0, "not json"), lambda argv: (0, "[1]"), lambda argv: (0, "")):
        io = replace(FakeIO().build(), run=run)
        status = agent_status(io)
        assert status.installed and status.logged_in is None


def test_agent_status_reads_logged_in_and_out_and_missing():
    from investigate.setup import agent_status

    assert agent_status(FakeIO().build()).logged_in is True
    out = FakeIO(status=(0, json.dumps({"loggedIn": False}))).build()
    assert agent_status(out).logged_in is False
    missing = agent_status(FakeIO(claude=None).build())
    assert not missing.installed and missing.logged_in is None


def test_default_which_finds_claude_in_the_native_installer_folder(monkeypatch):
    from investigate import setup

    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup, "find_claude", lambda which: "C:/tools/claude.exe")
    assert setup.SetupIO().which("claude") == "C:/tools/claude.exe"
    assert setup.SetupIO().which("ffmpeg") is None

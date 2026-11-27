from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path

from investigate import cli
from investigate.autostart import Autostart, startup_command
from investigate.config import config_path, load_config
from investigate.firstrun import (
    SHORTCUT_NAME,
    UNINSTALL_NAME,
    InstallIO,
    Shortcuts,
    run_first_run,
    run_uninstall,
)

TRAY = ["C:\\tool\\Scripts\\investigate-tray.exe"]
UNINSTALL = ["C:\\tool\\Scripts\\investigate.exe", "uninstall"]


class FakeShortcuts(Shortcuts):
    def __init__(self) -> None:
        self.files: dict[Path, tuple[str, str]] = {}
        self.created: list[Path] = []
        self.removed: list[Path] = []

    def read(self, path: Path) -> tuple[str, str] | None:
        return self.files.get(path)

    def create(self, path: Path, target: str, arguments: str, description: str) -> None:
        self.files[path] = (target, arguments)
        self.created.append(path)

    def remove(self, path: Path) -> None:
        self.files.pop(path, None)
        self.removed.append(path)


class FakeAutostart(Autostart):
    def __init__(self) -> None:
        self.command: list[str] | None = None
        self.enables = 0

    def enabled(self) -> bool:
        return self.command is not None

    def matches(self, command: Sequence[str]) -> bool:
        return self.command == list(command)

    def enable(self, command: Sequence[str]) -> None:
        self.command = list(command)
        self.enables += 1

    def disable(self) -> None:
        self.command = None


class World:
    """All the outside effects of first-run and uninstall, recorded."""

    def __init__(self, tmp: Path, *, answers: list[str] | None = None) -> None:
        self.menu = tmp / "start-menu"
        self.shortcuts = FakeShortcuts()
        self.autostart = FakeAutostart()
        self.answers = list(answers or [])
        self.said: list[str] = []
        self.asked: list[str] = []
        self.started: list[list[str]] = []
        self.opened: list[str] = []
        self.stopped = 0
        self.uv_runs = 0
        self.tray_up = False  # a tray holds the host lock
        self.url: str | None = None  # what tray.json says
        self.url_after_start = "http://127.0.0.1:8765"  # what a started tray publishes
        self.stop_works = True
        self.uv_tool = True
        self.now = 0.0

    def start(self, command: list[str]) -> None:
        self.started.append(command)
        if self.url_after_start:
            self.tray_up, self.url = True, self.url_after_start

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def stop(self, home: Path) -> bool:
        self.stopped += 1
        if self.stop_works:
            self.tray_up, self.url = False, None
        return self.stop_works

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def schedule_uv(self) -> bool:
        self.uv_runs += 1
        return True

    def build(self) -> InstallIO:
        return InstallIO(
            ask=self.ask,
            say=self.said.append,
            platform="win32",
            shortcuts=self.shortcuts,
            start_menu=self.menu,
            autostart=self.autostart,
            tray_command=lambda home: list(TRAY),
            uninstall_command=lambda home: list(UNINSTALL),
            tray_running=lambda home: self.tray_up,
            running_url=lambda home: self.url,
            start_detached=self.start,
            open_browser=lambda url: self.opened.append(url) or True,
            stop_tray=self.stop,
            is_uv_tool_install=lambda: self.uv_tool,
            schedule_uv_uninstall=self.schedule_uv,
            sleep=self.sleep,
            clock=lambda: self.now,
        )

    @property
    def output(self) -> str:
        return "\n".join(self.said)

    @property
    def start_link(self) -> Path:
        return self.menu / f"{SHORTCUT_NAME}.lnk"

    @property
    def uninstall_link(self) -> Path:
        return self.menu / f"{UNINSTALL_NAME}.lnk"


# first-run


def test_first_run_does_everything(tmp_path, home):
    world = World(tmp_path)
    assert run_first_run(home, world.build()) == 0
    assert world.shortcuts.files[world.start_link] == (TRAY[0], "")
    assert world.shortcuts.files[world.uninstall_link] == (UNINSTALL[0], "uninstall")
    assert world.autostart.command == TRAY
    assert world.started == [TRAY]
    assert world.opened == ["http://127.0.0.1:8765/setup"]
    assert load_config(home).start_at_login is True


def test_a_second_first_run_creates_nothing_and_starts_no_second_tray(tmp_path, home):
    world = World(tmp_path)
    run_first_run(home, world.build())
    created, enables = list(world.shortcuts.created), world.autostart.enables
    config_before = config_path(home).read_bytes()
    assert run_first_run(home, world.build()) == 0
    assert world.shortcuts.created == created
    assert world.autostart.enables == enables
    assert world.started == [TRAY]  # still just the first start
    assert config_path(home).read_bytes() == config_before
    assert world.opened == ["http://127.0.0.1:8765/setup"] * 2  # opening the page is harmless


def test_first_run_leaves_a_running_tray_alone(tmp_path, home):
    world = World(tmp_path)
    world.tray_up, world.url = True, "http://127.0.0.1:9000"
    run_first_run(home, world.build())
    assert world.started == []
    assert world.opened == ["http://127.0.0.1:9000/setup"]


def test_first_run_repairs_a_shortcut_that_points_elsewhere(tmp_path, home):
    world = World(tmp_path)
    world.shortcuts.files[world.start_link] = ("C:\\old\\investigate-tray.exe", "")
    run_first_run(home, world.build())
    assert world.shortcuts.files[world.start_link] == (TRAY[0], "")


def test_first_run_keeps_an_existing_config_and_comments(tmp_path, home):
    home.mkdir()
    config_path(home).write_text("# mine\nparallel_runs = 2\nstart_at_login = false\n")
    world = World(tmp_path)
    run_first_run(home, world.build())
    text = config_path(home).read_text()
    assert "# mine" in text
    assert tomllib.loads(text) == {"parallel_runs": 2, "start_at_login": True}


def test_first_run_refuses_a_broken_config_and_changes_nothing(tmp_path, home):
    home.mkdir()
    config_path(home).write_text("parallel_runs = [")
    world = World(tmp_path)
    assert run_first_run(home, world.build()) == 2
    assert world.shortcuts.created == [] and world.started == [] and world.opened == []
    assert world.autostart.command is None
    assert config_path(home).read_text() == "parallel_runs = ["


def test_first_run_says_so_when_the_tray_never_comes_up(tmp_path, home):
    world = World(tmp_path)
    world.url_after_start = ""
    assert run_first_run(home, world.build()) == 1
    assert world.opened == []
    assert "did not come up" in world.output
    assert world.now > 0  # it waited, on the injected clock


def test_first_run_is_for_windows(tmp_path, home):
    world = World(tmp_path)
    io = world.build()
    object.__setattr__(io, "platform", "linux")
    assert run_first_run(home, io) == 2
    assert world.shortcuts.created == [] and world.started == []


def test_a_failing_shortcut_is_reported_and_the_rest_goes_on(tmp_path, home):
    world = World(tmp_path)

    def boom(*args):
        raise OSError("no powershell")

    world.shortcuts.create = boom  # type: ignore[method-assign]
    assert run_first_run(home, world.build()) == 1
    assert "no powershell" in world.output
    assert world.autostart.command == TRAY and world.started == [TRAY]


# uninstall


def _installed(world: World, home: Path) -> None:
    run_first_run(home, world.build())
    world.said.clear()


def test_uninstall_removes_everything_and_keeps_the_data_folder(tmp_path, home):
    world = World(tmp_path, answers=["y", ""])
    _installed(world, home)
    assert run_uninstall(home, world.build()) == 0
    assert world.stopped == 1
    assert world.autostart.command is None
    assert world.start_link not in world.shortcuts.files
    assert world.uninstall_link not in world.shortcuts.files
    assert world.uv_runs == 1
    assert home.is_dir() and config_path(home).is_file()
    assert "kept" in world.output


def test_uninstall_asks_before_anything_and_defaults_to_no(tmp_path, home):
    world = World(tmp_path)
    _installed(world, home)
    assert run_uninstall(home, world.build()) == 1
    assert world.stopped == 0 and world.uv_runs == 0 and world.autostart.command == TRAY


def test_uninstall_deletes_the_data_folder_only_on_an_explicit_yes(tmp_path, home):
    world = World(tmp_path, answers=["y", "y"])
    _installed(world, home)
    assert run_uninstall(home, world.build()) == 0
    assert not home.exists()
    assert "data folder" in world.asked[-1].lower()


def test_uninstall_yes_flag_still_asks_about_the_data_folder(tmp_path, home):
    world = World(tmp_path, answers=[""])
    _installed(world, home)
    run_uninstall(home, world.build(), assume_yes=True)
    assert len(world.asked) == 1 and home.is_dir()


def test_uninstall_never_deletes_a_folder_that_is_not_a_data_folder(tmp_path):
    stray = tmp_path / "documents"
    stray.mkdir()
    (stray / "thesis.txt").write_text("keep")
    world = World(tmp_path, answers=["y", "y"])
    run_uninstall(stray, world.build())
    assert (stray / "thesis.txt").is_file()
    assert "will not delete" in world.output.lower()


def test_uninstall_stops_when_the_tray_will_not_quit(tmp_path, home):
    world = World(tmp_path, answers=["y", "n"])
    _installed(world, home)
    world.stop_works = False
    assert run_uninstall(home, world.build()) == 1
    assert world.autostart.command == TRAY and world.uv_runs == 0
    assert world.start_link in world.shortcuts.files


def test_uninstall_skips_uv_when_not_a_uv_tool_install(tmp_path, home):
    world = World(tmp_path, answers=["y", ""])
    _installed(world, home)
    world.uv_tool = False
    assert run_uninstall(home, world.build()) == 0
    assert world.uv_runs == 0
    assert "uv tool uninstall" in world.output


def test_uninstall_works_when_nothing_was_installed(tmp_path, home):
    world = World(tmp_path, answers=["y", ""])
    assert run_uninstall(home, world.build()) == 0
    assert world.uv_runs == 1


# wiring


def test_the_startup_command_prefers_the_windowless_script(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / "python.exe"
    python.write_bytes(b"")
    plain = startup_command(str(python))
    assert plain[1:] == ["-m", "investigate.cli", "tray"]
    gui = scripts / "investigate-tray.exe"
    gui.write_bytes(b"")
    assert startup_command(str(python)) == [str(gui)]
    other = tmp_path / "elsewhere"
    assert startup_command(str(python), home=other) == [str(gui), "--home", str(other.resolve())]


def test_cli_commands_exist_and_tray_main_is_the_gui_entry(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv=None, **kw: calls.append(list(argv)) or 0)
    monkeypatch.setattr("sys.argv", ["investigate-tray", "--port", "9001"])
    assert cli.tray_main() == 0
    assert calls == [["tray", "--port", "9001"]]
    parser = cli._parser()
    assert parser.parse_args(["first-run"]).command == "first-run"
    assert parser.parse_args(["uninstall", "--yes"]).yes is True


def test_the_gui_script_is_declared():
    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["gui-scripts"]["investigate-tray"] == "investigate.cli:tray_main"


# review fixes


def _deletions(monkeypatch) -> list[Path]:
    gone: list[Path] = []
    monkeypatch.setattr("investigate.firstrun.shutil.rmtree", lambda path, **kw: gone.append(path))
    return gone


def _answer_yes_to_all(world: World, home: Path):
    world.answers = ["y", "y"]
    return run_uninstall(home, world.build())


def test_uninstall_never_deletes_the_user_home_or_its_ancestors(tmp_path, monkeypatch):
    gone = _deletions(monkeypatch)
    user = tmp_path / "users" / "someone"
    user.mkdir(parents=True)
    (user / "config.toml").write_text("")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    for folder in (user, user.parent, tmp_path):
        (folder / "config.toml").write_text("")
        world = World(tmp_path)
        _answer_yes_to_all(world, folder)
        assert "will not delete" in world.output.lower()
    assert gone == []


def test_uninstall_never_deletes_a_drive_root(tmp_path, monkeypatch):
    gone = _deletions(monkeypatch)
    root = Path(tmp_path.anchor)
    world = World(tmp_path)
    _answer_yes_to_all(world, root)
    assert gone == [] and "not delete" in world.output


def test_uninstall_never_deletes_a_folder_with_unrelated_entries(tmp_path, monkeypatch):
    gone = _deletions(monkeypatch)
    folder = tmp_path / "work"
    (folder / "runs").mkdir(parents=True)
    (folder / "notes.txt").write_text("mine")
    world = World(tmp_path)
    _answer_yes_to_all(world, folder)
    assert gone == [] and "not delete" in world.output
    (folder / "notes.txt").unlink()  # runs alone, without config.toml or index.json, is not enough
    world = World(tmp_path)
    _answer_yes_to_all(world, folder)
    assert gone == []


def test_uninstall_deletes_a_folder_made_only_of_what_the_app_writes(tmp_path, monkeypatch):
    gone = _deletions(monkeypatch)
    folder = tmp_path / "data2"
    for name in ("runs", "inbox", "browser-profile"):
        (folder / name).mkdir(parents=True)
    for name in ("config.toml", "index.json", "index.lock", "tray.lock", "profile.lock"):
        (folder / name).write_text("")
    world = World(tmp_path)
    _answer_yes_to_all(world, folder)
    assert gone == [folder]


def test_the_shortcut_script_reads_utf8_and_the_output_is_decoded_as_utf8(tmp_path):
    from investigate.firstrun import _CREATE_SCRIPT, _READ_SCRIPT

    assert "OutputEncoding" in _READ_SCRIPT and "UTF8" in _READ_SCRIPT
    assert "OutputEncoding" not in _CREATE_SCRIPT or "UTF8" in _CREATE_SCRIPT


def test_a_real_shortcut_round_trips_a_non_ascii_path(tmp_path):
    import shutil
    import sys

    import pytest

    from investigate.firstrun import PowerShellShortcuts

    if sys.platform != "win32" or shutil.which("powershell.exe") is None:
        pytest.skip("needs Windows PowerShell")
    link = tmp_path / "Üser" / "investigate.lnk"
    shortcuts = PowerShellShortcuts()
    target = str(tmp_path / "Müller" / "investigate-tray.exe")
    shortcuts.create(link, target, r'--home "C:\Jörg"', "d")
    assert shortcuts.read(link) == (target, r'--home "C:\Jörg"')


def test_uninstall_says_it_is_waiting_for_running_runs(tmp_path, home):
    world = World(tmp_path, answers=["y", ""])
    world.tray_up = True
    run_uninstall(home, world.build())
    assert "waiting for running runs to stop" in world.output.lower()


def test_uninstall_waits_long_enough_for_a_run_to_stop():
    from investigate.firstrun import WAIT_FOR_QUIT_SECONDS

    assert WAIT_FOR_QUIT_SECONDS >= 90


def test_the_uninstall_shortcut_keeps_its_console_open_and_pause_waits_for_enter(tmp_path, home):
    command = InstallIO().uninstall_command(home)
    assert "uninstall" in command and "--pause" in command
    world = World(tmp_path, answers=["n", ""])
    run_uninstall(home, world.build(), pause=True)
    assert world.asked[-1].startswith("Press Enter to close")
    world = World(tmp_path, answers=["n"])
    run_uninstall(home, world.build())
    assert not any(a.startswith("Press Enter") for a in world.asked)


def test_the_default_uninstall_shortcut_parses_for_a_non_default_home(tmp_path):
    from investigate.cli import _parser
    from investigate.firstrun import InstallIO

    other = tmp_path / "elsewhere"
    command = InstallIO().uninstall_command(other)
    args = (
        command[command.index("investigate.cli") + 1 :]
        if "investigate.cli" in command
        else command[1:]
    )
    parsed = _parser().parse_args(args)
    assert parsed.command == "uninstall" and parsed.pause
    assert Path(parsed.home).resolve() == other.resolve()


def test_uninstall_deletes_a_folder_the_setup_page_and_login_wrote_to(tmp_path, monkeypatch):
    from investigate.browser import mark_logged_in
    from investigate.setupweb import add_contexts

    gone = _deletions(monkeypatch)
    folder = tmp_path / "data3"
    folder.mkdir()
    (folder / "index.json").write_text("[]")
    add_contexts(folder, [("work.md", b"my work")], False)
    mark_logged_in(folder)
    world = World(tmp_path)
    _answer_yes_to_all(world, folder)
    assert gone == [folder]


def test_the_default_uninstall_shortcut_parses_for_the_default_home():
    from investigate.config import default_home
    from investigate.firstrun import InstallIO

    command = InstallIO().uninstall_command(default_home())
    args = (
        command[command.index("investigate.cli") + 1 :]
        if "investigate.cli" in command
        else command[1:]
    )
    assert "--home" not in args
    parsed = cli._parser().parse_args(args)
    assert parsed.command == "uninstall" and parsed.pause and parsed.home is None


# stop-tray: what install.cmd runs before it replaces the program


def test_stop_tray_is_a_quiet_no_op_when_no_tray_runs(tmp_path, home):
    from investigate.firstrun import run_stop_tray

    world = World(tmp_path)
    assert run_stop_tray(home, world.build()) == 0
    assert world.stopped == 0 and world.said == []


def test_stop_tray_quits_a_running_tray(tmp_path, home):
    from investigate.firstrun import run_stop_tray

    world = World(tmp_path)
    world.tray_up, world.url = True, "http://127.0.0.1:8765"
    assert run_stop_tray(home, world.build()) == 0
    assert world.stopped == 1 and not world.tray_up


def test_stop_tray_says_plainly_when_the_tray_will_not_quit(tmp_path, home):
    from investigate.firstrun import run_stop_tray

    world = World(tmp_path)
    world.tray_up, world.url, world.stop_works = True, "http://127.0.0.1:8765", False
    # A code of its own: exit 1 is also what a broken install's crash gives, and install.cmd
    # must not tell the user to quit a tray that is not there.
    from investigate.firstrun import TRAY_STAYS

    assert TRAY_STAYS not in (0, 1, 2)
    assert run_stop_tray(home, world.build()) == TRAY_STAYS
    assert "quit it from its menu" in world.output.lower()


def test_stop_tray_subcommand_exists_and_takes_home():
    parsed = cli._parser().parse_args(["stop-tray", "--home", "x"])
    assert parsed.command == "stop-tray" and parsed.home == Path("x")

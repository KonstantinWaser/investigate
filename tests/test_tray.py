"""The tray host and the single-instance lock, with no GUI and the fake runner."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from investigate import history
from investigate.instance import InstanceLock, running_url
from investigate.server import FinishedRun
from investigate.tray import Toast, TrayApp, popup_command, run_tray, toast_for, toast_text
from tests.fakes import FakeRunner
from tests.test_queue_server import CountingRunner, _statuses, _wait_for
from tests.test_server import Client

HOLDER = str(Path(__file__).with_name("lock_holder.py"))


class RecordingNotifier:
    def __init__(self) -> None:
        self.toasts: list[Toast] = []

    def show(self, toast: Toast) -> None:
        self.toasts.append(toast)


def _app(home, runner=None, **kwargs) -> TrayApp:
    kwargs.setdefault("notifier", RecordingNotifier())
    kwargs.setdefault("open_url", lambda url: None)
    kwargs.setdefault("open_capture", lambda url: None)
    kwargs.setdefault("quit_grace", 0.2)
    return TrayApp(home, runner or FakeRunner(), port=0, **kwargs)


# menu


def test_menu_has_the_agreed_items_and_new_is_the_left_click(home):
    items = _app(home).menu_items()
    assert [i.label for i in items] == [
        "New",
        "History",
        "Check inbox now",
        "Log in to sites",
        "Settings",
        "Setup",
        "Quit",
    ]
    by = {i.label: i for i in items}
    assert by["New"].default and not by["History"].default
    assert by["Check inbox now"].enabled and by["Check inbox now"].action
    assert by["Settings"].enabled and by["Setup"].enabled and by["Setup"].action
    assert by["Log in to sites"].enabled and by["Log in to sites"].action


def test_new_and_history_open_the_capture_page_and_the_history(home):
    opened = []
    app = _app(
        home,
        open_url=lambda u: opened.append(("url", u)),
        open_capture=lambda u: opened.append(("capture", u)),
    )
    app.start()
    try:
        app.on_new()
        app.on_history()
        base = app.server.url
        assert opened == [("capture", base + "capture"), ("url", base)]
    finally:
        app.shutdown()


# login


def test_login_runs_off_the_calling_thread_and_a_second_click_is_ignored(home):
    release, started, calls = threading.Event(), threading.Event(), []

    def login(path):
        calls.append(threading.current_thread())
        started.set()
        assert release.wait(10)
        return 0

    notifier = RecordingNotifier()
    app = _app(home, login=login, notifier=notifier)
    app.start()
    try:
        began = time.monotonic()
        app.on_login()
        assert time.monotonic() - began < 2  # did not wait for the login browser
        assert started.wait(5)
        app.on_login()  # still open: ignored
        release.set()
        app._login_thread.join(5)
        assert len(calls) == 1 and calls[0] is not threading.main_thread()
        assert [t.title for t in notifier.toasts] == ["Logged in"]
    finally:
        release.set()
        app.shutdown()


def test_a_failed_login_says_so(home):
    notifier = RecordingNotifier()
    app = _app(home, login=lambda path: 1, notifier=notifier)
    app.start()
    try:
        app.on_login()
        app._login_thread.join(5)
        assert [t.title for t in notifier.toasts] == ["Login did not finish"]
    finally:
        app.shutdown()


# toasts


def test_toast_per_outcome_links_to_the_report(home):
    notifier = RecordingNotifier()
    app = _app(home, notifier=notifier)
    app.start()
    try:
        for status in ("completed", "partial", "failed"):
            app._on_finished(FinishedRun("run-1", status, "A title"))
        assert [t.title for t in notifier.toasts] == [
            "Report ready",
            "Report ready (partial)",
            "Run failed",
        ]
        assert notifier.toasts[0].url == f"{app.server.url}runs/run-1/"
        assert notifier.toasts[0].message == "A title"
    finally:
        app.shutdown()


def test_a_finished_run_in_the_server_raises_a_toast(home):
    notifier = RecordingNotifier()
    app = _app(home, notifier=notifier)
    app.start()
    try:
        run_id = app.server.submit("topic", [], "quick", [])
        assert app.server.wait_for_runs()
        (toast,) = notifier.toasts
        assert toast.url.endswith(f"/runs/{run_id}/") and toast.message == "Found a thing"
    finally:
        app.shutdown()


@pytest.mark.parametrize(
    "hostile",
    [
        "$(Remove-Item -Recurse C:\\) `calc`",
        "]]></text><script>",
        '"@\nWrite-Host pwned',
        "a" * 500,
    ],
)
def test_toast_text_is_plain_single_line_and_short(hostile):
    text = toast_text(hostile)
    assert not set(text) & set('$`<>"\\[]\n@{}')
    assert len(text) <= 100


def test_toast_for_survives_an_empty_title():
    assert toast_for(FinishedRun("x", "completed", "$$$"), "http://h/").message == "Untitled"


# status


def test_tooltip_and_icon_state_follow_the_queue(home):
    runner = CountingRunner()
    app = _app(home, runner)
    app.server.queue.limit = 1
    app.start()
    try:
        assert app.tooltip() == "Investigate" and app.working() == (0, 0)
        app.server.submit("a", [], "quick", [])
        app.server.submit("b", [], "quick", [])
        assert _wait_for(lambda: runner.started == 1)
        assert app.working() == (1, 1)
        assert app.tooltip() == "Investigate: 1 running, 1 queued"
    finally:
        app.on_quit()
        app.shutdown()


# quit


def test_quit_asks_the_gui_to_stop_and_shutdown_leaves_nothing_behind(home):
    runner = CountingRunner()
    stopped, login_stopped = [], []
    app = _app(home, runner, stop_login=lambda: login_stopped.append(True))
    app.bind_gui(lambda: stopped.append(True))
    app.start()
    app.server.queue.limit = 1
    first = app.server.submit("first", [], "quick", [])
    second = app.server.submit("second", [], "quick", [])
    assert _wait_for(lambda: runner.started == 1)

    app.on_quit()
    assert stopped == [True]
    app.shutdown()

    assert login_stopped == [True]
    assert app.working() == (0, 0)
    statuses = _statuses(home)
    assert statuses[first] == "completed" and statuses[second] == "failed"
    assert not any(e.status in ("queued", "running") for e in history.load_entries(home))
    with pytest.raises(OSError):  # the server no longer answers
        Client(app.server).get("/")


def test_no_toasts_while_quitting(home):
    notifier = RecordingNotifier()
    app = _app(home, notifier=notifier)
    app.on_quit()
    app._on_finished(FinishedRun("r", "completed", "t"))
    assert notifier.toasts == []


# single instance


def test_run_tray_serves_publishes_and_cleans_up(home):
    seen = {}

    def gui(app):
        seen["url"] = running_url(home)
        seen["status"] = Client(app.server).get("/")[0]
        seen["capture"] = Client(app.server).get("/capture")[0]
        seen["server_url"] = app.server.url

    assert run_tray(home, FakeRunner(), gui, port=0, notifier=None, quit_grace=0.2) == 0
    assert seen["url"] == seen["server_url"]
    assert seen["status"] == 200 and seen["capture"] == 200
    assert running_url(home) is None
    lock = InstanceLock(home)
    assert lock.acquire()  # released on exit
    lock.release()


def test_a_second_instance_exits_cleanly_without_a_server(home, capsys):
    first = InstanceLock(home)
    assert first.acquire()
    first.publish("http://127.0.0.1:8765/")
    built = []
    try:
        code = run_tray(
            home,
            FakeRunner(),
            lambda app: built.append("gui"),
            app_factory=lambda *a, **k: built.append("app"),
        )
    finally:
        first.release()
    assert code == 0 and built == []
    assert "already running at http://127.0.0.1:8765/" in capsys.readouterr().out


def test_a_second_process_cannot_take_the_lock(home):
    holder = subprocess.Popen(
        [sys.executable, HOLDER, str(home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert running_url(home) == "http://127.0.0.1:9/"
        assert not InstanceLock(home).acquire()
        assert run_tray(home, FakeRunner(), lambda app: None, port=0) == 0
    finally:
        holder.stdin.close()
        holder.wait(10)
    again = InstanceLock(home)
    assert again.acquire()  # released when the holder exited
    again.release()


def test_two_starts_at_once_run_exactly_one_server(home):
    entered, release, codes = [], threading.Event(), []
    gate = threading.Barrier(2)

    def gui(app):
        entered.append(app.server.url)
        assert release.wait(10)

    def start():
        gate.wait(5)
        codes.append(run_tray(home, FakeRunner(), gui, port=0, notifier=None, quit_grace=0.2))

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    assert _wait_for(lambda: len(codes) == 1)  # the loser returns while the winner serves
    assert len(entered) == 1
    release.set()
    for t in threads:
        t.join(10)
    assert codes == [0, 0] and len(entered) == 1


def test_a_taken_port_is_an_error_and_frees_the_lock(home, capsys):
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert run_tray(home, FakeRunner(), lambda app: None, port=port) == 2
    assert "error" in capsys.readouterr().err
    lock = InstanceLock(home)
    assert lock.acquire()
    lock.release()


def test_a_bad_config_is_an_error_not_a_traceback(home, capsys):
    home.mkdir(parents=True)
    (home / "config.toml").write_text("parallel_runs = 9\n", encoding="utf-8")
    assert run_tray(home, FakeRunner(), lambda app: None, port=0) == 2
    assert "parallel_runs" in capsys.readouterr().err


# capture window


@pytest.mark.skipif(sys.platform != "win32", reason="Edge path lookup is Windows only")
def test_popup_uses_edge_in_app_mode_when_present(tmp_path, monkeypatch):
    exe = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    command = popup_command("http://127.0.0.1:8765/capture", {"PROGRAMFILES(X86)": str(tmp_path)})
    assert command[0] == str(exe) and command[1] == "--app=http://127.0.0.1:8765/capture"
    assert not any("user-data-dir" in part for part in command)  # never the research profile

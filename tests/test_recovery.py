"""Quit paths of `serve`, recovery after a hard kill, and the lock `serve` shares with the tray."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from investigate import history
from investigate.cli import main
from investigate.config import Depth
from investigate.instance import InstanceLock, recover_interrupted
from investigate.server import HistoryServer
from investigate.service import reserve_run
from tests.fakes import FakeRunner
from tests.test_queue_server import CountingRunner, _wait_for


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _entry(home, status, pid=None):
    folder = reserve_run("topic", Depth.QUICK, home=home)
    entry = history.load_entries(home)[-1]
    history.upsert(home, history.HistoryEntry(**{**entry.__dict__, "status": status}))
    if pid is not None:
        (folder / "running.pid").write_text(str(pid), encoding="utf-8")
    return folder


def test_serve_exit_path_runs_the_same_shutdown_as_the_tray(home):
    runner = CountingRunner()
    srv = HistoryServer(home, runner, port=0, max_parallel=1)
    srv.start()
    first = srv.submit("first", [], "quick", [])
    second = srv.submit("second", [], "quick", [])
    assert _wait_for(lambda: runner.started == 1)

    srv.shutdown_grace = 0.1
    t = threading.Thread(target=srv.serve_forever)  # start() is a no-op: already started
    t.start()
    srv.interrupt()  # what Ctrl+C does inside serve_forever
    t.join(30)
    assert not t.is_alive()

    statuses = {e.id: e.status for e in history.load_entries(home)}
    assert statuses[second] == "failed"
    assert statuses[first] != "running"
    assert "queued" not in statuses.values()


def test_recovery_fails_queued_and_dead_running_entries_but_not_live_ones(home):
    queued = _entry(home, "queued")
    dead = _entry(home, "running", pid=_dead_pid())
    no_pid = _entry(home, "running")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        alive = _entry(home, "running", pid=other.pid)
        done = _entry(home, "completed")
        fixed = recover_interrupted(home)
    finally:
        other.kill()
        other.wait()

    statuses = {e.id: e.status for e in history.load_entries(home)}
    assert set(fixed) == {queued.name, dead.name, no_pid.name}
    assert statuses[queued.name] == statuses[dead.name] == statuses[no_pid.name] == "failed"
    assert statuses[alive.name] == "running" and statuses[done.name] == "completed"
    assert "Interrupted" in (queued / "agent.log").read_text(encoding="utf-8")


def test_the_tray_recovers_on_start(home):
    from investigate.tray import run_tray

    stale = _entry(home, "queued")
    seen = {}

    def gui(app):
        seen["status"] = {e.id: e.status for e in history.load_entries(home)}[stale.name]

    assert run_tray(home, FakeRunner(), gui, port=0, notifier=None, quit_grace=0.2) == 0
    assert seen["status"] == "failed"


def test_serve_refuses_to_start_while_a_host_holds_the_lock(home, capsys):
    lock = InstanceLock(home)
    assert lock.acquire()
    lock.publish("http://127.0.0.1:8765/")
    stale = _entry(home, "queued")
    try:
        assert main(["serve", "--port", "0"], runner=FakeRunner()) == 1
    finally:
        lock.release()
    assert "already running at http://127.0.0.1:8765/" in capsys.readouterr().err
    assert {e.id: e.status for e in history.load_entries(home)}[stale.name] == "queued"


def test_recovery_treats_a_running_run_with_this_hosts_own_pid_as_dead(home):
    """A hard-killed host's pid can be handed to the new host. Under the lock, before any Run
    started here, a pid equal to ours cannot be a live Run."""
    reused = _entry(home, "running", pid=os.getpid())

    assert recover_interrupted(home) == [reused.name]
    assert history.load_entries(home)[-1].status == "failed"


def test_a_finished_run_leaves_no_pid_file_behind(home):
    from investigate.service import execute_run

    result = execute_run("topic", Depth.QUICK, home=home, runner=FakeRunner())
    assert not (result.folder / "running.pid").exists()


def test_a_crashing_run_leaves_no_pid_file_behind(home):
    from investigate.service import execute_run

    class Boom(FakeRunner):
        def run(self, request):
            raise KeyboardInterrupt

    import pytest

    with pytest.raises(KeyboardInterrupt):
        execute_run("topic", Depth.QUICK, home=home, runner=Boom())
    folder = next((home / "runs").iterdir())
    assert not (folder / "running.pid").exists()


def test_recovery_runs_before_the_server_listens(home, monkeypatch):
    """A submit that arrives in the gap would be marked Interrupted by a late recovery."""
    import investigate.tray as tray_module
    from investigate.tray import TrayApp, run_tray

    order = []
    real = tray_module.recover_interrupted
    monkeypatch.setattr(
        tray_module, "recover_interrupted", lambda h: (order.append("recover"), real(h))[1]
    )

    class Spy(TrayApp):
        def start(self):
            order.append("start")
            super().start()

    assert run_tray(home, FakeRunner(), lambda app: None, port=0, app_factory=Spy) == 0
    assert order == ["recover", "start"]


def test_serve_recovers_before_its_server_listens(home, monkeypatch):
    import investigate.cli as cli_module

    order = []
    real = cli_module.recover_interrupted
    monkeypatch.setattr(
        cli_module, "recover_interrupted", lambda h: (order.append("recover"), real(h))[1]
    )

    class Spy(HistoryServer):
        def start(self):
            order.append("start")
            super().start()

        def serve_forever(self):
            self.start()
            self.shutdown(0.1)

    monkeypatch.setattr(cli_module, "HistoryServer", Spy)
    assert main(["serve", "--port", "0"], runner=FakeRunner()) == 0
    assert order[:2] == ["recover", "start"]

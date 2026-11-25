"""The Run queue behind the submit endpoint, quit handling and the capture page."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from investigate import history
from investigate.config import ConfigError, PermissionMode, load_config
from investigate.runner import ClaudeCodeRunner, RunRequest
from investigate.server import FinishedRun, HistoryServer, SubmitError
from tests.fakes import FakeRunner
from tests.test_server import Client


class CountingRunner(FakeRunner):
    """Blocks every Run until released and records how many ran at the same time."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.now = self.peak = self.started = 0
        self.release = threading.Event()

    def run(self, request: RunRequest):
        with self._lock:
            self.now += 1
            self.started += 1
            self.peak = max(self.peak, self.now)
        try:
            assert self.release.wait(10)
            return super().run(request)
        finally:
            with self._lock:
                self.now -= 1

    def stop(self) -> None:
        self.release.set()


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _statuses(home) -> dict[str, str]:
    return {e.id: e.status for e in history.load_entries(home)}


def test_parallel_limit_is_honoured_and_waiting_runs_show_as_queued(home):
    runner = CountingRunner()
    srv = HistoryServer(home, runner, port=0, max_parallel=2)
    srv.start()
    try:
        ids = [srv.submit(f"topic {i}", [], "quick", []) for i in range(4)]
        assert _wait_for(lambda: runner.started == 2)
        statuses = _statuses(home)
        assert sorted(statuses.values()) == ["queued", "queued", "running", "running"]
        assert [statuses[i] for i in ids[2:]] == ["queued", "queued"]
        page = Client(srv).get("/")[2]
        assert b"queued" in page and b"running" in page
        assert Client(srv).post(f"/runs/{ids[3]}/delete")[0] == 409  # queued: not deletable

        runner.release.set()
        assert srv.wait_for_runs()
        assert runner.peak == 2 and runner.started == 4
        assert set(_statuses(home).values()) == {"completed"}
    finally:
        runner.release.set()
        srv.stop()


def test_limit_comes_from_config_and_is_validated(home):
    (home).mkdir(parents=True, exist_ok=True)
    assert load_config(home).parallel_runs == 2
    config = home / "config.toml"
    config.write_text("parallel_runs = 3\n", encoding="utf-8")
    assert HistoryServer(home, FakeRunner(), port=0).queue.limit == 3
    for bad in ("0", "4", "1.5", "true", '"two"'):
        config.write_text(f"parallel_runs = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(home)


def test_on_finished_hears_success_and_failure(home):
    heard: list[FinishedRun] = []
    srv = HistoryServer(home, FakeRunner(), port=0, on_finished=heard.append)
    ok_id = srv.submit("good", [], "quick", [])
    assert srv.wait_for_runs()
    bad = HistoryServer(home, FakeRunner(ok=False, error="nope"), port=0, on_finished=heard.append)
    bad_id = bad.submit("bad", [], "quick", [])
    assert bad.wait_for_runs()
    assert [(h.id, h.status) for h in heard] == [(ok_id, "completed"), (bad_id, "failed")]
    assert heard[0].title == "Found a thing"


def test_a_broken_listener_does_not_break_the_queue(home):
    def explode(finished):
        raise RuntimeError("toast failed")

    srv = HistoryServer(home, FakeRunner(), port=0, on_finished=explode)
    srv.submit("one", [], "quick", [])
    srv.submit("two", [], "quick", [])
    assert srv.wait_for_runs()
    assert set(_statuses(home).values()) == {"completed"}


def test_shutdown_cancels_queued_stops_running_and_refuses_new_runs(home):
    runner = CountingRunner()
    srv = HistoryServer(home, runner, port=0, max_parallel=1)
    srv.start()
    first = srv.submit("first", [], "quick", [])
    second = srv.submit("second", [], "quick", [])
    assert _wait_for(lambda: runner.started == 1)

    srv.shutdown(grace=0.1)  # the fake runner's stop() ends the running Run

    statuses = _statuses(home)
    assert statuses[first] == "completed"  # the fake finished when stopped
    assert statuses[second] == "failed"
    assert "Cancelled" in (home / "runs" / second / "agent.log").read_text(encoding="utf-8")
    assert runner.started == 1
    assert srv.active_ids() == set()
    with pytest.raises(SubmitError) as err:
        srv.submit("late", [], "quick", [])
    assert err.value.status == 503
    assert "queued" not in _statuses(home).values()


def test_shutdown_lets_a_run_finish_inside_the_grace_period(home):
    runner = CountingRunner()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    run_id = srv.submit("work", [], "quick", [])
    assert _wait_for(lambda: runner.started == 1)
    threading.Timer(0.2, runner.release.set).start()
    srv.shutdown(grace=5)
    assert _statuses(home)[run_id] == "completed"


def test_capture_page_posts_to_the_submit_endpoint(home):
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    try:
        status, headers, body = Client(srv).get("/capture")
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        page = body.decode()
        assert "/api/runs" in page and 'id="depth"' in page
        assert '<option value="standard" selected>' in page
        assert "clipboardData" in page and "dataTransfer" in page
    finally:
        srv.stop()


STUB = str(Path(__file__).with_name("stub_claude.py"))


def test_runner_stop_ends_the_agent_and_returns_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "hang")
    runner = ClaudeCodeRunner(command=[sys.executable, STUB])
    request = RunRequest(
        prompt="hello",
        workdir=tmp_path,
        session_id="11111111-1111-1111-1111-111111111111",
        timeout_seconds=60,
        model=None,
        permission_mode=PermissionMode.BYPASS,
    )
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(runner.run(request)))
    started = time.monotonic()
    thread.start()
    assert _wait_for(lambda: (tmp_path / "result.md").exists() and runner._procs)
    runner.stop()
    thread.join(20)
    assert not thread.is_alive() and time.monotonic() - started < 30
    (outcome,) = outcomes
    assert outcome.ok and outcome.timed_out
    assert "app was quit" in outcome.log
    assert not runner._procs
    # a Run that starts after stop() is killed at once
    again = runner.run(request)
    assert again.timed_out

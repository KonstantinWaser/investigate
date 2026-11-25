"""Follow-up fixes found in review: fallback Run tracking, markers, locking, cwd, input."""

from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime

from investigate import history
from investigate.config import Depth
from investigate.followup import FollowupError, FollowupManager, read_followups, run_followup
from investigate.server import HistoryServer
from investigate.service import execute_run
from tests.fakes import FakeRunner
from tests.test_followup import FORM, _ask, _run, served  # noqa: F401  (fixture)
from tests.test_server import Client

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class GatedRunner(FakeRunner):
    """Runs normally, except `run` blocks once `gated` is set (the fallback Run)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gated = False
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, request):
        if self.gated:
            self.started.set()
            assert self.release.wait(10)
        return super().run(request)


def test_a_running_fallback_run_cannot_be_deleted_and_the_link_is_kept(home):
    runner = GatedRunner(resume_unavailable=True)
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    try:
        client = Client(srv)
        result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
        runner.gated = True
        assert client.post(f"/runs/{result.id}/followup", b"question=q", FORM)[0] == 303
        assert runner.started.wait(10)
        (new_id,) = [e.id for e in history.load_entries(home) if e.id != result.id]
        assert new_id in srv.active_ids()
        status = client.request("DELETE", f"/runs/{new_id}", headers=client.own_origin())[0]
        assert status == 409
        assert (home / "runs" / new_id).is_dir()
        runner.release.set()
        assert srv.followups.wait()
        (entry,) = read_followups(result.folder)
        assert entry["new_run_id"] == new_id and entry["mode"] == "new_run"
        assert new_id not in srv.active_ids()
    finally:
        runner.release.set()
        srv.stop()


def test_a_crashing_fallback_run_still_stores_the_followup_with_error_and_link(home, monkeypatch):
    result, runner = _run(home)
    runner.resume_unavailable = True

    def crash(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr("investigate.service.execute_run", crash)
    entry = _ask(home, result, "q", runner)
    assert entry["status"] == "failed" and "disk went away" in entry["error"]
    new_id = entry["new_run_id"]
    assert new_id and new_id != result.id  # it was reserved before the crash
    by_id = {e.id: e for e in history.load_entries(home)}
    assert by_id[new_id].status == "failed" and by_id[new_id].followup_of == result.id
    html = result.report_path.read_text(encoding="utf-8")
    assert "disk went away" in html and f"/runs/{new_id}/" in html
    assert read_followups(result.folder)[0]["question"] == "q"


def test_a_crash_before_the_new_run_exists_is_still_stored(home, monkeypatch):
    result, runner = _run(home)
    runner.resume_unavailable = True

    def crash(*args, **kwargs):
        raise OSError("no folder")

    monkeypatch.setattr("investigate.service.reserve_run", crash)
    entry = _ask(home, result, "q", runner)
    assert entry["status"] == "failed" and entry["new_run_id"] is None
    assert "no folder" in result.report_path.read_text(encoding="utf-8")


def test_old_report_gets_the_progress_refresh_too(home):
    result, runner = _run(home)
    html = result.report_path.read_text(encoding="utf-8")
    for mark in (
        "<!--investigate:followups-->",
        "<!--/investigate:followups-->",
        "<!--investigate:followup-box-->",
        "<!--investigate:head-->",
    ):
        html = html.replace(mark, "")
    manager = FollowupManager(home, runner)
    manager._active[result.id] = "pending question"
    served_html = manager.decorate(result.id, html, busy_run=False)
    assert '<meta http-equiv="refresh"' in served_html and "Follow-up running" in served_html
    assert served_html.index("http-equiv") < served_html.index("</head>")
    bare = manager.decorate(result.id, "<main>x</main>", busy_run=False)
    assert "http-equiv" in bare


def test_delete_and_followup_start_are_mutually_exclusive(served, monkeypatch):  # noqa: F811
    srv, client, result, _ = served
    in_delete, finish = threading.Event(), threading.Event()
    real = shutil.rmtree

    def slow_rmtree(path, **kwargs):
        in_delete.set()
        assert finish.wait(10)
        real(path, **kwargs)

    monkeypatch.setattr("investigate.server.shutil.rmtree", slow_rmtree)
    outcome = {}

    def delete():
        outcome["delete"] = srv.delete(result.id)

    def ask():
        try:
            srv.ask_followup(result.id, "late")
            outcome["ask"] = "started"
        except FollowupError as exc:
            outcome["ask"] = exc.status

    deleter = threading.Thread(target=delete)
    deleter.start()
    assert in_delete.wait(10)
    asker = threading.Thread(target=ask)
    asker.start()
    asker.join(0.5)
    assert asker.is_alive()  # waiting for the delete, not racing it
    finish.set()
    deleter.join(10)
    asker.join(10)
    assert outcome == {"delete": "deleted", "ask": 404}


def test_resume_uses_the_cwd_the_original_run_used(tmp_path):
    (tmp_path / "a").mkdir()
    odd_home = tmp_path / "a" / ".." / "odd-home"  # an unresolved spelling of the data folder
    runner = FakeRunner()
    result = execute_run("topic", Depth.QUICK, home=odd_home, runner=runner, now=NOW)
    original_cwd = runner.requests[0].workdir
    assert original_cwd != original_cwd.resolve()
    srv = HistoryServer(odd_home, runner, port=0)
    srv.start()
    try:
        client = Client(srv)
        assert client.post(f"/runs/{result.id}/followup", b"question=q", FORM)[0] == 303
        assert srv.followups.wait()
    finally:
        srv.stop()
    assert runner.resumes[0].workdir == original_cwd
    recorded = json.loads((result.folder / "run.json").read_text(encoding="utf-8"))["workdir"]
    assert recorded == str(original_cwd)


def test_resume_of_a_run_without_a_recorded_cwd_uses_the_unresolved_home(tmp_path):
    (tmp_path / "a").mkdir()
    odd_home = tmp_path / "a" / ".." / "odd-home"
    runner = FakeRunner()
    result = execute_run("topic", Depth.QUICK, home=odd_home, runner=runner, now=NOW)
    run_json = result.folder / "run.json"
    data = json.loads(run_json.read_text(encoding="utf-8"))
    del data["workdir"]
    run_json.write_text(json.dumps(data), encoding="utf-8")
    run_followup(odd_home, result.id, result.folder.resolve(), "q", runner=runner)
    assert runner.resumes[0].workdir == odd_home / "runs" / result.id


def test_fallback_run_gets_the_attachments_and_earlier_followups(home, tmp_path):
    attached = tmp_path / "notes.txt"
    attached.write_text("attached text", encoding="utf-8")
    runner = FakeRunner()
    result = execute_run(
        "topic", Depth.QUICK, home=home, runner=runner, now=NOW, attachments=[attached]
    )
    _ask(home, result, "first question", runner)
    runner.resume_unavailable = True
    entry = _ask(home, result, "second question", runner)
    new_folder = home / "runs" / entry["new_run_id"]
    assert (new_folder / "attachments" / "notes.txt").read_text(encoding="utf-8") == "attached text"
    item = (new_folder / "item.md").read_text(encoding="utf-8")
    assert "first question" in item and "A follow-up claim 1." in item
    assert "The thing exists." in item and "second question" in item


def test_the_cap_covers_report_and_earlier_followups_together(home):
    runner = FakeRunner(result_file="x" * 50_000)
    result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
    runner.resume_unavailable = True
    entry = _ask(home, result, "q", runner)
    item = (home / "runs" / entry["new_run_id"] / "item.md").read_text(encoding="utf-8")
    assert item.count("x") <= 30_000


# the queue, recovery and the page


def test_a_running_fallback_run_is_listed_linked_and_counts_in_the_queue(home):
    runner = GatedRunner(resume_unavailable=True)
    srv = HistoryServer(home, runner, port=0, max_parallel=2)
    srv.start()
    try:
        client = Client(srv)
        result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
        runner.gated = True
        assert client.post(f"/runs/{result.id}/followup", b"question=q", FORM)[0] == 303
        assert runner.started.wait(10)
        (new,) = [e for e in history.load_entries(home) if e.id != result.id]
        assert new.status == "running" and new.followup_of == result.id
        assert srv.queue.running_ids() == {f"followup:{result.id}"}  # one slot, not two
        runner.release.set()
        assert srv.wait_for_runs()
    finally:
        runner.release.set()
        srv.stop()


class _Blocking(FakeRunner):
    def __init__(self):
        super().__init__()
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False

    def run(self, request):
        if self.block:
            self.started.set()
            assert self.release.wait(10)
        return super().run(request)


def test_a_followup_takes_a_queue_slot_and_shows_queued(home):
    runner = _Blocking()
    srv = HistoryServer(home, runner, port=0, max_parallel=1)
    srv.start()
    try:
        client = Client(srv)
        done = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
        runner.block = True
        body = json.dumps({"text": "another"}).encode()
        assert client.post("/api/runs", body, {"Content-Type": "application/json"})[0] == 202
        assert runner.started.wait(10)  # the only slot is taken
        assert client.post(f"/runs/{done.id}/followup", b"question=wait", FORM)[0] == 303
        assert runner.resumes == []  # queued behind the Run
        page = client.get(f"/runs/{done.id}/")[2].decode()
        assert "Follow-up queued" in page and 'http-equiv="refresh"' in page
        runner.release.set()
        assert srv.wait_for_runs()
        assert len(runner.resumes) == 1
        assert "Follow-up 1" in done.report_path.read_text(encoding="utf-8")
    finally:
        runner.release.set()
        srv.stop()


def test_a_hard_kill_leaves_no_stuck_follow_up_state(home):
    """The in-progress state is in memory only. A fresh host serves the ask box again, and a
    fallback Run the kill left behind is failed by recovery with its link intact."""
    from investigate.instance import recover_interrupted
    from investigate.service import reserve_run

    runner = FakeRunner()
    result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
    # what a killed host leaves: a reserved fallback Run, linked, still queued, and a stale answer
    folder = reserve_run("Follow-up: q", Depth.QUICK, home=home)
    entry = next(e for e in history.load_entries(home) if e.id == folder.name)
    history.upsert(home, entry.__class__(**{**entry.__dict__, "followup_of": result.id}))
    (result.folder / "followup-1.md").write_text("half an answer", encoding="utf-8")

    assert recover_interrupted(home) == [folder.name]
    by_id = {e.id: e for e in history.load_entries(home)}
    assert by_id[folder.name].status == "failed" and by_id[folder.name].followup_of == result.id
    assert by_id[result.id].status == "completed"

    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    try:
        page = Client(srv).get(f"/runs/{result.id}/")[2].decode()
        assert "http-equiv" not in page and "Ask a follow-up" in page
        assert "half an answer" not in page
        assert Client(srv).post(f"/runs/{result.id}/followup", b"question=again", FORM)[0] == 303
        assert srv.wait_for_runs()
        assert "Follow-up 1" in result.report_path.read_text(encoding="utf-8")  # stale file reset
    finally:
        srv.stop()


def test_served_report_csp_keeps_frame_ancestors_and_the_form_rules(home):
    runner = FakeRunner()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    try:
        result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
        csp = Client(srv).get(f"/runs/{result.id}/")[1]["Content-Security-Policy"]
        for part in (
            "default-src 'none'",
            "img-src 'self'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "base-uri 'none'",
        ):
            assert part in csp
        assert "script-src" not in csp
    finally:
        srv.stop()


def test_a_queued_followup_dropped_on_quit_is_stored_as_cancelled(home):
    runner = _Blocking()
    srv = HistoryServer(home, runner, port=0, max_parallel=1)
    srv.start()
    try:
        client = Client(srv)
        done = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
        runner.block = True
        body = json.dumps({"text": "another"}).encode()
        assert client.post("/api/runs", body, {"Content-Type": "application/json"})[0] == 202
        assert runner.started.wait(10)
        assert client.post(f"/runs/{done.id}/followup", b"question=dropped", FORM)[0] == 303
        threading.Timer(0.3, runner.release.set).start()
        srv.shutdown(0.1)
    finally:
        runner.release.set()
        srv.stop()
    (entry,) = read_followups(done.folder)
    assert entry["question"] == "dropped" and entry["status"] == "failed"
    assert "Cancelled" in entry["error"]
    assert "Cancelled" in done.report_path.read_text(encoding="utf-8")
    assert srv.followups.active_ids() == set()


def test_the_ask_box_is_hidden_on_a_running_fallback_run(home):
    runner = GatedRunner(resume_unavailable=True)
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    try:
        client = Client(srv)
        result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
        runner.gated = True
        assert client.post(f"/runs/{result.id}/followup", b"question=q", FORM)[0] == 303
        assert runner.started.wait(10)
        (new_id,) = [e.id for e in history.load_entries(home) if e.id != result.id]
        report = home / "runs" / new_id / "report.html"
        report.write_text("<html><head></head><body><main>x</main></body></html>", "utf-8")
        page = client.get(f"/runs/{new_id}/")[2].decode()
        assert "Ask a follow-up" not in page
        runner.release.set()
        assert srv.followups.wait()
    finally:
        runner.release.set()
        srv.stop()

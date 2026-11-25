"""Follow-ups: resume, append, ordering, fallback, the served page and the POST route."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from datetime import UTC, datetime

import pytest

from investigate import history
from investigate.config import Depth
from investigate.followup import FollowupManager, read_followups, run_followup
from investigate.runner import ClaudeCodeRunner
from investigate.server import HistoryServer
from investigate.service import execute_run
from tests.fakes import FakeRunner
from tests.test_runner import STUB, _request
from tests.test_server import Client

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _run(home, **kwargs):
    runner = FakeRunner(**kwargs)
    result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
    return result, runner


def _ask(home, result, question, runner):
    return run_followup(home, result.id, result.folder, question, runner=runner)


# resume on the runner


def test_resume_command_uses_resume_instead_of_session_id(tmp_path):
    from investigate.runner import build_command

    cmd = build_command(["claude"], _request(tmp_path), resume=True)
    assert cmd[cmd.index("--resume") + 1] == "11111111-1111-1111-1111-111111111111"
    assert "--session-id" not in cmd  # the CLI refuses both together


def test_claude_runner_resume_runs_the_cli_with_resume(tmp_path):
    runner = ClaudeCodeRunner(command=[sys.executable, STUB])
    outcome = runner.resume(_request(tmp_path))
    assert outcome.ok and not outcome.resume_unavailable
    assert "--resume" in outcome.log and "--session-id" not in outcome.log


def test_claude_runner_flags_an_unknown_session(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "unknown_session")
    runner = ClaudeCodeRunner(command=[sys.executable, STUB])
    outcome = runner.resume(_request(tmp_path))
    assert not outcome.ok and outcome.resume_unavailable
    assert not runner.run(_request(tmp_path)).resume_unavailable  # only a resume can be one


# append and ordering


def test_followup_appends_and_keeps_earlier_content(home):
    result, runner = _run(home)
    before = result.report_path.read_text(encoding="utf-8")
    assert "The thing exists." in before

    entry = _ask(home, result, "What about X?", runner)

    assert entry["status"] == "completed" and entry["mode"] == "resumed"
    (request,) = runner.resumes
    assert request.session_id == "fake-session" and request.workdir == result.folder
    assert "What about X?" in request.prompt
    html = result.report_path.read_text(encoding="utf-8")
    assert "The thing exists." in html  # the original findings are still there
    assert "Follow-up 1" in html and "What about X?" in html
    assert "A follow-up claim 1." in html
    assert 'href="https://example.com/other1"' in html  # its own Citations
    data = json.loads((result.folder / "result.json").read_text(encoding="utf-8"))
    assert data["findings"][0]["claim"] == "The thing exists."
    (stored,) = data["followups"]
    assert stored["question"] == "What about X?"
    assert stored["result"]["findings"][0]["claim"] == "A follow-up claim 1."
    assert "=== Follow-up 1 ===" in (result.folder / "agent.log").read_text(encoding="utf-8")


def test_followups_stack_in_order(home):
    result, runner = _run(home)
    _ask(home, result, "first question", runner)
    _ask(home, result, "second question", runner)
    _ask(home, result, "third question", runner)

    html = result.report_path.read_text(encoding="utf-8")
    positions = [html.index(f"{n} question") for n in ("first", "second", "third")]
    assert positions == sorted(positions)
    assert html.count('class="followup"') == 3
    assert "A follow-up claim 3." in html
    assert [e["question"] for e in read_followups(result.folder)] == [
        "first question",
        "second question",
        "third question",
    ]
    data = json.loads((result.folder / "result.json").read_text(encoding="utf-8"))
    assert [f["question"] for f in data["followups"]] == [
        "first question",
        "second question",
        "third question",
    ]
    assert html.count("<!--investigate:followups-->") == 1  # markers are not duplicated


def test_followup_text_is_escaped(home):
    result, runner = _run(home)
    _ask(home, result, "<script>alert(1)</script>", runner)
    html = result.report_path.read_text(encoding="utf-8")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_an_old_report_without_markers_still_gets_the_section(home):
    result, runner = _run(home)
    html = result.report_path.read_text(encoding="utf-8")
    for mark in ("<!--investigate:followups-->", "<!--/investigate:followups-->"):
        html = html.replace(mark, "")
    html = html.replace("<!--investigate:followup-box-->", "")
    html = html.replace("<!--investigate:head-->", "")
    result.report_path.write_text(html, encoding="utf-8")
    _ask(home, result, "late question", runner)
    assert "late question" in result.report_path.read_text(encoding="utf-8")
    manager = FollowupManager(home, runner)
    manager._active[result.id] = "late question"  # a Follow-up in progress on the old Report
    page = manager.decorate(
        result.id, result.report_path.read_text(encoding="utf-8"), busy_run=False
    )
    assert '<meta http-equiv="refresh"' in page


# timeout and failure


def test_followup_timeout_is_partial_and_keeps_what_was_written(home):
    result, runner = _run(home)
    runner.resume_timed_out = True
    entry = _ask(home, result, "slow one", runner)
    assert entry["status"] == "partial"
    html = result.report_path.read_text(encoding="utf-8")
    assert "time limit" in html and "A follow-up claim 1." in html
    assert "The thing exists." in html


def test_followup_uses_the_depth_timeout(home):
    result, runner = _run(home)
    _ask(home, result, "q", runner)
    assert runner.resumes[0].timeout_seconds == runner.requests[0].timeout_seconds


def test_failed_followup_is_recorded_and_earlier_content_survives(home):
    result, runner = _run(home)
    runner.resume_ok = False
    runner.answer = None
    entry = _ask(home, result, "doomed", runner)
    assert entry["status"] == "failed"
    html = result.report_path.read_text(encoding="utf-8")
    assert "Follow-up failed: resume failed" in html and "The thing exists." in html
    assert [e.status for e in history.load_entries(home)] == ["completed"]  # the Run is unchanged


def test_a_runner_that_raises_becomes_a_failed_followup(home):
    result, runner = _run(home)

    def boom(request):
        raise RuntimeError("kaput")

    runner.resume = boom
    entry = _ask(home, result, "q", runner)
    assert entry["status"] == "failed" and "kaput" in entry["error"]


def test_malformed_answer_shows_raw_text_and_problems(home):
    result, runner = _run(home, answer="Just prose, no JSON block.")
    _ask(home, result, "q", runner)
    html = result.report_path.read_text(encoding="utf-8")
    assert "Just prose, no JSON block." in html and "malformed" in html


# fallback


def test_no_session_id_starts_a_linked_new_run(home):
    result, runner = _run(home, session_id=None)
    # the Run itself always gets a session id from execute_run; a Run without one is an old index
    entry_ = history.load_entries(home)[0]
    history.upsert(home, entry_.__class__(**{**entry_.__dict__, "session_id": None}))
    run_json = result.folder / "run.json"
    data = json.loads(run_json.read_text(encoding="utf-8"))
    data["session_id"] = None
    run_json.write_text(json.dumps(data), encoding="utf-8")

    entry = _ask(home, result, "and what else?", runner)

    assert runner.resumes == []  # nothing to resume
    assert entry["mode"] == "new_run" and entry["new_run_id"]
    new_id = entry["new_run_id"]
    assert new_id != result.id
    new_folder = home / "runs" / new_id
    item = (new_folder / "item.md").read_text(encoding="utf-8")
    assert "and what else?" in item and "The thing exists." in item  # the previous Report is input
    assert json.loads((new_folder / "run.json").read_text(encoding="utf-8"))["followup_of"] == (
        result.id
    )
    by_id = {e.id: e for e in history.load_entries(home)}
    assert by_id[new_id].followup_of == result.id and by_id[result.id].followup_of is None
    html = result.report_path.read_text(encoding="utf-8")
    assert "no session id" in html and f"/runs/{new_id}/" in html
    assert "The thing exists." in html


def test_unresumable_session_falls_back_to_a_new_run(home):
    result, runner = _run(home)
    runner.resume_unavailable = True
    entry = _ask(home, result, "still there?", runner)
    assert entry["mode"] == "new_run"
    assert len(runner.resumes) == 1  # it tried first
    assert "could no longer be resumed" in result.report_path.read_text(encoding="utf-8")
    assert len(history.load_entries(home)) == 2


def test_old_index_entries_load_without_followup_of(home):
    path = home / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": "a",
                        "kind": "investigation",
                        "depth": "quick",
                        "status": "completed",
                        "title": "t",
                        "created": "x",
                        "session_id": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert history.load_entries(home)[0].followup_of is None


# the served page and the POST route


class BlockingResume(FakeRunner):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def resume(self, request):
        self.started.set()
        assert self.release.wait(10)
        return super().resume(request)


@pytest.fixture
def served(home):
    runner = BlockingResume()
    runner.release.set()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    result = execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)
    yield srv, Client(srv), result, runner
    runner.release.set()
    srv.stop()


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def test_served_report_has_the_box_and_the_file_does_not(served):
    srv, client, result, _ = served
    status, headers, body = client.get(f"/runs/{result.id}/")
    page = body.decode()
    assert status == 200
    assert f'action="/runs/{result.id}/followup"' in page and "Ask a follow-up" in page
    assert "<script" not in page and "http-equiv" not in page
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp and "form-action 'self'" in csp
    file_html = result.report_path.read_text(encoding="utf-8")
    assert "<form" not in file_html and "Ask a follow-up" not in file_html  # file view stays clean


def test_posting_a_followup_runs_in_the_background_and_shows_progress(served):
    srv, client, result, runner = served
    runner.release.clear()
    status, headers, _ = client.post(f"/runs/{result.id}/followup", b"question=Why%3F", FORM)
    assert status == 303 and headers["Location"] == f"/runs/{result.id}/"
    assert runner.started.wait(10)
    page = client.get(f"/runs/{result.id}/")[2].decode()
    assert '<meta http-equiv="refresh"' in page and "Follow-up running" in page
    assert "Why?" in page and "<form" not in page  # no second box while one runs
    assert result.id in srv.active_ids()
    assert client.post(f"/runs/{result.id}/delete")[0] == 409

    runner.release.set()
    assert srv.followups.wait()
    page = client.get(f"/runs/{result.id}/")[2].decode()
    assert "http-equiv" not in page and "Follow-up 1" in page and "A follow-up claim 1." in page
    assert result.id not in srv.active_ids()


def test_a_second_followup_at_the_same_time_is_refused_with_409(served):
    srv, client, result, runner = served
    runner.release.clear()
    path = f"/runs/{result.id}/followup"
    assert (
        client.post(
            path, json.dumps({"question": "one"}).encode(), {"Content-Type": "application/json"}
        )[0]
        == 202
    )
    assert runner.started.wait(10)
    status, _, body = client.post(path, b"question=two", FORM)
    assert status == 409 and b"already running" in body
    runner.release.set()
    assert srv.followups.wait()
    assert client.post(path, b"question=three", FORM)[0] == 303  # free again afterwards
    assert srv.followups.wait()
    assert [e["question"] for e in read_followups(result.folder)] == ["one", "three"]


def test_two_racing_posts_start_exactly_one_followup(served):
    srv, client, result, runner = served
    runner.release.clear()
    path = f"/runs/{result.id}/followup"
    statuses = []

    def post(n):
        statuses.append(client.post(path, f"question=q{n}".encode(), FORM)[0])

    threads = [threading.Thread(target=post, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    runner.release.set()
    assert srv.followups.wait()
    assert sorted(statuses) == [303, 409, 409, 409]
    assert len(read_followups(result.folder)) == 1
    json.loads((result.folder / "result.json").read_text(encoding="utf-8"))  # not corrupted


def test_followup_post_goes_through_the_origin_and_host_guard(served):
    srv, client, result, _ = served
    path = f"/runs/{result.id}/followup"
    foreign = {**FORM, "Origin": "http://evil.example"}
    assert client.request("POST", path, b"question=x", foreign)[0] == 403
    assert client.request("POST", path, b"question=x", {**FORM, "Host": "evil.example"})[0] == 403
    assert read_followups(result.folder) == []


def test_followup_post_body_cap_applies(served):
    srv, client, result, _ = served
    (srv.home / "config.toml").write_text("max_attachment_mb = 0.001" + chr(10), encoding="utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.putrequest("POST", f"/runs/{result.id}/followup")
    conn.putheader("Origin", f"http://127.0.0.1:{srv.port}")
    conn.putheader("Content-Type", FORM["Content-Type"])
    conn.putheader("Content-Length", str(50 * 1024 * 1024))
    conn.endheaders()  # no body is sent: the cap must answer before it is read
    assert conn.getresponse().status == 413
    conn.close()


def test_bad_followup_requests(served):
    srv, client, result, _ = served
    path = f"/runs/{result.id}/followup"
    assert client.post(path, b"question=%20", FORM)[0] == 400
    assert client.post(path, b"{}", {"Content-Type": "application/json"})[0] == 400
    assert client.post(path, b"not json", {"Content-Type": "application/json"})[0] == 400
    assert client.post(path, b"x", {"Content-Type": "text/plain"})[0] == 400
    long_q = b"question=" + b"a" * 10_001
    assert client.post(path, long_q, FORM)[0] == 413
    assert client.post("/runs/nope/followup", b"question=x", FORM)[0] == 404
    assert client.post("/runs/..%2f..%2fx/followup", b"question=x", FORM)[0] == 404


def test_followup_on_a_running_run_is_409(home):
    class Blocking(FakeRunner):
        def __init__(self):
            super().__init__()
            self.started, self.release = threading.Event(), threading.Event()

        def run(self, request):
            self.started.set()
            assert self.release.wait(10)
            return super().run(request)

    runner = Blocking()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    try:
        client = Client(srv)
        run_id = json.loads(
            client.post(
                "/api/runs",
                json.dumps({"text": "x"}).encode(),
                {"Content-Type": "application/json"},
            )[2]
        )["id"]
        assert runner.started.wait(10)
        assert client.post(f"/runs/{run_id}/followup", b"question=x", FORM)[0] == 409
    finally:
        runner.release.set()
        srv.wait_for_runs()
        srv.stop()


def test_box_says_so_when_there_is_no_session(served):
    srv, client, result, _ = served
    run_json = result.folder / "run.json"
    data = json.loads(run_json.read_text(encoding="utf-8"))
    data["session_id"] = None
    run_json.write_text(json.dumps(data), encoding="utf-8")
    entry = history.load_entries(srv.home)[0]
    history.upsert(srv.home, entry.__class__(**{**entry.__dict__, "session_id": None}))
    page = client.get(f"/runs/{result.id}/")[2].decode()
    assert "no session to resume" in page

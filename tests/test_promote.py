"""Promote to Investigation: a POST through the server's guard that starts a linked Run."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investigate import history
from investigate.attachments import Upload
from investigate.config import Depth
from investigate.server import HistoryServer
from investigate.service import execute_run
from tests.fakes import GOOD_FACTCHECK, FakeRunner
from tests.test_server import Client

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def server(home):
    runner = FakeRunner()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    srv.fake = runner
    yield srv
    srv.stop()


def _fact_check(home):
    return execute_run(
        "Video title\nhttps://example.com/v\n\nTranscript: attached.",
        Depth.QUICK,
        home=home,
        runner=FakeRunner(GOOD_FACTCHECK),
        now=NOW,
        kind="fact-check",
        attachments=[Upload("transcript.txt", b"hello transcript")],
    )


def _entries(home):
    return {e.id: e for e in history.load_entries(home)}


def test_promote_creates_a_linked_investigation_on_the_same_item(home, server):
    fc = _fact_check(home)
    status, headers, _ = Client(server).post(f"/runs/{fc.id}/promote", b"depth=deep", FORM)
    assert (status, headers["Location"]) == (303, "/")
    assert server.wait_for_runs()

    entries = _entries(home)
    (promoted,) = [e for e in entries.values() if e.promoted_from == fc.id]
    assert promoted.kind == "investigation"
    assert promoted.depth == "deep"
    assert promoted.status == "completed"
    folder = home / "runs" / promoted.id
    assert (folder / "item.md").read_text(encoding="utf-8") == (
        (fc.folder / "item.md").read_text(encoding="utf-8")
    )
    assert (folder / "attachments" / "transcript.txt").read_bytes() == b"hello transcript"
    assert "research agent" in server.fake.requests[-1].prompt
    assert "fact-checking agent" not in server.fake.requests[-1].prompt
    assert "Promoted from the Fact-check" in (folder / "report.html").read_text(encoding="utf-8")
    assert entries[fc.id].promoted_from is None


def test_promote_defaults_to_the_fact_checks_depth(home, server):
    fc = _fact_check(home)
    Client(server).post(f"/runs/{fc.id}/promote", b"", FORM)
    assert server.wait_for_runs()
    (promoted,) = [e for e in _entries(home).values() if e.promoted_from == fc.id]
    assert promoted.depth == "quick"


def test_promoting_twice_does_not_start_a_second_investigation(home, server):
    fc = _fact_check(home)
    client = Client(server)
    client.post(f"/runs/{fc.id}/promote", b"", FORM)
    client.post(f"/runs/{fc.id}/promote", b"", FORM)
    assert server.wait_for_runs()
    assert len([e for e in _entries(home).values() if e.promoted_from == fc.id]) == 1


def test_a_failed_promotion_can_be_promoted_again(home, server):
    fc = _fact_check(home)
    server.promote(fc.id)
    assert server.wait_for_runs()
    (first,) = [e for e in _entries(home).values() if e.promoted_from == fc.id]
    history.upsert(home, first.__class__(**{**first.__dict__, "status": "failed"}))
    second = server.promote(fc.id)
    assert second != first.id
    assert server.wait_for_runs()


def test_promote_refuses_investigations_unknown_ids_and_bad_depth(home, server):
    client = Client(server)
    plain = execute_run("q", Depth.QUICK, home=home, runner=FakeRunner(), now=NOW)
    assert client.post(f"/runs/{plain.id}/promote", b"", FORM)[0] == 400
    assert client.post("/runs/nope/promote", b"", FORM)[0] == 404
    fc = _fact_check(home)
    assert client.post(f"/runs/{fc.id}/promote", b"depth=bogus", FORM)[0] == 400
    assert len(history.load_entries(home)) == 2


def test_promote_is_refused_while_the_fact_check_runs(home, server):
    fc = _fact_check(home)
    entry = _entries(home)[fc.id]
    history.upsert(home, entry.__class__(**{**entry.__dict__, "status": "running"}))
    assert Client(server).post(f"/runs/{fc.id}/promote", b"", FORM)[0] == 409


def test_promote_goes_through_the_guard_cross_origin_is_refused(home, server):
    fc = _fact_check(home)
    status, _, _ = Client(server).request(
        "POST", f"/runs/{fc.id}/promote", b"depth=deep", {**FORM, "Origin": "http://evil.example"}
    )
    assert status == 403
    assert len(history.load_entries(home)) == 1

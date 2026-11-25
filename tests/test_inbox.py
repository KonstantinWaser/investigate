"""The Inbox pipeline, its config and the `investigate inbox` command, offline."""

from __future__ import annotations

import threading
import time as clock
from datetime import time

import pytest

from investigate import history
from investigate.cli import main
from investigate.config import ConfigError, Depth, config_path
from investigate.inbox import (
    InboxBusy,
    InboxState,
    ProcessedIds,
    check_inbox,
    inbox_lock,
    load_inbox_config,
)
from investigate.server import HistoryServer
from tests.fake_inbox import FakeSource
from tests.fakes import GOOD_FACTCHECK, FakeRunner


class Recorder:
    def __init__(self, fail_on=()) -> None:
        self.items = []
        self.fail_on = set(fail_on)

    def __call__(self, item):
        if item.id in self.fail_on:
            raise OSError("disk full")
        self.items.append(item)
        return f"run-{item.id}"


# pipeline


def test_unseen_items_become_runs_and_a_rerun_does_nothing(home):
    source, enqueue = FakeSource(), Recorder()
    first = check_inbox(home, [source], enqueue)
    assert [c.run_id for c in first.created] == ["run-id0", "run-id1"]
    assert first.ok and first.already_processed == 0

    second = check_inbox(home, [source], enqueue)
    assert second.created == [] and second.already_processed == 2 and second.ok
    assert len(enqueue.items) == 2
    assert source.fetched == ["id0", "id1"]  # the second pass fetched nothing


def test_only_new_items_are_processed_later(home):
    source, enqueue = FakeSource(titles=("A",)), Recorder()
    check_inbox(home, [source], enqueue)
    source.refs += FakeSource(titles=("A", "B")).refs[1:]
    result = check_inbox(home, [source], enqueue)
    assert [c.title for c in result.created] == ["B"]


def test_processed_ids_are_stored_per_source_under_the_data_folder(home):
    check_inbox(home, [FakeSource()], Recorder())
    assert (home / "inbox" / "fake.json").is_file()
    assert ProcessedIds(home, "fake").ids() == {"id0", "id1"}
    assert ProcessedIds(home, "other").ids() == set()


def test_an_id_is_recorded_only_after_its_run_was_created(home):
    source, enqueue = FakeSource(), Recorder(fail_on={"id0"})
    result = check_inbox(home, [source], enqueue)
    assert ProcessedIds(home, "fake").ids() == set()  # nothing lost, so it is tried again
    assert result.created == [] and "not queued" in result.errors[0]

    retry = check_inbox(home, [source], Recorder())
    assert [c.title for c in retry.created] == ["A video", "B video"]


def test_one_unreadable_item_is_skipped_and_retried_next_time(home):
    source = FakeSource(fail_fetch={"id0"})
    result = check_inbox(home, [source], Recorder())
    assert [c.title for c in result.created] == ["B video"]
    assert len(result.errors) == 1 and not result.unreachable
    source.fail_fetch.clear()
    assert [c.title for c in check_inbox(home, [source], Recorder()).created] == ["A video"]


def test_an_unreadable_list_is_reported_as_unreachable_and_other_sources_still_run(home):
    broken = FakeSource(fail_list=OSError("offline"))
    good = FakeSource(titles=("Only",))
    good.name = "good"
    result = check_inbox(home, [broken, good], Recorder())
    assert result.unreachable and not result.ok
    assert [c.title for c in result.created] == ["Only"]


def test_stop_ends_the_check_early_without_recording_the_rest(home):
    source, enqueue = FakeSource(), Recorder()
    calls = iter([False, True])
    result = check_inbox(home, [source], enqueue, stop=lambda: next(calls, True))
    assert [c.title for c in result.created] == ["A video"]
    assert ProcessedIds(home, "fake").ids() == {"id0"}


def test_two_checks_cannot_run_at_once(home):
    inner = []
    with inbox_lock(home):
        inner.append(check_inbox(home, [FakeSource()], Recorder()))
        with pytest.raises(InboxBusy), inbox_lock(home):
            pass
    assert inner[0].busy and inner[0].created == []
    with inbox_lock(home):  # released afterwards
        pass


def test_a_damaged_processed_file_reads_as_empty(home):
    (home / "inbox").mkdir(parents=True)
    (home / "inbox" / "fake.json").write_text("{not json", encoding="utf-8")
    assert ProcessedIds(home, "fake").ids() == set()
    assert len(check_inbox(home, [FakeSource()], Recorder()).created) == 2


def test_last_check_round_trips_and_survives_a_bad_file(home):
    state = InboxState(home)
    assert state.last_check() is None
    from datetime import datetime

    when = datetime(2026, 3, 4, 8, 5).astimezone()
    state.record(when)
    assert state.last_check() == when
    (home / "inbox" / "state.json").write_text("[]", encoding="utf-8")
    assert state.last_check() is None


# config


def _write(home, text):
    home.mkdir(exist_ok=True)
    config_path(home).write_text(text, encoding="utf-8")


def test_defaults_when_there_is_no_inbox_section(home):
    config = load_inbox_config(home)
    assert not config.configured
    assert config.check_times == (time(8, 0), time(18, 0))
    assert config.depth is Depth.STANDARD and config.cookies is None


def test_inbox_section_is_parsed(home):
    _write(
        home,
        '[inbox]\ncheck_times = ["19:30", "07:15"]\ndepth = "quick"\n'
        '[inbox.youtube]\nplaylist = "PLabcdefghijklmnop"\ncookies = "yt-cookies.txt"\n'
        'subtitle_languages = ["fr"]\n',
    )
    config = load_inbox_config(home)
    assert config.check_times == (time(7, 15), time(19, 30))
    assert config.depth is Depth.QUICK
    assert config.playlist == "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"
    assert config.cookies == (home / "yt-cookies.txt").resolve()
    assert config.subtitle_languages == ("fr",)


@pytest.mark.parametrize(
    "text",
    [
        '[inbox]\ncheck_times = ["25:00"]',
        "[inbox]\ncheck_times = []",
        '[inbox]\ndepth = "huge"',
        '[inbox.youtube]\nplaylist = "My saved videos"',
        "[inbox.youtube]\nplaylist = 5",
        "[inbox.youtube]\ncookies = 5",
        "[inbox.youtube]\nsubtitle_languages = []",
        "inbox = 3",
    ],
)
def test_bad_inbox_config_is_a_config_error(home, text):
    _write(home, text)
    with pytest.raises(ConfigError):
        load_inbox_config(home)


# the command and the real queue


def _configure(home):
    _write(home, '[inbox.youtube]\nplaylist = "PLabcdefghijklmnop"\n')


def test_cli_inbox_checks_new_videos_through_the_queue_and_rerun_is_empty(home, capsys):
    _configure(home)
    runner = FakeRunner(GOOD_FACTCHECK)
    sources = lambda config: [FakeSource()]  # noqa: E731

    assert main(["inbox"], runner=runner, inbox_sources=sources) == 0
    out = capsys.readouterr().out
    assert "2 new, 0 already checked" in out and "completed: A video" in out

    entries = history.load_entries(home)
    assert len(entries) == 2
    assert {e.kind for e in entries} == {"fact-check"}
    assert all(e.verdicts == {"confirmed": 1, "partly_true": 1, "wrong": 1} for e in entries)
    assert "fact-checking agent" in runner.requests[0].prompt

    assert main(["inbox"], runner=runner, inbox_sources=sources) == 0
    assert "0 new, 2 already checked" in capsys.readouterr().out
    assert len(history.load_entries(home)) == 2 and len(runner.requests) == 2


def test_cli_inbox_without_a_playlist_explains_what_to_set(home, capsys):
    assert main(["inbox"], runner=FakeRunner()) == 2
    assert "[inbox.youtube]" in capsys.readouterr().err


def test_cli_inbox_with_a_playlist_name_is_a_config_error(home, capsys):
    _write(home, '[inbox.youtube]\nplaylist = "Watch later"\n')
    assert main(["inbox"], runner=FakeRunner()) == 2
    assert "id or URL" in capsys.readouterr().err


def test_fact_checks_queue_like_any_run_and_show_as_queued(home):
    gate = threading.Event()

    class Gated(FakeRunner):
        def run(self, request):
            assert gate.wait(10)
            return super().run(request)

        def stop(self):
            gate.set()

    server = HistoryServer(home, Gated(GOOD_FACTCHECK), port=0, max_parallel=1)
    try:
        check_inbox(
            home,
            [FakeSource()],
            lambda item: server.submit_fact_check(item.text, Depth.QUICK, item.attachments),
        )
        deadline = clock.monotonic() + 5
        while clock.monotonic() < deadline and not (
            len(server.queue.running_ids()) == 1
            and sorted(e.status for e in history.load_entries(home)) == ["queued", "running"]
        ):
            clock.sleep(0.02)
        assert sorted(e.status for e in history.load_entries(home)) == ["queued", "running"]
        assert len(server.queue.queued_ids()) == 1
        gate.set()
        assert server.wait_for_runs()
        assert {e.status for e in history.load_entries(home)} == {"completed"}
    finally:
        gate.set()
        server.shutdown(1)

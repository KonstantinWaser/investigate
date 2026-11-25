"""The inbox timer with a fake clock, and its wiring into the tray (menu, toast, quit)."""

from __future__ import annotations

import threading
import time as clock
from datetime import datetime, time, timedelta, timezone

import pytest

from investigate import history
from investigate.config import config_path
from investigate.inbox import InboxState
from investigate.inbox_timer import InboxScheduler
from investigate.tray import TrayApp
from tests.fake_inbox import FakeSource
from tests.fakes import GOOD_FACTCHECK, FakeRunner
from tests.test_tray import RecordingNotifier

TZ = timezone(timedelta(hours=1))
TIMES = (time(8, 0), time(18, 0))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=TZ)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _scheduler(home, now, check=None, **kwargs):
    fake = FakeClock(now)
    calls = []

    def default(manual):
        calls.append(manual)
        return True

    scheduler = InboxScheduler(InboxState(home), TIMES, check or default, clock=fake, **kwargs)
    return scheduler, fake, calls


# schedule


def test_latest_slot_is_the_most_recent_one_even_across_midnight(home):
    scheduler, _, _ = _scheduler(home, at(4, 12))
    assert scheduler.latest_slot(at(4, 12)) == at(4, 8)
    assert scheduler.latest_slot(at(4, 18)) == at(4, 18)
    assert scheduler.latest_slot(at(4, 7, 59)) == at(3, 18)
    assert scheduler.latest_slot(at(4, 23)) == at(4, 18)


def test_a_first_start_with_no_recorded_check_runs_one(home):
    scheduler, _, calls = _scheduler(home, at(4, 12))
    assert scheduler.tick() is True
    assert calls == [False]


def test_a_check_runs_when_a_slot_passes_and_not_again_until_the_next(home):
    scheduler, clk, calls = _scheduler(home, at(4, 7, 59))
    InboxState(home).record(at(3, 18, 5))
    assert scheduler.tick() is False  # 07:59, the 18:00 slot of yesterday was already covered

    clk.now = at(4, 8, 0)
    assert scheduler.tick() is True
    assert calls == [False]
    clk.now = at(4, 12)
    assert scheduler.tick() is False  # already checked after the 08:00 slot
    clk.now = at(4, 18, 0)
    assert scheduler.tick() is True
    assert len(calls) == 2
    assert InboxState(home).last_check() == at(4, 18, 0)


def test_start_up_catches_up_a_slot_missed_while_the_app_was_off(home):
    InboxState(home).record(at(3, 18, 5))
    scheduler, clk, calls = _scheduler(home, at(4, 12))  # the 08:00 slot passed unseen
    assert scheduler.is_due(clk.now)
    scheduler.tick()
    assert calls == [False]


def test_start_up_does_not_check_when_nothing_was_missed(home):
    InboxState(home).record(at(4, 8, 30))
    scheduler, clk, calls = _scheduler(home, at(4, 12))
    assert not scheduler.is_due(clk.now)
    assert scheduler.tick() is False and calls == []


def test_a_failed_check_is_retried_after_the_delay_and_not_recorded(home):
    outcomes = iter([False, False, True])
    scheduler, clk, _ = _scheduler(home, at(4, 8, 1), lambda manual: next(outcomes))
    assert scheduler.tick() is True
    assert InboxState(home).last_check() is None

    clk.now = at(4, 8, 5)
    assert scheduler.tick() is False  # still inside the retry delay
    clk.now = at(4, 8, 12)
    assert scheduler.tick() is True  # second failure
    clk.now = at(4, 8, 23)
    assert scheduler.tick() is True  # success
    assert InboxState(home).last_check() == at(4, 8, 23)
    clk.now = at(4, 8, 40)
    assert scheduler.tick() is False


def test_a_check_that_raises_is_treated_as_a_failure(home):
    def boom(manual):
        raise RuntimeError("x")

    scheduler, _, _ = _scheduler(home, at(4, 8, 1), boom)
    assert scheduler.tick() is True
    assert InboxState(home).last_check() is None


def test_check_now_runs_even_when_nothing_is_due_or_a_retry_waits(home):
    InboxState(home).record(at(4, 8, 30))
    scheduler, _, calls = _scheduler(home, at(4, 9))
    assert scheduler.check_now() is True
    assert calls == [True]


def test_two_checks_never_run_at_once(home):
    entered, release = threading.Event(), threading.Event()

    def slow(manual):
        entered.set()
        assert release.wait(5)
        return True

    scheduler, _, _ = _scheduler(home, at(4, 12), slow)
    first = threading.Thread(target=scheduler.check_now)
    first.start()
    assert entered.wait(5)
    assert scheduler.checking
    assert scheduler.check_now() is False
    assert scheduler.tick() is False
    assert scheduler.check_now_in_background() is False
    release.set()
    first.join(5)
    assert not scheduler.checking


def test_the_background_thread_catches_up_immediately_and_stops(home):
    done = threading.Event()

    def check(manual):
        done.set()
        return True

    scheduler = InboxScheduler(
        InboxState(home), TIMES, check, clock=FakeClock(at(4, 12)), poll_seconds=0.01
    )
    scheduler.start()
    try:
        assert done.wait(5)
    finally:
        scheduler.stop()
    assert scheduler._thread is None


def test_a_scheduler_needs_at_least_one_time(home):
    with pytest.raises(ValueError):
        InboxScheduler(InboxState(home), (), lambda manual: True)


# tray wiring


def _tray(home, sources, **kwargs):
    config_path(home).parent.mkdir(parents=True, exist_ok=True)
    config_path(home).write_text(
        '[inbox.youtube]\nplaylist = "PLabcdefghijklmnop"\n', encoding="utf-8"
    )
    notifier = RecordingNotifier()
    app = TrayApp(
        home,
        FakeRunner(GOOD_FACTCHECK),
        port=0,
        notifier=notifier,
        open_url=lambda url: None,
        open_capture=lambda url: None,
        quit_grace=0.5,
        inbox_sources=lambda config: sources,
        clock=FakeClock(at(4, 12)),
        inbox_poll_seconds=3600,
        **kwargs,
    )
    return app, notifier


def _wait(condition, timeout=5.0):
    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        if condition():
            return True
        clock.sleep(0.02)
    return False


def test_start_up_check_queues_fact_checks_and_one_toast_summarises_them(home):
    app, notifier = _tray(home, [FakeSource()])
    app.start()
    try:
        assert _wait(lambda: len(history.load_entries(home)) == 2)
        assert _wait(lambda: any(t.title == "2 new Fact-checks" for t in notifier.toasts))
        summary = next(t for t in notifier.toasts if t.title == "2 new Fact-checks")
        assert "A video" in summary.message and "B video" in summary.message
        assert summary.url.endswith("?kind=fact-check")
        assert {e.kind for e in history.load_entries(home)} == {"fact-check"}
        assert app.server.wait_for_runs()
    finally:
        app.shutdown()


def test_check_inbox_now_is_enabled_and_checks_again_with_nothing_new(home):
    source = FakeSource()
    app, notifier = _tray(home, [source])
    app.start()
    try:
        assert _wait(lambda: len(history.load_entries(home)) == 2)
        assert _wait(lambda: InboxState(home).last_check() is not None)
        item = {i.label: i for i in app.menu_items()}["Check inbox now"]
        assert item.enabled
        item.action()
        assert _wait(lambda: any(t.message == "Nothing new in the inbox." for t in notifier.toasts))
        assert len(history.load_entries(home)) == 2
        assert source.fetched == ["id0", "id1"]
    finally:
        app.shutdown()


def test_check_inbox_now_without_a_playlist_says_how_to_set_it_up(home):
    app = TrayApp(
        home,
        FakeRunner(),
        port=0,
        notifier=(notifier := RecordingNotifier()),
        clock=FakeClock(at(4, 12)),
        inbox_poll_seconds=3600,
    )
    app.start()
    try:
        assert _wait(lambda: InboxState(home).last_check() is not None)  # start-up pass done
        app.on_check_inbox()
        assert _wait(lambda: any(t.title == "Inbox not set up" for t in notifier.toasts))
        assert history.load_entries(home) == []  # the scheduled pass stayed silent
        assert [t.title for t in notifier.toasts] == ["Inbox not set up"]
    finally:
        app.shutdown()


def test_an_unreachable_playlist_is_retried_not_recorded_and_toasted(home):
    app, notifier = _tray(home, [FakeSource(fail_list=OSError("offline"))])
    app.start()
    try:
        assert _wait(lambda: any(t.title == "Inbox check had problems" for t in notifier.toasts))
        assert _wait(lambda: not app.inbox.checking)
        assert InboxState(home).last_check() is None
    finally:
        app.shutdown()


def test_a_bad_inbox_section_stops_the_tray_from_starting(home):
    config_path(home).parent.mkdir(parents=True, exist_ok=True)
    config_path(home).write_text('[inbox]\ncheck_times = ["nope"]\n', encoding="utf-8")
    app = TrayApp(home, FakeRunner(), port=0)
    with pytest.raises(ValueError):
        app.start()
    app.server.stop()

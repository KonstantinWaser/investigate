"""Instagram in the Inbox pipeline: config, outages of one source, notices, the tray and CLI."""

from __future__ import annotations

import pytest

from investigate import history
from investigate.cli import main
from investigate.config import ConfigError, config_path
from investigate.inbox import (
    LoginRequired,
    Notices,
    ProcessedIds,
    SourceBusy,
    check_inbox,
    default_sources,
    load_inbox_config,
)
from investigate.instagram import InstagramSource
from investigate.server import HistoryServer
from investigate.webpages import Filters, history_page
from tests.fake_inbox import FakeSource
from tests.fakes import GOOD_FACTCHECK, FakeRunner
from tests.test_inbox import Recorder
from tests.test_inbox_timer import _tray, _wait


class Named(FakeSource):
    def __init__(self, name, **kwargs):
        super().__init__(**kwargs)
        self.name = name


def _write(home, text):
    config_path(home).parent.mkdir(parents=True, exist_ok=True)
    config_path(home).write_text(text, encoding="utf-8")


# config


def test_the_collection_is_read_from_the_instagram_table(home):
    _write(home, '[inbox.instagram]\ncollection = "To Check"\nmax_duration_minutes = 5\n')
    config = load_inbox_config(home)
    assert config.instagram_collection == "To Check"
    assert config.instagram_max_duration_minutes == 5
    assert config.playlist is None and config.configured


def test_no_instagram_table_keeps_the_defaults(home):
    config = load_inbox_config(home)
    assert config.instagram_collection is None and not config.configured


@pytest.mark.parametrize(
    "text",
    [
        "[inbox]\ninstagram = 3\n",
        '[inbox.instagram]\ncollection = ""\n',
        "[inbox.instagram]\ncollection = 4\n",
        '[inbox.instagram]\ncollection = "x"\nmax_duration_minutes = 0\n',
    ],
)
def test_a_bad_instagram_table_is_a_config_error(home, text):
    _write(home, text)
    with pytest.raises(ConfigError):
        load_inbox_config(home)


def test_default_sources_enable_instagram_only_when_a_collection_is_set(home):
    _write(home, '[inbox.instagram]\ncollection = "To Check"\n')
    sources = default_sources(load_inbox_config(home), home)
    assert [type(s) for s in sources] == [InstagramSource]
    assert sources[0].name == "instagram"
    assert default_sources(load_inbox_config(home.parent / "elsewhere"), home) == []


# outages of one source


class LoggedOut(Named):
    def list_items(self):
        raise LoginRequired("Instagram is logged out. Run `investigate login`.")


class Busy(Named):
    def list_items(self):
        raise SourceBusy("the browser profile is in use")


def test_a_logged_out_source_is_reported_and_the_other_source_still_runs(home):
    enqueue = Recorder()
    result = check_inbox(
        home, [LoggedOut("instagram"), Named("youtube", titles=("Video",))], enqueue
    )
    assert [c.source for c in result.created] == ["youtube"]
    assert result.login_required == ["instagram: Instagram is logged out. Run `investigate login`."]
    assert result.errors == [] and not result.unreachable
    assert not result.ok


def test_a_logged_out_source_leaves_a_notice_that_clears_when_it_works_again(home):
    check_inbox(home, [LoggedOut("instagram")], Recorder())
    assert "logged out" in Notices(home).all()["instagram"]

    check_inbox(home, [Named("instagram", titles=())], Recorder())
    assert Notices(home).all() == {}


def test_notices_survive_a_damaged_file_and_stay_per_source(home):
    notices = Notices(home)
    notices.set("instagram", "Instagram is logged out.")
    notices.set("youtube", "something")
    notices.clear("youtube")
    assert notices.all() == {"instagram": "Instagram is logged out."}
    (home / "inbox" / "notices.json").write_text("{nope", encoding="utf-8")
    assert notices.all() == {}


def test_a_busy_profile_skips_the_source_without_counting_an_attempt(home):
    enqueue = Recorder()
    for _ in range(5):  # far more than max_attempts
        result = check_inbox(home, [Busy("instagram")], enqueue, max_attempts=2)
        assert result.source_busy == ["instagram: the browser profile is in use"]
        assert result.errors == [] and result.gave_up == [] and not result.unreachable
    assert ProcessedIds(home, "instagram").ids() == set()
    assert not (home / "inbox" / "instagram.json").exists()
    assert Notices(home).all() == {}  # busy is not an outage


def test_instagram_items_are_recorded_by_shortcode_and_not_redone(home):
    source, enqueue = Named("instagram", titles=("Reel",)), Recorder()
    check_inbox(home, [source], enqueue)
    second = check_inbox(home, [source], enqueue)
    assert second.created == [] and second.already_processed == 1
    assert (home / "inbox" / "instagram.json").is_file()


# History notice


def test_the_history_page_shows_inbox_notices_escaped():
    page = history_page([], Filters(), set(), 0, notices=["Instagram <b>logged out</b>"])
    assert "Instagram &lt;b&gt;logged out&lt;/b&gt;" in page
    assert "<b>logged out</b>" not in page
    assert 'class="notice"' not in history_page([], Filters(), set(), 0)


def test_the_server_serves_the_notice_on_the_history_page(home):
    from tests.test_server import Client

    _write(home, '[inbox.instagram]\ncollection = "To Check"\n')
    Notices(home).set("instagram", "Instagram is logged out. Run investigate login.")
    server = HistoryServer(home, FakeRunner(), port=0)
    server.start()
    try:
        status, _, body = Client(server).get("/")
    finally:
        server.stop()
    assert status == 200 and "Instagram is logged out" in body.decode()


# tray


def test_a_logged_out_instagram_is_toasted_once_and_youtube_still_runs(home):
    app, notifier = _tray(home, [LoggedOut("instagram"), Named("youtube", titles=("V",))])
    app.start()
    try:
        assert _wait(lambda: len(history.load_entries(home)) == 1)
        assert _wait(lambda: any("logged out" in t.message for t in notifier.toasts))
        assert _wait(lambda: not app.inbox.checking)
        for _ in range(3):
            app._check_inbox(False)  # the outage is not toasted again
        out = [t for t in notifier.toasts if "logged out" in t.message]
        assert len(out) == 1
        assert Notices(home).all().get("instagram")
        assert app.server.wait_for_runs()
    finally:
        app.shutdown()


def test_a_busy_profile_is_retried_quietly_and_a_manual_check_says_so(home):
    app, notifier = _tray(home, [Busy("instagram")])
    app.start()
    try:
        assert _wait(lambda: not app.inbox.checking)
        assert notifier.toasts == []
        assert app._check_inbox(False) is False  # asks the scheduler to retry
        assert app._check_inbox(True) is False
        assert any("in use" in t.message for t in notifier.toasts)
    finally:
        app.shutdown()


# CLI


def test_cli_inbox_with_only_a_collection_runs_and_reports_a_logged_out_source(home, capsys):
    _write(home, '[inbox.instagram]\ncollection = "To Check"\n')
    sources = lambda config: [LoggedOut("instagram")]  # noqa: E731
    assert main(["inbox"], runner=FakeRunner(GOOD_FACTCHECK), inbox_sources=sources) == 1
    assert "logged out" in capsys.readouterr().err


def test_cli_inbox_reports_a_busy_profile_as_skipped_not_failed(home, capsys):
    _write(home, '[inbox.instagram]\ncollection = "To Check"\n')
    sources = lambda config: [Busy("instagram")]  # noqa: E731
    assert main(["inbox"], runner=FakeRunner(GOOD_FACTCHECK), inbox_sources=sources) == 0
    assert "in use" in capsys.readouterr().out


def test_cli_inbox_without_any_inbox_explains_both_settings(home, capsys):
    assert main(["inbox"], runner=FakeRunner()) == 2
    err = capsys.readouterr().err
    assert "[inbox.youtube]" in err and "[inbox.instagram]" in err


# the "no factual claims" Report


NO_CLAIMS = (
    "A joke reel with no factual claim.\n\n"
    '```json\n{"title": "Funny cat", "summary": "The post makes no factual claims.", '
    '"claims": []}\n```\n'
)


def test_a_post_with_no_claims_gets_a_short_report_without_verdicts(home):
    from datetime import UTC, datetime

    from investigate.config import Depth
    from investigate.service import execute_run

    result = execute_run(
        "Funny cat\nhttps://www.instagram.com/reel/X/",
        Depth.QUICK,
        home=home,
        runner=FakeRunner(NO_CLAIMS),
        now=datetime(2026, 3, 4, 12, 0, tzinfo=UTC),
        kind="fact-check",
    )
    html = (result.folder / "report.html").read_text(encoding="utf-8")
    assert "The post makes no factual claims." in html
    assert "no checkable claims" in html
    assert 'class="verdict' not in html
    (entry,) = history.load_entries(home)
    assert entry.status == "completed" and entry.verdicts in ({}, None)


def test_the_fact_check_prompt_covers_posts_images_and_the_no_claims_case():
    from investigate.config import Depth
    from investigate.prompt import build_prompt

    prompt = build_prompt("item", Depth.QUICK, "English", kind="fact-check")
    assert "Instagram" in prompt and "image" in prompt
    assert "empty claims list" in prompt


# review fixes


def test_instagram_is_read_before_youtube_so_its_lock_is_free_before_any_run_starts(home):
    _write(
        home,
        '[inbox.youtube]\nplaylist = "PLabcdefghijklmnop"\n'
        '[inbox.instagram]\ncollection = "To Check"\n',
    )
    names = [s.name for s in default_sources(load_inbox_config(home), home)]
    assert names == ["instagram", "youtube"]


class Switch(Named):
    mode = "ok"

    def list_items(self):
        if self.mode == "logged_out":
            raise LoginRequired("Instagram is logged out. Run investigate login.")
        if self.mode == "busy":
            raise SourceBusy("the browser profile is in use")
        return super().list_items()


def test_a_busy_skip_between_logged_out_checks_is_not_a_recovery(home):
    source = Switch("instagram", titles=())
    source.mode = "logged_out"
    app, notifier = _tray(home, [source])
    app.start()
    try:
        assert _wait(lambda: any("logged out" in t.message for t in notifier.toasts))
        assert _wait(lambda: not app.inbox.checking)
        source.mode = "busy"
        app._check_inbox(False)
        source.mode = "logged_out"
        app._check_inbox(False)
        assert len([t for t in notifier.toasts if "logged out" in t.message]) == 1
        assert not [t for t in notifier.toasts if "reachable again" in t.message]
        source.mode = "ok"
        app._check_inbox(False)
        assert any("reachable again" in t.message for t in notifier.toasts)
    finally:
        app.shutdown()


def test_a_youtube_outage_is_toasted_even_when_instagram_created_items(home):
    app, notifier = _tray(
        home, [Named("instagram", titles=("Reel",)), Named("youtube", fail_list=OSError("offline"))]
    )
    app.start()
    try:
        assert _wait(lambda: any(t.title == "Inbox check had problems" for t in notifier.toasts))
        assert any("1 new Fact-check" in t.title for t in notifier.toasts)
        assert _wait(lambda: not app.inbox.checking)
        app._check_inbox(False)  # still the same outage: not toasted again
        assert len([t for t in notifier.toasts if t.title == "Inbox check had problems"]) == 1
    finally:
        app.shutdown()


def test_a_notice_for_a_source_that_is_no_longer_configured_is_dropped(home):
    from investigate.inbox import active_notices

    Notices(home).set("instagram", "Instagram is logged out.")
    Notices(home).set("youtube", "Some problem.")
    _write(home, '[inbox.instagram]\ncollection = "To Check"\n')
    assert active_notices(home) == {"instagram": "Instagram is logged out."}
    assert Notices(home).all() == {"instagram": "Instagram is logged out."}
    _write(home, "")
    assert active_notices(home) == {} and Notices(home).all() == {}


def test_the_history_page_hides_the_notice_after_the_collection_is_removed(home):
    from tests.test_server import Client

    Notices(home).set("instagram", "Instagram is logged out.")
    server = HistoryServer(home, FakeRunner(), port=0)
    server.start()
    try:
        _, _, body = Client(server).get("/")
    finally:
        server.stop()
    assert b"logged out" not in body

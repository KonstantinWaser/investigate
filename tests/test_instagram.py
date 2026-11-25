"""The Instagram Inbox source: parsing saved fixtures, the Collection API, the source and the
browser reader. Offline: fixtures are synthetic JSON, the browser and downloads are fakes."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from investigate.browser import mark_logged_in, profile_dir
from investigate.inbox import InboxRef, ItemSkipped, LoginRequired, SourceBusy
from investigate.instagram import (
    InstagramApi,
    InstagramError,
    InstagramSource,
    Media,
    Post,
    browser_reader,
    find_collection,
    parse_collection_page,
)
from investigate.profile_lock import profile_lock
from tests.fake_inbox import FakeTranscriber

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# parsing


def test_find_collection_matches_the_name_ignoring_case_and_spaces():
    data = fixture("collections_list.json")
    assert find_collection(data, "to check") == "9002"
    assert find_collection(data, "  To Check ") == "9002"
    assert find_collection(data, "Missing") is None
    assert find_collection({"items": "oops"}, "x") is None


def test_a_page_yields_reels_posts_and_carousels_in_order():
    posts, cursor = parse_collection_page(fixture("collection_page1.json"))
    assert cursor == "cursor-2"
    reel, post = posts
    assert (reel.shortcode, reel.kind, reel.author) == ("REEL000001", "reel", "examplecreator")
    assert reel.caption.startswith("Honey never spoils")
    assert reel.taken_at == "2026-01-01"
    assert reel.media == (Media("video", "https://cdn.example.test/reel1_hd.mp4", 21.4),)
    assert (post.kind, post.caption) == ("post", "")
    assert post.media == (Media("image", "https://cdn.example.test/post2_big.jpg", None),)


def test_a_carousel_keeps_every_slide_and_skips_entries_without_a_code():
    posts, cursor = parse_collection_page(fixture("collection_page2.json"))
    assert cursor is None
    assert [p.shortcode for p in posts] == ["CARO000003"]  # the ad and the code-less item go
    carousel = posts[0]
    assert carousel.kind == "carousel"
    assert [m.kind for m in carousel.media] == ["image", "video", "image"]
    assert carousel.media[1].url == "https://cdn.example.test/car3_2.mp4"


@pytest.mark.parametrize("data", [{}, [], "text", {"items": None}, {"items": [1, None, {}]}])
def test_a_malformed_page_parses_to_nothing_without_raising(data):
    assert parse_collection_page(data) == ([], None)


# the API


class FakeGet:
    def __init__(self, routes):
        self.routes = routes
        self.paths: list[str] = []

    def __call__(self, path):
        self.paths.append(path)
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                return answer(path) if callable(answer) else answer
        return 404, {}


def _routes():
    def collection(path):
        name = "collection_page2.json" if "max_id=cursor-2" in path else "collection_page1.json"
        return 200, fixture(name)

    return {
        "/api/v1/collections/list/": (200, fixture("collections_list.json")),
        "/api/v1/feed/collection/9002/posts/": collection,
    }


def test_the_api_reads_every_page_of_the_named_collection():
    get = FakeGet(_routes())
    posts = InstagramApi(get).collection_posts("To Check")
    assert [p.shortcode for p in posts] == ["REEL000001", "POST000002", "CARO000003"]
    assert len(get.paths) == 3  # list of Collections, then two pages


def test_the_api_stops_at_the_page_limit():
    forever = (200, {**fixture("collection_page1.json"), "next_max_id": "again"})
    get = FakeGet({**_routes(), "/api/v1/feed/collection/9002/posts/": forever})
    InstagramApi(get, max_pages=2).collection_posts("To Check")
    assert len(get.paths) == 3  # the Collection list plus two pages


def test_an_unknown_collection_is_an_error_naming_it_but_not_a_login_problem():
    with pytest.raises(InstagramError, match="Nope") as info:
        InstagramApi(FakeGet(_routes())).collection_posts("Nope")
    assert not isinstance(info.value, LoginRequired)


@pytest.mark.parametrize(
    "answer",
    [(401, {}), (403, {"message": "login_required"}), (200, fixture("login_required.json"))],
)
def test_a_logged_out_answer_is_login_required(answer):
    get = FakeGet({"/api/v1/collections/list/": answer})
    with pytest.raises(LoginRequired):
        InstagramApi(get).collection_posts("To Check")


@pytest.mark.parametrize("answer", [(429, {}), (500, "<html>"), (200, "not json")])
def test_other_failures_are_plain_errors(answer):
    get = FakeGet({"/api/v1/collections/list/": answer})
    with pytest.raises(InstagramError) as info:
        InstagramApi(get).collection_posts("To Check")
    assert not isinstance(info.value, LoginRequired)


# the source


class FakeDownloads:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.urls: list[str] = []

    def __call__(self, url, dest: Path):
        self.urls.append(url)
        if url in self.fail:
            raise OSError("expired")
        dest.write_bytes(b"data:" + url.encode())


class FakeFrames:
    def __init__(self, frames=2, available=True):
        self.frames, self.available, self.calls = frames, available, 0

    def unavailable(self):
        return None if self.available else "ffmpeg is not installed"

    def __call__(self, video: Path, folder: Path):
        self.calls += 1
        out = []
        for i in range(self.frames):
            path = folder / f"frame{self.calls}-{i}.jpg"
            path.write_bytes(b"jpg")
            out.append(path)
        return out


def _posts():
    first = parse_collection_page(fixture("collection_page1.json"))[0]
    return first + parse_collection_page(fixture("collection_page2.json"))[0]


def _source(posts=None, **kwargs):
    kwargs.setdefault("download", FakeDownloads())
    kwargs.setdefault("transcriber", FakeTranscriber(text="Honey lasts forever."))
    kwargs.setdefault("frames", FakeFrames())
    return InstagramSource(lambda: posts if posts is not None else _posts(), **kwargs)


def test_the_listing_carries_only_id_and_title():
    refs = _source().list_items()
    assert [r.id for r in refs] == ["REEL000001", "POST000002", "CARO000003"]
    assert refs[0].title.startswith("Honey never spoils")
    assert refs[1].title == "POST000002"  # no caption: the shortcode stands in
    assert set(vars(refs[0])) == {"id", "title"}


def test_a_reel_is_transcribed_from_its_audio_and_the_text_names_it():
    downloads, transcriber = FakeDownloads(), FakeTranscriber(text="Honey lasts forever.")
    source = _source(download=downloads, transcriber=transcriber)
    item = source.fetch(source.list_items()[0])
    assert downloads.urls == ["https://cdn.example.test/reel1_hd.mp4"]
    assert item.text.splitlines()[0].startswith("Honey never spoils")
    assert "https://www.instagram.com/reel/REEL000001/" in item.text
    assert "@examplecreator" in item.text and "2026-01-01" in item.text
    assert "Honey never spoils, they found 3000 year old jars!" in item.text
    assert [(u.name, u.content) for u in item.attachments] == [
        ("transcript.txt", b"Honey lasts forever.")
    ]


def test_a_single_image_post_attaches_the_image_and_needs_no_transcriber():
    transcriber = FakeTranscriber(reason="ffmpeg is not installed")
    source = _source(transcriber=transcriber)
    item = source.fetch(source.list_items()[1])
    assert [u.name for u in item.attachments] == ["image-1.jpg"]
    assert "https://www.instagram.com/p/POST000002/" in item.text
    assert "read the attached image" in item.text.lower()
    assert transcriber.transcribed == []
    assert item.notes == ()


def test_a_carousel_attaches_each_image_and_transcribes_its_video_slide():
    transcriber = FakeTranscriber(text="Take vitamin D.")
    source = _source(transcriber=transcriber)
    item = source.fetch(source.list_items()[2])
    names = [u.name for u in item.attachments]
    assert names == ["image-1.jpg", "image-3.jpg", "transcript.txt"]
    assert b"Take vitamin D." in item.attachments[-1].content
    assert "carousel" in item.text.lower()


def test_a_silent_reel_attaches_frames_for_the_on_screen_text():
    frames = FakeFrames(frames=2)
    source = _source(transcriber=FakeTranscriber(text="  "), frames=frames)
    item = source.fetch(source.list_items()[0])
    assert [u.name for u in item.attachments] == ["frame-1.jpg", "frame-2.jpg"]
    assert frames.calls == 1
    assert "on-screen text" in item.text.lower()
    assert any("no speech" in n.lower() for n in item.notes)


def test_without_a_transcriber_the_reel_falls_back_to_frames_with_a_note():
    source = _source(transcriber=FakeTranscriber(reason="ffmpeg is not installed"))
    item = source.fetch(source.list_items()[0])
    assert [u.name for u in item.attachments] == ["frame-1.jpg", "frame-2.jpg"]
    assert any("transcription is not set up" in n for n in item.notes)


def test_a_reel_over_the_duration_limit_is_not_transcribed():
    transcriber = FakeTranscriber()
    source = _source(transcriber=transcriber, max_duration_seconds=10)
    item = source.fetch(source.list_items()[0])
    assert transcriber.transcribed == []
    assert any("longer than" in n for n in item.notes)


def test_a_failed_video_download_still_gives_a_caption_item():
    source = _source(download=FakeDownloads(fail={"https://cdn.example.test/reel1_hd.mp4"}))
    item = source.fetch(source.list_items()[0])
    assert "Honey never spoils" in item.text
    assert any("download failed" in n.lower() for n in item.notes)


def test_a_post_with_no_caption_whose_images_all_fail_is_a_fetch_error():
    source = _source(download=FakeDownloads(fail={"https://cdn.example.test/post2_big.jpg"}))
    with pytest.raises(OSError):
        source.fetch(source.list_items()[1])


def test_a_caption_only_post_with_no_media_still_fetches():
    post = Post("TEXT000001", "someone", "Only words here", "2026-01-01", "post", ())
    source = _source(posts=[post])
    item = source.fetch(source.list_items()[0])
    assert item.attachments == () and "Only words here" in item.text


def test_every_item_tells_the_agent_what_to_do_when_there_is_no_factual_claim():
    source = _source()
    item = source.fetch(source.list_items()[0])
    assert "no factual claim" in item.text


def test_fetching_an_id_missing_from_the_listing_is_skipped_not_failed():
    source = _source()
    with pytest.raises(ItemSkipped):
        source.fetch(InboxRef("GONE", "gone"))


# the browser reader


@contextmanager
def _session_returning(get):
    yield get


def test_the_reader_holds_the_profile_lock_while_it_reads(home):
    mark_logged_in(home)
    seen = []

    @contextmanager
    def session(profile):
        with pytest.raises(Exception):  # noqa: B017  the lock is ours, a second taker is refused
            with profile_lock(home):
                pass
        seen.append(profile)
        yield FakeGet(_routes())

    posts = browser_reader(home, "To Check", session)()
    assert [p.shortcode for p in posts][0] == "REEL000001"
    assert seen == [profile_dir(home)]
    with profile_lock(home):  # and released afterwards
        pass


def test_a_busy_profile_is_source_busy_and_never_opens_the_browser(home):
    mark_logged_in(home)
    opened = []

    @contextmanager
    def session(profile):
        opened.append(profile)
        yield FakeGet(_routes())

    with profile_lock(home), pytest.raises(SourceBusy):
        browser_reader(home, "To Check", session)()
    assert opened == []


def test_never_logged_in_is_login_required_without_opening_the_browser(home):
    opened = []

    @contextmanager
    def session(profile):
        opened.append(profile)
        yield FakeGet(_routes())

    with pytest.raises(LoginRequired):
        browser_reader(home, "To Check", session)()
    assert opened == []


def test_a_logged_out_instagram_is_login_required(home):
    mark_logged_in(home)
    reader = browser_reader(
        home,
        "To Check",
        lambda profile: _session_returning(FakeGet({"/api/v1/collections/list/": (401, {})})),
    )
    with pytest.raises(LoginRequired):
        reader()

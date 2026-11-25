"""YouTube source: playlist ids, subtitle text, and the transcript fallbacks, all offline."""

from __future__ import annotations

import pytest

from investigate.inbox import InboxRef
from investigate.youtube import (
    PlaylistError,
    VideoInfo,
    YouTubeSource,
    pick_subtitle_lang,
    playlist_url,
    vtt_to_text,
)
from tests.fake_inbox import FakeTranscriber, FakeYouTube

PLAYLIST = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"


def _source(client, transcriber=None):
    return YouTubeSource(client, transcriber or FakeTranscriber(), pause_seconds=0)


# playlist config


@pytest.mark.parametrize(
    "value",
    [
        "PLabcdefghijklmnop",
        PLAYLIST,
        "https://www.youtube.com/watch?v=xyz&list=PLabcdefghijklmnop&index=2",
        "  PLabcdefghijklmnop  ",
    ],
)
def test_playlist_id_and_urls_normalise_to_one_url(value):
    assert playlist_url(value) == PLAYLIST


@pytest.mark.parametrize(
    "value",
    [
        "My saved videos",
        "",
        "PL",
        "WL",
        "https://www.youtube.com/playlist?list=WL",
        "https://evil.example/playlist?list=PLabcdefghijklmnop",
        "ftp://youtube.com/?list=PLab",
    ],
)
def test_playlist_names_and_odd_values_are_refused(value):
    with pytest.raises(PlaylistError):
        playlist_url(value)


def test_the_error_for_a_name_says_why():
    with pytest.raises(PlaylistError, match="without logging in"):
        playlist_url("Watch later ideas")


# helpers


def test_vtt_to_text_drops_timestamps_tags_and_rolling_duplicates():
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:00.000 --> 00:00:02.000\nhello <c>there</c>\n\n"
        "00:00:02.000 --> 00:00:04.000\nhello there\nhow are you\n\n"
        "NOTE ignore\n"
    )
    assert vtt_to_text(vtt) == "hello there how are you"


def test_pick_subtitle_lang_prefers_own_language_then_manual_over_auto():
    info = {"language": "de", "subtitles": {"en": [1]}, "automatic_captions": {"de": [1]}}
    assert pick_subtitle_lang(info, ("en",)) == "en"  # manual track wins over automatic
    assert pick_subtitle_lang({"automatic_captions": {"en-orig": [1]}}, ("en",)) == "en-orig"
    assert pick_subtitle_lang({"subtitles": {"fr": [1]}}, ("en", "de")) is None
    assert pick_subtitle_lang({}, ("en",)) is None


# transcripts


def test_subtitles_come_first_and_audio_is_never_touched():
    client, transcriber = FakeYouTube(), FakeTranscriber()
    item = _source(client, transcriber).fetch(InboxRef("v1", "Video one"))

    assert [c[0] for c in client.calls] == ["video", "subtitles"]
    assert transcriber.transcribed == []
    assert item.notes == ()
    (upload,) = item.attachments
    assert (upload.name, upload.content) == ("transcript.txt", b"hello from subtitles")
    first, second, *_ = item.text.splitlines()
    assert first == "Video one" and second == "https://www.youtube.com/watch?v=v1"
    assert "Channel: Chan" in item.text and "Published: 2026-01-02" in item.text
    assert "Duration: 2:05" in item.text and "About stuff." in item.text
    assert "transcript.txt" in item.text


def test_failed_subtitles_fall_back_to_audio_transcription():
    client = FakeYouTube(subtitle_error=RuntimeError("HTTP Error 429"))
    transcriber = FakeTranscriber(text="spoken words")
    item = _source(client, transcriber).fetch(InboxRef("v1", "Video one"))

    assert [c[0] for c in client.calls] == ["video", "subtitles", "audio"]
    assert item.attachments[0].content == b"spoken words"
    assert any("429" in n for n in item.notes)
    assert "audio transcribed locally" in item.text
    assert not transcriber.transcribed[0].exists()  # the audio file is cleaned up


def test_video_without_subtitles_is_transcribed_from_audio():
    info = VideoInfo("v2", "No subs", "https://www.youtube.com/watch?v=v2")
    client = FakeYouTube({"v2": info})
    item = _source(client).fetch(InboxRef("v2", "No subs"))
    assert [c[0] for c in client.calls] == ["video", "audio"]
    assert item.attachments[0].content == b"spoken words"


def test_missing_transcription_extra_is_noted_and_the_item_still_exists():
    info = VideoInfo("v2", "No subs", "https://www.youtube.com/watch?v=v2", description="Claims.")
    client = FakeYouTube({"v2": info})
    transcriber = FakeTranscriber(reason="ffmpeg is not installed")
    item = _source(client, transcriber).fetch(InboxRef("v2", "No subs"))

    assert [c[0] for c in client.calls] == ["video"]  # no audio is downloaded for nothing
    assert item.attachments == ()
    (note,) = item.notes
    assert "ffmpeg is not installed" in note and "uv sync --extra transcribe" in note
    assert "title and description only" in note
    assert "Transcript: not available" in item.text and "Claims." in item.text


def test_transcription_crash_is_a_note_not_an_error():
    info = VideoInfo("v2", "No subs", "https://www.youtube.com/watch?v=v2")
    transcriber = FakeTranscriber(error=RuntimeError("model download failed"))
    item = _source(FakeYouTube({"v2": info}), transcriber).fetch(InboxRef("v2", "No subs"))
    assert item.attachments == ()
    assert "model download failed" in item.notes[0]


def test_unreadable_video_raises_so_the_item_stays_unprocessed():
    class Broken(FakeYouTube):
        def video(self, video_id):
            raise RuntimeError("Video unavailable")

    with pytest.raises(RuntimeError):
        _source(Broken()).fetch(InboxRef("v1", "x"))


def test_title_with_newlines_and_a_huge_description_are_tamed():
    info = VideoInfo("v3", "Line one\nline two", "u", description="x" * 10_000)
    item = _source(FakeYouTube({"v3": info})).fetch(InboxRef("v3", "t"))
    assert item.text.splitlines()[0] == "Line one line two"
    assert len(item.text) < 6_000 and "[cut]" in item.text


# the real transcriber's availability check


def test_whisper_transcriber_names_what_is_missing(monkeypatch):
    import importlib.util
    import shutil

    from investigate.transcribe import WhisperTranscriber

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    reason = WhisperTranscriber().unavailable()
    assert "ffmpeg is not installed" in reason and "faster-whisper is not installed" in reason

    monkeypatch.setattr(shutil, "which", lambda name: "/bin/ffmpeg")
    assert WhisperTranscriber().unavailable() == "faster-whisper is not installed"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert WhisperTranscriber().unavailable() is None

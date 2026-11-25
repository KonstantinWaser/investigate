"""Fakes for the Inbox: a source with canned items and a YouTube client that never uses the
network."""

from __future__ import annotations

from pathlib import Path

from investigate.inbox import InboxItem, InboxRef
from investigate.youtube import VideoInfo


class FakeSource:
    name = "fake"

    def __init__(self, titles=("A video", "B video"), fail_list=None, fail_fetch=()) -> None:
        self.refs = [InboxRef(f"id{i}", t) for i, t in enumerate(titles)]
        self.fail_list = fail_list
        self.fail_fetch = set(fail_fetch)
        self.fetched: list[str] = []

    def list_items(self):
        if self.fail_list:
            raise self.fail_list
        return list(self.refs)

    def fetch(self, ref):
        self.fetched.append(ref.id)
        if ref.id in self.fail_fetch:
            raise RuntimeError("video unavailable")
        return InboxItem(ref.id, ref.title, f"{ref.title}\nhttps://example.com/{ref.id}")


class FakeYouTube:
    """Canned playlist, one VideoInfo per id, subtitles and audio on request."""

    def __init__(self, videos=None, subtitle_text="hello from subtitles", subtitle_error=None):
        self.videos = videos or {
            "v1": VideoInfo(
                "v1",
                "Video one",
                "https://www.youtube.com/watch?v=v1",
                channel="Chan",
                upload_date="20260102",
                duration=125,
                description="About stuff.",
                subtitle_lang="en",
            )
        }
        self.subtitle_text = subtitle_text
        self.subtitle_error = subtitle_error
        self.calls: list[tuple] = []

    def playlist(self):
        self.calls.append(("playlist",))
        return [InboxRef(v.id, v.title) for v in self.videos.values()]

    def video(self, video_id):
        self.calls.append(("video", video_id))
        return self.videos[video_id]

    def subtitles(self, video_id, lang):
        self.calls.append(("subtitles", video_id, lang))
        if self.subtitle_error:
            raise self.subtitle_error
        return self.subtitle_text

    def audio(self, video_id, folder: Path):
        self.calls.append(("audio", video_id))
        path = folder / f"yt-{video_id}.m4a"
        path.write_bytes(b"audio")
        return path


class FakeTranscriber:
    def __init__(self, reason=None, text="spoken words", error=None) -> None:
        self.reason, self.text, self.error = reason, text, error
        self.transcribed: list[Path] = []

    def unavailable(self):
        return self.reason

    def transcribe(self, audio):
        self.transcribed.append(audio)
        if self.error:
            raise self.error
        return self.text

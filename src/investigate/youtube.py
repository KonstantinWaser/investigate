"""Read a YouTube playlist and fetch one video's metadata and transcript, never changing anything.

`YouTubeClient` is the seam to yt-dlp, so tests use a fake and never touch the network.
`YouTubeSource` plugs the client and a `Transcriber` into the Inbox: subtitles first, otherwise
the audio is transcribed locally, otherwise the Fact-check runs on the title and description.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from investigate.attachments import Upload
from investigate.inbox import InboxItem, InboxRef, ItemSkipped
from investigate.transcribe import HOW_TO_ENABLE, Transcriber

SOURCE_NAME = "youtube"
DEFAULT_LANGUAGES = ("en", "de")
DESCRIPTION_LIMIT = 4000
DEFAULT_MAX_DURATION_SECONDS = 90 * 60
MAX_AUDIO_BYTES = 300 * 1024 * 1024  # yt-dlp skips a larger audio file
SOCKET_TIMEOUT = 30  # seconds, for every yt-dlp network read
_NOT_READY = ("is_live", "is_upcoming", "post_live")  # yt-dlp live_status values
_PLAYLIST_ID = re.compile(r"^(PL|OL|UU|FL|LL)[A-Za-z0-9_-]*$")
_PLAYLIST_URL = "https://www.youtube.com/playlist?list={}"
_HIDDEN_TITLES = {"[private video]", "[deleted video]"}


class PlaylistError(ValueError):
    """The configured playlist is not a playlist id or URL."""


def playlist_url(value: str) -> str:
    """Normalise a playlist URL or a bare playlist id to one canonical URL.

    A playlist name is not accepted: looking one up needs a logged-in account, and the same
    name can exist twice. An id never changes when the playlist is renamed.
    """
    value = value.strip()
    if _PLAYLIST_ID.match(value) and len(value) > 2:
        return _PLAYLIST_URL.format(value)
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.netloc.lower().endswith(
        ("youtube.com", "youtu.be")
    ):
        ids = parse_qs(parts.query).get("list")
        if ids and re.fullmatch(r"[A-Za-z0-9_-]+", ids[0]) and ids[0] != "WL":
            return _PLAYLIST_URL.format(ids[0])
    raise PlaylistError(
        f"{value!r} is not a YouTube playlist id (like PLxxxxxxxx) or a playlist URL with "
        "?list=. A playlist name cannot be looked up without logging in, so use the id or URL."
    )


@dataclass(frozen=True)
class VideoInfo:
    id: str
    title: str
    url: str
    channel: str = ""
    upload_date: str = ""  # YYYYMMDD as YouTube gives it
    duration: int | None = None  # seconds
    description: str = ""
    subtitle_lang: str | None = None  # the track to fetch, when the video has a usable one
    live_status: str | None = None  # yt-dlp: is_live, is_upcoming, post_live, was_live, not_live


class YouTubeClient(Protocol):
    def playlist(self) -> list[InboxRef]: ...

    def video(self, video_id: str) -> VideoInfo: ...

    def subtitles(self, video_id: str, lang: str) -> str:
        """The plain text of the subtitle track `lang`."""
        ...

    def audio(self, video_id: str, folder: Path) -> Path:
        """Download the audio into `folder` and return the file."""
        ...


def vtt_to_text(vtt: str) -> str:
    """Drop WebVTT headers, timestamps and tags, and collapse the repeated lines automatic
    captions produce."""
    out: list[str] = []
    for line in vtt.splitlines():
        if (
            not line.strip()
            or "-->" in line
            or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
        ):
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text and (not out or out[-1] != text):
            out.append(text)
    return " ".join(out)


def pick_subtitle_lang(info: dict, preferred: tuple[str, ...]) -> str | None:
    """A manual track beats an automatic one; the video's own language beats the preferred list."""
    own = info.get("language")
    order = ([own] if isinstance(own, str) and own else []) + [
        lang for p in preferred for lang in (p, f"{p}-orig")
    ]
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        for lang in order:
            if tracks.get(lang):
                return lang
    return None


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


class YtDlpClient:
    """The real client. `cookies` is a Netscape cookies.txt for private playlists."""

    def __init__(
        self,
        playlist: str,
        cookies: Path | None = None,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
    ) -> None:
        self._playlist = playlist
        self._cookies = cookies
        self._languages = languages

    @contextlib.contextmanager
    def _ydl(self, **extra) -> Iterator:
        """A YoutubeDL. yt-dlp rewrites its cookie file on close, so it gets a copy that is
        deleted afterwards and the user's cookies.txt is never touched."""
        from yt_dlp import YoutubeDL

        options = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": SOCKET_TIMEOUT,
            **extra,
        }
        with TemporaryDirectory() as tmp:
            if self._cookies is not None:
                copy = Path(tmp) / "cookies.txt"
                shutil.copyfile(self._cookies, copy)  # a missing file raises, never anonymous
                options["cookiefile"] = str(copy)
            with YoutubeDL(options) as ydl:
                yield ydl

    def playlist(self) -> list[InboxRef]:
        with self._ydl(extract_flat="in_playlist") as ydl:
            info = ydl.extract_info(self._playlist, download=False)
        refs = []
        for entry in (info or {}).get("entries") or []:
            video_id = (entry or {}).get("id")
            title = (entry or {}).get("title") or ""
            if video_id and title.strip().lower() not in _HIDDEN_TITLES:
                refs.append(InboxRef(video_id, title))
        return refs

    def video(self, video_id: str) -> VideoInfo:
        # A premiere or scheduled stream has no formats yet; without this flag yt-dlp raises
        # before `live_status` can be read, and the video would count as unfetchable.
        with self._ydl(ignore_no_formats_error=True) as ydl:
            info = ydl.extract_info(_watch_url(video_id), download=False)
        if not info.get("formats") and _live_status(info) not in _NOT_READY:
            # The flag also silences private, removed, age-gated and rate-limited videos;
            # those must stay fetch failures so they are retried and given up on as before.
            raise RuntimeError(
                "YouTube returned no playable formats (private, removed, sign-in required "
                "or rate-limited)"
            )
        return VideoInfo(
            id=video_id,
            title=info.get("title") or video_id,
            url=info.get("webpage_url") or _watch_url(video_id),
            channel=info.get("channel") or info.get("uploader") or "",
            upload_date=info.get("upload_date") or "",
            duration=info.get("duration"),
            description=info.get("description") or "",
            subtitle_lang=pick_subtitle_lang(info, self._languages),
            live_status=_live_status(info),
        )

    def subtitles(self, video_id: str, lang: str) -> str:
        with TemporaryDirectory() as tmp:
            extra = dict(
                skip_download=True,
                writesubtitles=True,
                writeautomaticsub=True,
                subtitleslangs=[lang],
                subtitlesformat="vtt",
                outtmpl=str(Path(tmp) / "yt-%(id)s.%(ext)s"),
            )
            with self._ydl(**extra) as ydl:
                ydl.download([_watch_url(video_id)])
            found = sorted(Path(tmp).glob("*.vtt"))
            if not found:
                raise RuntimeError(f"no {lang} subtitle file was written")
            return vtt_to_text(found[0].read_text(encoding="utf-8", errors="replace"))

    def audio(self, video_id: str, folder: Path) -> Path:
        target = folder / f"yt-{video_id}.%(ext)s"
        options = {
            "format": "ba[ext=m4a]/ba",
            "outtmpl": str(target),
            "max_filesize": MAX_AUDIO_BYTES,
        }
        with self._ydl(**options) as ydl:
            ydl.download([_watch_url(video_id)])
        found = sorted(folder.glob(f"yt-{video_id}.*"))
        if not found:
            raise RuntimeError("no audio file was written (it may be over the size limit)")
        return found[0]


def _live_status(info: dict) -> str | None:
    status = info.get("live_status")
    if isinstance(status, str) and status:
        return status
    return "is_live" if info.get("is_live") else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def item_text(info: VideoInfo, transcript_line: str) -> str:
    """The Item text. The first line is the title, so folder names and History show it."""
    lines = [_one_line(info.title) or info.id, info.url, ""]
    if info.channel:
        lines.append(f"Channel: {_one_line(info.channel)}")
    if len(info.upload_date) == 8 and info.upload_date.isdigit():
        d = info.upload_date
        lines.append(f"Published: {d[:4]}-{d[4:6]}-{d[6:]}")
    if info.duration:
        lines.append(f"Duration: {_duration(info.duration)}")
    description = info.description.strip()
    if description:
        if len(description) > DESCRIPTION_LIMIT:
            description = description[:DESCRIPTION_LIMIT].rstrip() + " [cut]"
        lines += ["", "Description:", description]
    lines += ["", transcript_line]
    return "\n".join(lines)


class YouTubeSource:
    name = SOURCE_NAME

    def __init__(
        self,
        client: YouTubeClient,
        transcriber: Transcriber,
        *,
        pause_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        max_duration_seconds: int | None = DEFAULT_MAX_DURATION_SECONDS,
    ) -> None:
        self._client, self._transcriber = client, transcriber
        self._max_duration = max_duration_seconds
        self._pause, self._sleep = pause_seconds, sleep

    def list_items(self) -> list[InboxRef]:
        return self._client.playlist()

    def fetch(self, ref: InboxRef) -> InboxItem:
        """Metadata, then a transcript from subtitles, else from audio, else none (with a note).

        A live or upcoming video is skipped (`ItemSkipped`) and looked at again at the next check.
        A video over the duration limit still uses its subtitles but is never downloaded and
        transcribed. Only a failure to read the video itself raises; a missing transcript never
        does.
        """
        info = self._client.video(ref.id)
        self._sleep(self._pause)  # be gentle with YouTube between requests
        if info.live_status in _NOT_READY:
            what = info.live_status.removeprefix("is_").replace("_", " ")
            raise ItemSkipped(f"{what}; it is checked once it has ended")
        notes: list[str] = []
        transcript, how = self._transcript(info, notes)
        if transcript:
            line = f"Transcript: attached as transcript.txt ({how})."
            attachments = (Upload("transcript.txt", transcript.encode("utf-8")),)
        else:
            line = "Transcript: not available. Check only what the title and description claim."
            attachments = ()
        return InboxItem(ref.id, info.title, item_text(info, line), attachments, tuple(notes))

    def _transcript(self, info: VideoInfo, notes: list[str]) -> tuple[str, str]:
        if info.subtitle_lang:
            try:
                text = self._client.subtitles(info.id, info.subtitle_lang)
                if text.strip():
                    return text, f"subtitles, {info.subtitle_lang}"
                notes.append("The subtitle track was empty.")
            except Exception as exc:  # fall back to audio: YouTube often rate-limits subtitles
                notes.append(f"Subtitles could not be fetched ({_one_line(str(exc))[:200]}).")
        if self._max_duration and info.duration and info.duration > self._max_duration:
            notes.append(
                f"No transcript: the video ({_duration(info.duration)}) is longer than the limit "
                f"of {self._max_duration // 60} minutes for audio transcription and has no usable "
                "subtitles. This Fact-check ran on the title and description only."
            )
            return "", ""
        reason = self._transcriber.unavailable()
        if reason:
            notes.append(
                "No transcript: the video has no usable subtitles and audio transcription is "
                f"not set up ({reason}). {HOW_TO_ENABLE} This Fact-check ran on the title and "
                "description only."
            )
            return "", ""
        try:
            with TemporaryDirectory() as tmp:
                audio = self._client.audio(info.id, Path(tmp))
                text = self._transcriber.transcribe(audio)
        except Exception as exc:
            notes.append(
                f"No transcript: audio transcription failed ({_one_line(str(exc))[:200]}). "
                "This Fact-check ran on the title and description only."
            )
            return "", ""
        if not text.strip():
            notes.append("No transcript: the audio held no recognisable speech.")
            return "", ""
        return text, "audio transcribed locally"

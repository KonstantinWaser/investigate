"""The Inbox pipeline: sources list saved items, unseen ones become Fact-check Runs.

An `InboxSource` lists items and fetches one (a YouTube playlist today, an Instagram Collection
later). `check_inbox` asks each source, skips ids it has processed before, fetches the new ones and
hands each to an enqueuer that creates the Fact-check Run. An id is recorded as processed only
after that Run exists, so a crash loses nothing (at worst one item is checked twice). Nothing is
ever changed at the source. Processed ids live under `<data folder>/inbox/`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Protocol

from investigate.attachments import Upload
from investigate.config import ConfigError, Depth, config_path
from investigate.profile_lock import _try_lock, _unlock

INBOX_DIR = "inbox"
LOCK_NAME = "inbox.lock"
STATE_NAME = "state.json"
DEFAULT_CHECK_TIMES = (time(8, 0), time(18, 0))
DEFAULT_RETRY_MINUTES = 10


@dataclass(frozen=True)
class InboxRef:
    """One entry of a source's listing: a stable id and a title for messages."""

    id: str
    title: str


@dataclass(frozen=True)
class InboxItem:
    """A fetched item, ready to become a Fact-check."""

    id: str
    title: str
    text: str  # the Item text; its first line should be the title
    attachments: tuple[Upload, ...] = ()
    notes: tuple[str, ...] = ()  # shown in the Report and the Run log (a missing transcript)


class InboxSource(Protocol):
    name: str  # a short stable key; names the processed-ids file

    def list_items(self) -> list[InboxRef]: ...

    def fetch(self, ref: InboxRef) -> InboxItem: ...


Enqueuer = Callable[[InboxItem], str]  # creates the Fact-check Run and returns its id


class InboxBusy(RuntimeError):
    """Another inbox check holds the lock."""


@dataclass(frozen=True)
class CreatedCheck:
    source: str
    run_id: str
    title: str


@dataclass
class InboxResult:
    created: list[CreatedCheck] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    already_processed: int = 0
    busy: bool = False  # another check was running; nothing was done
    unreachable: bool = False  # a source's list could not be read (offline?): retry soon

    @property
    def ok(self) -> bool:
        return not self.errors and not self.busy


# configuration


@dataclass(frozen=True)
class InboxConfig:
    check_times: tuple[time, ...] = DEFAULT_CHECK_TIMES
    depth: Depth = Depth.STANDARD
    playlist: str | None = None  # canonical playlist URL, None when not configured
    cookies: Path | None = None  # Netscape cookies.txt, for a private playlist
    subtitle_languages: tuple[str, ...] = ("en", "de")

    @property
    def configured(self) -> bool:
        return self.playlist is not None


def _parse_time(value: object, path: Path) -> time:
    try:
        hour, minute = str(value).split(":")
        return time(int(hour), int(minute))
    except ValueError:
        raise ConfigError(f"{path}: inbox.check_times entries must look like '08:00'") from None


def load_inbox_config(home: Path) -> InboxConfig:
    """The `[inbox]` section of config.toml. Absent means defaults and no playlist."""
    from investigate.youtube import PlaylistError, playlist_url

    path = config_path(home)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    section = raw.get("inbox", {})
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: inbox must be a table")

    times = section.get("check_times")
    if times is None:
        check_times = DEFAULT_CHECK_TIMES
    elif isinstance(times, list) and times:
        check_times = tuple(sorted({_parse_time(t, path) for t in times}))
    else:
        raise ConfigError(f"{path}: inbox.check_times must be a non-empty list like ['08:00']")

    try:
        depth = Depth(section.get("depth", "standard"))
    except ValueError:
        raise ConfigError(f"{path}: inbox.depth must be quick, standard or deep") from None

    youtube = section.get("youtube", {})
    if not isinstance(youtube, dict):
        raise ConfigError(f"{path}: inbox.youtube must be a table")
    playlist = youtube.get("playlist")
    if playlist is not None:
        if not isinstance(playlist, str) or not playlist.strip():
            raise ConfigError(f"{path}: inbox.youtube.playlist must be a playlist id or URL")
        try:
            playlist = playlist_url(playlist)
        except PlaylistError as exc:
            raise ConfigError(f"{path}: inbox.youtube.playlist: {exc}") from None
    cookies = youtube.get("cookies")
    cookie_path = None
    if cookies is not None:
        if not isinstance(cookies, str) or not cookies.strip():
            raise ConfigError(f"{path}: inbox.youtube.cookies must be a path to a cookies.txt")
        expanded = Path(cookies).expanduser()
        cookie_path = (expanded if expanded.is_absolute() else path.parent / expanded).resolve()
    languages = youtube.get("subtitle_languages", ["en", "de"])
    if not (
        isinstance(languages, list)
        and languages
        and all(isinstance(lang, str) and lang.strip() for lang in languages)
    ):
        raise ConfigError(f"{path}: inbox.youtube.subtitle_languages must be a list like ['en']")
    return InboxConfig(
        check_times, depth, playlist, cookie_path, tuple(lang.strip() for lang in languages)
    )


def default_sources(config: InboxConfig) -> list[InboxSource]:
    """The sources the config enables. The imports are lazy: yt-dlp loads only when used."""
    from investigate.transcribe import WhisperTranscriber
    from investigate.youtube import YouTubeSource, YtDlpClient

    sources: list[InboxSource] = []
    if config.playlist:
        client = YtDlpClient(config.playlist, config.cookies, config.subtitle_languages)
        sources.append(YouTubeSource(client, WhisperTranscriber()))
    return sources


# state on disk


def _dir(home: Path) -> Path:
    return home / INBOX_DIR


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class ProcessedIds:
    """The ids a source has already turned into Fact-checks, one file per source."""

    def __init__(self, home: Path, source: str) -> None:
        self._path = _dir(home) / f"{source}.json"

    def _load(self) -> dict:
        processed = _read_json(self._path).get("processed", {})
        return processed if isinstance(processed, dict) else {}

    def ids(self) -> set[str]:
        return set(self._load())

    def add(self, item_id: str, run_id: str, now: datetime | None = None) -> None:
        processed = self._load()
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        processed[item_id] = {"run": run_id, "at": stamp}
        _write_json(self._path, {"processed": processed})


class InboxState:
    """When the inbox was last checked, so a missed slot can be caught up at start-up."""

    def __init__(self, home: Path) -> None:
        self._path = _dir(home) / STATE_NAME

    def last_check(self) -> datetime | None:
        value = _read_json(self._path).get("last_check")
        try:
            moment = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None
        if moment is not None and moment.tzinfo is None:
            moment = moment.astimezone()
        return moment

    def record(self, when: datetime) -> None:
        data = _read_json(self._path)
        data["last_check"] = when.isoformat(timespec="seconds")
        _write_json(self._path, data)


@contextlib.contextmanager
def inbox_lock(home: Path) -> Iterator[None]:
    """One inbox check at a time per data folder, across processes (the tray and the CLI)."""
    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / LOCK_NAME, "a+b")  # noqa: SIM115
    try:
        if not _try_lock(handle):
            raise InboxBusy("another inbox check is running")
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


# the check


def check_inbox(
    home: Path,
    sources: Sequence[InboxSource],
    enqueue: Enqueuer,
    *,
    stop: Callable[[], bool] = lambda: False,
) -> InboxResult:
    """Turn every unseen item of every source into a Fact-check Run.

    A source or item that fails is reported in `errors` and left unprocessed, so the next check
    tries it again. `stop` ends the check early (the app is quitting).
    """
    result = InboxResult()
    try:
        with inbox_lock(home):
            for source in sources:
                _check_source(home, source, enqueue, stop, result)
    except InboxBusy:
        result.busy = True
    return result


def _check_source(
    home: Path, source: InboxSource, enqueue: Enqueuer, stop: Callable[[], bool], out: InboxResult
) -> None:
    seen = ProcessedIds(home, source.name)
    try:
        refs = source.list_items()
    except Exception as exc:
        out.errors.append(f"{source.name}: could not read the list: {_brief(exc)}")
        out.unreachable = True
        return
    done = seen.ids()
    for ref in refs:
        if ref.id in done:
            out.already_processed += 1
            continue
        if stop():
            return
        try:
            item = source.fetch(ref)
        except Exception as exc:
            out.errors.append(f"{source.name}: {ref.title or ref.id}: {_brief(exc)}")
            continue
        try:
            run_id = enqueue(item)
        except Exception as exc:  # the queue is closed or the disk is full: stop, keep the rest
            out.errors.append(f"{source.name}: {ref.title or ref.id}: not queued: {_brief(exc)}")
            return
        seen.add(ref.id, run_id)  # only now: the Run exists
        done.add(ref.id)
        out.created.append(CreatedCheck(source.name, run_id, item.title))


def _brief(exc: Exception) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:300]

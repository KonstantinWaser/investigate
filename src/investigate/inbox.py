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
import tomllib
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Protocol

from investigate import history
from investigate.attachments import Upload
from investigate.config import ConfigError, Depth, config_path
from investigate.history import _replace
from investigate.profile_lock import _try_lock, _unlock

INBOX_DIR = "inbox"
LOCK_NAME = "inbox.lock"
STATE_NAME = "state.json"
DEFAULT_CHECK_TIMES = (time(8, 0), time(18, 0))
DEFAULT_RETRY_MINUTES = 10
DEFAULT_MAX_ATTEMPTS = 3  # tries per item (a fetch, or a failed Run) before giving up
DEFAULT_MAX_NEW = 10  # new items taken per check; the rest wait for the next one
DEFAULT_MAX_DURATION_MINUTES = 90  # longer videos are checked on title and description
LOG_NAME = "inbox.log"
NOTICES_NAME = "notices.json"
DEFAULT_INSTAGRAM_MAX_MINUTES = 10  # longer reels are checked on caption and frames


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


class ItemSkipped(Exception):  # noqa: N818
    """An item that is not ready yet (a live stream, say). It is not an error and not counted
    as a failed attempt; the next check looks at it again."""


class LoginRequired(Exception):  # noqa: N818
    """A source is logged out. An outage of that source only: it is reported and shown as a
    History notice, and the other sources still run."""


class SourceBusy(Exception):  # noqa: N818
    """A source cannot be read right now (the browser profile is in use). It is skipped without
    counting an attempt and the check is retried later."""


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
    skipped: list[str] = field(default_factory=list)  # not ready yet, looked at again later
    gave_up: list[str] = field(default_factory=list)  # out of attempts, said once
    deferred: int = 0  # items left for the next check by the per-check cap
    login_required: list[str] = field(default_factory=list)  # logged-out sources
    source_busy: list[str] = field(default_factory=list)  # skipped, to be retried

    @property
    def ok(self) -> bool:
        return not (self.errors or self.busy or self.login_required)


# configuration


@dataclass(frozen=True)
class InboxConfig:
    check_times: tuple[time, ...] = DEFAULT_CHECK_TIMES
    depth: Depth = Depth.STANDARD
    playlist: str | None = None  # canonical playlist URL, None when not configured
    cookies: Path | None = None  # Netscape cookies.txt, for a private playlist
    subtitle_languages: tuple[str, ...] = ("en", "de")
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_new_per_check: int = DEFAULT_MAX_NEW
    max_duration_minutes: int = DEFAULT_MAX_DURATION_MINUTES
    instagram_collection: str | None = None  # name of the saved Collection, None: off
    instagram_max_duration_minutes: int = DEFAULT_INSTAGRAM_MAX_MINUTES

    @property
    def configured(self) -> bool:
        return self.playlist is not None or self.instagram_collection is not None


def _parse_time(value: object, path: Path) -> time:
    try:
        hour, minute = str(value).split(":")
        return time(int(hour), int(minute))
    except ValueError:
        raise ConfigError(f"{path}: inbox.check_times entries must look like '08:00'") from None


def _positive_int(section: dict, key: str, default: int, path: Path, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{path}: {where}.{key} must be a whole number of 1 or more")
    return value


def load_inbox_config(home: Path) -> InboxConfig:
    """The `[inbox]` section of config.toml. Absent means defaults and no playlist."""
    path = config_path(home)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_inbox_config(raw, path)


def parse_inbox_config(raw: dict, path: Path) -> InboxConfig:
    """The rules for `[inbox]`, shared by `load_inbox_config` and the setup page. Raises
    ConfigError, its message starting with `path`."""
    from investigate.youtube import PlaylistError, playlist_url

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
    instagram = section.get("instagram", {})
    if not isinstance(instagram, dict):
        raise ConfigError(f"{path}: inbox.instagram must be a table")
    collection = instagram.get("collection")
    if collection is not None:
        if not isinstance(collection, str) or not collection.strip():
            raise ConfigError(f"{path}: inbox.instagram.collection must be the Collection's name")
        collection = collection.strip()
    return InboxConfig(
        check_times,
        depth,
        playlist,
        cookie_path,
        tuple(lang.strip() for lang in languages),
        max_attempts=_positive_int(section, "max_attempts", DEFAULT_MAX_ATTEMPTS, path, "inbox"),
        max_new_per_check=_positive_int(
            section, "max_new_per_check", DEFAULT_MAX_NEW, path, "inbox"
        ),
        max_duration_minutes=_positive_int(
            youtube, "max_duration_minutes", DEFAULT_MAX_DURATION_MINUTES, path, "inbox.youtube"
        ),
        instagram_collection=collection,
        instagram_max_duration_minutes=_positive_int(
            instagram,
            "max_duration_minutes",
            DEFAULT_INSTAGRAM_MAX_MINUTES,
            path,
            "inbox.instagram",
        ),
    )


def default_sources(config: InboxConfig, home: Path) -> list[InboxSource]:
    """The sources the config enables. The imports are lazy: yt-dlp loads only when used."""
    from investigate.transcribe import WhisperTranscriber
    from investigate.youtube import YouTubeSource, YtDlpClient

    sources: list[InboxSource] = []
    # Instagram first: it holds the profile lock only while listing, so it is free again
    # before any Run of this check (which may take the lock) starts.
    if config.instagram_collection:
        from investigate.instagram import InstagramSource, browser_reader

        sources.append(
            InstagramSource(
                browser_reader(home, config.instagram_collection),
                transcriber=WhisperTranscriber(),
                max_duration_seconds=config.instagram_max_duration_minutes * 60,
            )
        )
    if config.playlist:
        client = YtDlpClient(config.playlist, config.cookies, config.subtitle_languages)
        limit = config.max_duration_minutes * 60
        sources.append(YouTubeSource(client, WhisperTranscriber(), max_duration_seconds=limit))
    return sources


# state on disk


def _dir(home: Path) -> Path:
    return home / INBOX_DIR


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class ProcessedIds:
    """What a source has turned into Fact-checks, one file per source.

    `processed` maps an item id to its latest Run (`run`), when (`at`), how many Runs it has had
    (`attempts`) and whether it was given up on (`gave_up`). `failing` counts failed fetches per
    item id, so an item that never fetches is not retried forever.
    """

    def __init__(self, home: Path, source: str) -> None:
        self._path = _dir(home) / f"{source}.json"

    def _section(self, key: str) -> dict:
        value = _read_json(self._path).get(key, {})
        return value if isinstance(value, dict) else {}

    def _save(self, **sections: dict) -> None:
        data = _read_json(self._path)
        data.update(sections)
        _write_json(self._path, data)

    def ids(self) -> set[str]:
        return set(self._section("processed"))

    def entry(self, item_id: str) -> dict:
        found = self._section("processed").get(item_id)
        return found if isinstance(found, dict) else {}

    def add(self, item_id: str, run_id: str, now: datetime | None = None) -> None:
        """Record a new Run for the item; each call is one attempt."""
        processed = self._section("processed")
        previous = processed.get(item_id)
        attempts = int(previous.get("attempts", 1)) if isinstance(previous, dict) else 0
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        processed[item_id] = {"run": run_id, "at": stamp, "attempts": attempts + 1}
        failing = self._section("failing")
        failing.pop(item_id, None)
        self._save(processed=processed, failing=failing)

    def mark_gave_up(self, item_id: str) -> None:
        processed = self._section("processed")
        if isinstance(processed.get(item_id), dict):
            processed[item_id]["gave_up"] = True
            self._save(processed=processed)

    def fetch_failures(self, item_id: str) -> dict:
        found = self._section("failing").get(item_id)
        return found if isinstance(found, dict) else {}

    def record_fetch_failure(self, item_id: str, title: str) -> int:
        """Count one more failed fetch and return the new count."""
        failing = self._section("failing")
        previous = failing.get(item_id)
        count = int(previous.get("count", 0)) if isinstance(previous, dict) else 0
        failing[item_id] = {"count": count + 1, "title": title}
        self._save(failing=failing)
        return count + 1

    def mark_fetch_given_up(self, item_id: str) -> None:
        failing = self._section("failing")
        if isinstance(failing.get(item_id), dict):
            failing[item_id]["gave_up"] = True
            self._save(failing=failing)


class Notices:
    """What the History page shows above the list: one line per source that is in trouble and
    needs the user (a logged-out Instagram). Set by a check, cleared when the source works."""

    def __init__(self, home: Path) -> None:
        self._path = _dir(home) / NOTICES_NAME

    def all(self) -> dict[str, str]:
        data = _read_json(self._path)
        return {str(k): v for k, v in data.items() if isinstance(v, str)}

    def set(self, source: str, text: str) -> None:
        current = self.all()
        if current.get(source) != text:
            current[source] = text
            _write_json(self._path, current)

    def clear(self, source: str) -> None:
        current = self.all()
        if current.pop(source, None) is not None:
            _write_json(self._path, current)


def configured_source_names(config: InboxConfig) -> set[str]:
    names = set()
    if config.playlist:
        names.add("youtube")
    if config.instagram_collection:
        names.add("instagram")
    return names


def active_notices(home: Path) -> dict[str, str]:
    """The notices of sources that are still configured. A notice of a source that was removed
    from config.toml is deleted. An unreadable config shows every notice and deletes none."""
    notices = Notices(home)
    try:
        wanted = configured_source_names(load_inbox_config(home))
    except ConfigError:
        return notices.all()
    for source in set(notices.all()) - wanted:
        notices.clear(source)
    return notices.all()


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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_new: int = DEFAULT_MAX_NEW,
) -> InboxResult:
    """Turn every unseen item of every source into a Fact-check Run.

    A source or item that fails is reported in `errors` and left unprocessed, so the next check
    tries it again, up to `max_attempts` times (then it is given up on, logged, and skipped). A
    Run that failed or was cancelled without a Report is queued again under the same limit.
    At most `max_new` Runs are created per check; the rest wait for the next one. `stop` ends the
    check early (the app is quitting).
    """
    result = InboxResult()
    budget = [max_new]
    try:
        with inbox_lock(home):
            for source in sources:
                _check_source(home, source, enqueue, stop, result, max_attempts, budget)
    except InboxBusy:
        result.busy = True
    return result


def _log(home: Path, line: str) -> None:
    """One line in `inbox/inbox.log`, for what the toasts do not say."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        path = _dir(home) / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")
    except OSError:
        pass


def _needs_new_run(home: Path, entry: dict, runs: dict[str, str]) -> bool:
    """True when the item's latest Run failed or was cancelled and left no Report.

    A Run missing from the History was deleted on purpose and is not redone. Queued and running
    Runs are left alone, and so is a Run that finished (even a failed one that wrote a Report).
    """
    run_id = entry.get("run")
    if not isinstance(run_id, str) or runs.get(run_id) != "failed":
        return False
    return not (home / "runs" / run_id / "report.html").is_file()


def _check_source(
    home: Path,
    source: InboxSource,
    enqueue: Enqueuer,
    stop: Callable[[], bool],
    out: InboxResult,
    max_attempts: int,
    budget: list[int],
) -> None:
    seen = ProcessedIds(home, source.name)
    notices = Notices(home)
    try:
        refs = source.list_items()
    except LoginRequired as exc:
        message = f"{source.name}: {_brief_text(exc)}"
        out.login_required.append(message)
        notices.set(source.name, _brief_text(exc))
        return
    except SourceBusy as exc:
        out.source_busy.append(f"{source.name}: {_brief_text(exc)}")
        return
    except Exception as exc:
        out.errors.append(f"{source.name}: could not read the list: {_brief(exc)}")
        out.unreachable = True
        return
    notices.clear(source.name)
    runs = {e.id: e.status for e in history.load_entries(home)}
    for ref in refs:
        label = ref.title or ref.id
        entry = seen.entry(ref.id)
        if entry:
            if not _needs_new_run(home, entry, runs):
                out.already_processed += 1
                continue
            if int(entry.get("attempts", 1)) >= max_attempts:
                out.already_processed += 1
                if not entry.get("gave_up"):
                    seen.mark_gave_up(ref.id)
                    message = f"{source.name}: {label}: the Fact-check failed {max_attempts} times"
                    out.gave_up.append(message)
                    _log(home, f"giving up on {message}; the failed Runs stay in the History")
                continue
        elif seen.fetch_failures(ref.id).get("gave_up"):
            continue
        if stop():
            return
        if budget[0] <= 0:
            out.deferred += 1
            continue
        try:
            item = source.fetch(ref)
        except ItemSkipped as exc:
            out.skipped.append(f"{source.name}: {label}: {exc}")
            continue
        except Exception as exc:
            out.errors.append(f"{source.name}: {label}: {_brief(exc)}")
            count = seen.record_fetch_failure(ref.id, label)
            if count >= max_attempts:
                seen.mark_fetch_given_up(ref.id)
                message = f"{source.name}: {label}: could not be fetched {count} times"
                out.gave_up.append(message)
                _log(home, f"giving up on {message}: {_brief(exc)}")
            continue
        try:
            run_id = enqueue(item)
        except Exception as exc:  # the queue is closed or the disk is full: stop, keep the rest
            out.errors.append(f"{source.name}: {label}: not queued: {_brief(exc)}")
            return
        seen.add(ref.id, run_id)  # only now: the Run exists
        budget[0] -= 1
        out.created.append(CreatedCheck(source.name, run_id, item.title))


def _brief_text(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]


def _brief(exc: Exception) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:300]

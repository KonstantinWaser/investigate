"""Read a saved Instagram Collection through the logged-in browser profile, never changing it.

There is no official API for saved Collections and yt-dlp cannot read them, so the persistent
browser profile of `investigate login` is the only route. While the profile lock is held, a
Chrome on that profile asks Instagram's own web endpoints (plain GET requests, the ones the site
itself calls) for the Collection list and its posts. Nothing is ever saved, unsaved, liked,
followed or commented.

The seams: `InstagramApi` takes a `get_json(path)` function, so parsing and paging are tested on
saved fixtures; `browser_reader` opens the browser through an injectable session; and
`InstagramSource` takes the downloader, the `Transcriber` and the frame extractor as arguments.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from investigate.attachments import Upload
from investigate.browser import is_logged_in, profile_dir, profile_in_use
from investigate.inbox import InboxItem, InboxRef, ItemSkipped, LoginRequired, SourceBusy
from investigate.profile_lock import ProfileBusy, profile_lock
from investigate.transcribe import HOW_TO_ENABLE, Transcriber

SOURCE_NAME = "instagram"
BASE_URL = "https://www.instagram.com"
WEB_APP_ID = "936619743392459"  # the id Instagram's own web client sends
DEFAULT_MAX_PAGES = 25  # about 12 posts a page, newest first
DEFAULT_MAX_DURATION_SECONDS = 10 * 60
MAX_IMAGES = 10  # attached per post
MAX_VIDEOS = 3  # transcribed per post
MAX_FRAMES = 4  # per silent video
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
DOWNLOAD_TIMEOUT = 60
FFMPEG_TIMEOUT_SECONDS = 120
CAPTION_LIMIT = 4000
TITLE_LIMIT = 80
LOGIN_HINT = "Run `investigate login` and sign in to Instagram."
_LOGGED_OUT_STATUS = (401, 403)
_URL_PARTS = {"reel": "reel", "post": "p", "carousel": "p"}


class InstagramError(RuntimeError):
    """Instagram answered with something that is not a usable Collection."""


@dataclass(frozen=True)
class Media:
    kind: str  # "image" or "video"
    url: str
    duration: float | None  # seconds, videos only


@dataclass(frozen=True)
class Post:
    shortcode: str
    author: str
    caption: str
    taken_at: str  # YYYY-MM-DD, or empty
    kind: str  # "reel", "post" or "carousel"
    media: tuple[Media, ...]


# parsing (pure, tested on saved fixtures)


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def find_collection(data: Any, name: str) -> str | None:
    """The id of the Collection called `name` (case and spacing ignored), or None."""
    items = data.get("items") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and _norm(str(item.get("collection_name", ""))) == _norm(name):
            found = item.get("collection_id")
            return str(found) if found else None
    return None


def _best(candidates: Any) -> str:
    """The widest image candidate's URL."""
    best, width = "", -1
    for c in candidates if isinstance(candidates, list) else []:
        if isinstance(c, dict) and isinstance(c.get("url"), str):
            w = c.get("width") if isinstance(c.get("width"), int) else 0
            if w > width:
                best, width = c["url"], w
    return best


def _first_url(versions: Any) -> str:
    for v in versions if isinstance(versions, list) else []:
        if isinstance(v, dict) and isinstance(v.get("url"), str):
            return v["url"]
    return ""


def _media(node: Any) -> Media | None:
    if not isinstance(node, dict):
        return None
    if node.get("media_type") == 2:
        url = _first_url(node.get("video_versions"))
        duration = node.get("video_duration")
        if url:
            ok = isinstance(duration, int | float) and not isinstance(duration, bool)
            return Media("video", url, float(duration) if ok else None)
    versions = node.get("image_versions2")
    url = _best(versions.get("candidates")) if isinstance(versions, dict) else ""
    return Media("image", url, None) if url else None


def _post(node: Any) -> Post | None:
    if not isinstance(node, dict) or not isinstance(node.get("code"), str) or not node["code"]:
        return None
    media_type = node.get("media_type")
    if media_type == 8:
        parts = [_media(m) for m in node.get("carousel_media") or [] if isinstance(m, dict)]
        media = tuple(m for m in parts if m)
        kind = "carousel"
    else:
        single = _media(node)
        media = (single,) if single else ()
        kind = "reel" if media_type == 2 else "post"
    caption = node.get("caption")
    text = caption.get("text") if isinstance(caption, dict) else ""
    user = node.get("user")
    author = user.get("username") if isinstance(user, dict) else ""
    taken = node.get("taken_at")
    day = ""
    if isinstance(taken, int | float) and not isinstance(taken, bool):
        try:
            day = datetime.fromtimestamp(taken, UTC).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            day = ""
    return Post(
        node["code"],
        author if isinstance(author, str) else "",
        text if isinstance(text, str) else "",
        day,
        kind,
        media,
    )


def parse_collection_page(data: Any) -> tuple[list[Post], str | None]:
    """The posts of one page of a Collection and the cursor of the next page (None at the end).
    Anything that is not a post (an ad, a malformed entry) is dropped; this never raises."""
    if not isinstance(data, dict):
        return [], None
    entries = data.get("items")
    posts = []
    for entry in entries if isinstance(entries, list) else []:
        post = _post(entry.get("media")) if isinstance(entry, dict) else None
        if post:
            posts.append(post)
    cursor = data.get("next_max_id")
    more = data.get("more_available") is True and isinstance(cursor, str) and cursor
    return posts, (cursor if more else None)


# the API


GetJson = Callable[[str], "tuple[int, Any]"]  # path -> (HTTP status, parsed JSON or raw text)


class InstagramApi:
    def __init__(self, get_json: GetJson, max_pages: int = DEFAULT_MAX_PAGES) -> None:
        self._get, self._max_pages = get_json, max_pages

    def _json(self, path: str) -> dict:
        status, data = self._get(path)
        as_dict = data if isinstance(data, dict) else {}
        if (
            status in _LOGGED_OUT_STATUS
            or as_dict.get("message") == "login_required"
            or as_dict.get("require_login")
        ):
            raise LoginRequired(f"Instagram is logged out. {LOGIN_HINT}")
        if status != 200 or not isinstance(data, dict):
            raise InstagramError(f"Instagram answered {status} for {path.split('?')[0]}")
        return data

    def collection_posts(self, name: str) -> list[Post]:
        collection = find_collection(self._json("/api/v1/collections/list/"), name)
        if collection is None:
            raise InstagramError(f"no saved Collection named {name!r} was found")
        posts: list[Post] = []
        cursor: str | None = None
        for _ in range(self._max_pages):
            path = f"/api/v1/feed/collection/{collection}/posts/"
            if cursor:
                path += f"?max_id={cursor}"
            page, cursor = parse_collection_page(self._json(path))
            posts += page
            if not cursor:
                break
        return posts


# the browser


_FETCH_JS = """async ([path, appId]) => {
  const r = await fetch(path, {credentials: 'include', headers: {'x-ig-app-id': appId}});
  return [r.status, await r.text()];
}"""

Session = Callable[[Path], contextlib.AbstractContextManager[GetJson]]


@contextlib.contextmanager
def playwright_session(profile: Path) -> Iterator[GetJson]:
    """A Chrome on the profile, on instagram.com, whose requests carry the login cookies.
    Only GETs are made. The caller holds the profile lock."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise InstagramError(
            "playwright is not installed. Run `uv sync --extra instagram`."
        ) from None
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=True
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
            if "/accounts/login" in page.url:
                raise LoginRequired(f"Instagram is logged out. {LOGIN_HINT}")

            def get_json(path: str) -> tuple[int, Any]:
                status, text = page.evaluate(_FETCH_JS, [path, WEB_APP_ID])
                try:
                    return status, json.loads(text)
                except ValueError:
                    return status, text

            yield get_json
        finally:
            context.close()


def browser_reader(
    home: Path, collection: str, session: Session = playwright_session
) -> Callable[[], list[Post]]:
    """A function that reads the Collection. It holds the profile lock while the browser is
    open. A profile in use (a deep Run, `login`) is `SourceBusy`; a profile never logged in or
    an Instagram that shows the login page is `LoginRequired`."""

    def read() -> list[Post]:
        if not is_logged_in(home):
            raise LoginRequired(f"The browser profile is not logged in. {LOGIN_HINT}")
        profile = profile_dir(home)
        try:
            with profile_lock(home):
                if profile_in_use(profile):
                    raise ProfileBusy("a Chrome window has the browser profile open")
                with session(profile) as get_json:
                    return InstagramApi(get_json).collection_posts(collection)
        except ProfileBusy as exc:
            raise SourceBusy(f"the browser profile is in use ({exc})") from exc

    return read


# downloads and frames


def download_file(url: str, dest: Path) -> None:
    """Fetch a media URL (they are signed, no cookies needed) with a time and size limit."""
    if not url.startswith("https://"):
        raise ValueError("refusing a non-https media URL")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310
        size = 0
        with open(dest, "wb") as out:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise OSError("the media file is over the size limit")
                out.write(chunk)


class FfmpegFrames:
    """A few still frames from a video, for text that is shown rather than spoken."""

    def unavailable(self) -> str | None:
        return None if shutil.which("ffmpeg") else "ffmpeg is not installed"

    def __call__(self, video: Path, folder: Path) -> list[Path]:
        pattern = folder / f"{video.stem}-frame%d.jpg"
        command = [
            "ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-vf", "fps=1/2,scale=720:-2", "-frames:v", str(MAX_FRAMES), str(pattern),
        ]  # fmt: skip
        subprocess.run(  # noqa: S603
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        return sorted(folder.glob(f"{video.stem}-frame*.jpg"))


# the source


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _title(post: Post) -> str:
    first = _one_line(post.caption.strip().splitlines()[0]) if post.caption.strip() else ""
    if len(first) > TITLE_LIMIT:
        first = first[: TITLE_LIMIT - 3].rstrip() + "..."
    return first or post.shortcode


def _url(post: Post) -> str:
    return f"{BASE_URL}/{_URL_PARTS.get(post.kind, 'p')}/{post.shortcode}/"


class InstagramSource:
    name = SOURCE_NAME

    def __init__(
        self,
        read: Callable[[], list[Post]],
        *,
        transcriber: Transcriber,
        download: Callable[[str, Path], None] = download_file,
        frames: Any = None,
        max_duration_seconds: int | None = DEFAULT_MAX_DURATION_SECONDS,
    ) -> None:
        self._read, self._download, self._transcriber = read, download, transcriber
        self._frames = frames if frames is not None else FfmpegFrames()
        self._max_duration = max_duration_seconds
        self._posts: dict[str, Post] = {}

    def list_items(self) -> list[InboxRef]:
        posts = self._read()
        self._posts = {p.shortcode: p for p in posts}
        return [InboxRef(p.shortcode, _title(p)) for p in posts]

    def fetch(self, ref: InboxRef) -> InboxItem:
        """The caption, the images, and for each video its transcript (or frames when it is
        silent). A failed media file is a note; a post with nothing to check at all raises."""
        post = self._posts.get(ref.id)
        if post is None:  # media URLs expire, so a post is only fetched from the listing at hand
            raise ItemSkipped("not in the current listing; it is looked at again next check")
        notes: list[str] = []
        attachments: list[Upload] = []
        transcripts: list[str] = []
        videos_seen = 0
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            images = 0
            for index, media in enumerate(post.media, start=1):
                if media.kind == "image" and images < MAX_IMAGES:
                    images += 1
                    self._attach_image(media, index, folder, attachments, notes)
                elif media.kind == "video" and videos_seen < MAX_VIDEOS:
                    videos_seen += 1
                    self._video(media, folder / f"v{index}", transcripts, attachments, notes)
        if transcripts:
            data = "\n\n".join(transcripts)
            attachments.append(Upload("transcript.txt", data.encode("utf-8")))
        if not post.caption.strip() and not attachments:
            raise OSError("the post has no caption and none of its media could be read")
        text = self._text(post, bool(transcripts), videos_seen, [a.name for a in attachments])
        return InboxItem(post.shortcode, _title(post), text, tuple(attachments), tuple(notes))

    def _attach_image(self, media, index, folder, attachments, notes) -> None:
        path = folder / f"image-{index}.jpg"
        try:
            self._download(media.url, path)
            attachments.append(Upload(f"image-{index}.jpg", path.read_bytes()))
        except Exception as exc:
            notes.append(f"Image {index} could not be downloaded ({_one_line(str(exc))[:200]}).")

    def _video(self, media, folder, transcripts, attachments, notes) -> None:
        if self._max_duration and media.duration and media.duration > self._max_duration:
            notes.append(
                f"A video of {int(media.duration)} seconds is longer than the limit of "
                f"{self._max_duration // 60} minutes and was not transcribed."
            )
            return
        folder.mkdir()
        path = folder / "video.mp4"
        try:
            self._download(media.url, path)
        except Exception as exc:
            notes.append(f"The video download failed ({_one_line(str(exc))[:200]}).")
            return
        reason = self._transcriber.unavailable()
        if reason:
            notes.append(
                f"No transcript: audio transcription is not set up ({reason}). {HOW_TO_ENABLE}"
            )
        else:
            try:
                text = self._transcriber.transcribe(path).strip()
            except Exception as exc:
                notes.append(f"Audio transcription failed ({_one_line(str(exc))[:200]}).")
            else:
                if text:
                    transcripts.append(text)
                    return
                notes.append("The video has no speech; its on-screen text is read from frames.")
        self._attach_frames(path, folder, attachments, notes)

    def _attach_frames(self, video, folder, attachments, notes) -> None:
        reason = self._frames.unavailable()
        if reason:
            notes.append(f"No frames were taken for on-screen text ({reason}).")
            return
        try:
            found = self._frames(video, folder)
        except Exception as exc:
            notes.append(f"Taking frames failed ({_one_line(str(exc))[:200]}).")
            return
        taken = sum(a.name.startswith("frame-") for a in attachments)
        for path in found[:MAX_FRAMES]:
            taken += 1
            attachments.append(Upload(f"frame-{taken}.jpg", path.read_bytes()))

    def _text(self, post: Post, transcript: bool, videos: int, names: list[str]) -> str:
        kind = {"reel": "reel", "post": "post", "carousel": "carousel post"}[post.kind]
        lines = [_title(post), _url(post), ""]
        lines.append(f"Instagram {kind}" + (f" by @{post.author}" if post.author else ""))
        if post.taken_at:
            lines.append(f"Published: {post.taken_at}")
        if post.kind == "carousel":
            lines.append(f"Slides: {len(post.media)}")
        caption = post.caption.strip()
        if len(caption) > CAPTION_LIMIT:
            caption = caption[:CAPTION_LIMIT].rstrip() + " [cut]"
        lines += ["", "Caption:", caption or "(none)", ""]
        images = [n for n in names if n.startswith("image-")]
        frames = [n for n in names if n.startswith("frame-")]
        if transcript:
            lines.append("Transcript: attached as transcript.txt (audio transcribed locally).")
        elif frames:
            lines.append(
                "Transcript: the video has no speech or it is not available. Check the "
                "on-screen text in the attached frames (" + ", ".join(frames) + ")."
            )
        elif videos:
            lines.append(
                "Transcript: not available. Check only what the caption and the images claim."
            )
        if images:
            lines.append(
                "Read the attached image files (" + ", ".join(images) + ") for the text and "
                "claims they show."
            )
        if not (transcript or videos or images):
            lines.append("The post has no media to read; check only what the caption claims.")
        lines.append(
            "If the post makes no factual claim (humour, a joke, a personal moment, an "
            "advertisement), say so in a short Report and list no claims."
        )
        return "\n".join(lines)

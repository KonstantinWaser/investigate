"""Files and images attached to an Item.

Callers (the CLI today, the web submit endpoint and the tray popup later) hand over paths or
in-memory uploads. Everything is checked first, then copied into `<run folder>/attachments/`
under a sanitised, collision-free name. Sources are never moved or modified.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ATTACHMENTS_DIR = "attachments"
DEFAULT_MAX_MB = 25.0
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10))} | {
    f"lpt{i}" for i in range(1, 10)
}
_MAX_NAME = 100


class AttachmentError(ValueError):
    """An attachment cannot be used; the message says which one and why."""


@dataclass(frozen=True)
class Upload:
    """A file that exists only in memory, such as a pasted screenshot."""

    name: str
    content: bytes


AttachmentSource = Path | Upload


@dataclass(frozen=True)
class Attachment:
    name: str  # the sanitised file name inside the attachments folder
    size: int
    is_image: bool

    @property
    def relative_path(self) -> str:
        """Path relative to the run folder, with forward slashes."""
        return f"{ATTACHMENTS_DIR}/{self.name}"


def sanitise_name(name: str) -> str:
    """A safe file name: no folders, no control or shell-special characters, never empty.

    The extension is split off first, so a stem that is all non-ASCII keeps it. Both parts are
    stripped of spaces and dots again after truncating, because Windows drops those silently.
    """
    base = name.replace(chr(92), "/").split("/")[-1]
    base = re.sub(r"[^\w.\- ]", "_", base, flags=re.ASCII).strip(" .")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = ext[:10].strip(" .")
    stem = re.sub(r"[ _]{2,}", "_", stem).strip(" ._")
    stem = stem[: _MAX_NAME - len(ext) - 1].strip(" .") or "file"
    if stem.split(".")[0].lower() in _RESERVED:
        stem = f"attachment-{stem}"
    return f"{stem}.{ext}" if ext else stem


def _source_name(source: AttachmentSource) -> str:
    return source.name


def _size(source: AttachmentSource) -> int:
    if isinstance(source, Upload):
        return len(source.content)
    return source.stat().st_size


def check_attachments(sources: Iterable[AttachmentSource], max_bytes: int) -> None:
    """Raise `AttachmentError` for a missing file, a non-file or one over the size limit."""
    for source in sources:
        label = _source_name(source) or "(unnamed)"
        if isinstance(source, Path):
            if not source.exists():
                raise AttachmentError(f"attachment not found: {source}")
            if not source.is_file():
                raise AttachmentError(f"attachment is not a file: {source}")
        if isinstance(source, Path):
            try:
                with source.open("rb"):
                    pass
            except OSError as exc:
                raise AttachmentError(f"cannot read attachment {source}: {exc}") from exc
        size = _size(source)
        if size > max_bytes:
            raise AttachmentError(
                f"attachment {label} is {size / 1_048_576:.1f} MB, over the limit of "
                f"{max_bytes / 1_048_576:g} MB (max_attachment_mb in config.toml)"
            )


def _unique(name: str, taken: set[str]) -> str:
    candidate = name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
    taken.add(candidate.lower())
    return candidate


def store_attachments(
    folder: Path, sources: Iterable[AttachmentSource], max_bytes: int
) -> list[Attachment]:
    """Check, then copy the sources into `folder/attachments/`. Returns what was stored."""
    sources = list(sources)
    check_attachments(sources, max_bytes)
    if not sources:
        return []
    target = folder / ATTACHMENTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    stored: list[Attachment] = []
    for source in sources:
        label = _source_name(source)
        name = _unique(sanitise_name(label), taken)
        destination = target / name
        try:
            if isinstance(source, Upload):
                destination.write_bytes(source.content)
            else:
                shutil.copyfile(source, destination)
        except OSError as exc:
            raise AttachmentError(f"cannot copy attachment {label}: {exc}") from exc
        stored.append(
            Attachment(
                name, destination.stat().st_size, destination.suffix.lower() in IMAGE_SUFFIXES
            )
        )
    return stored

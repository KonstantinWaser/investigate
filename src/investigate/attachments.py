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
    """A safe file name: no folders, no control or shell-special characters, never empty."""
    base = name.replace(chr(92), "/").split("/")[-1]
    base = re.sub(r"[^\w.\- ]", "_", base, flags=re.ASCII)
    base = re.sub(r"[ _]{2,}", "_", base).strip(" ._")
    if "." in base:
        stem, _, ext = base.rpartition(".")
        ext = ext[:10]
    else:
        stem, ext = base, ""
    stem = stem.strip(" ._")[: _MAX_NAME - len(ext) - 1]
    if not stem or stem.lower() in _RESERVED:
        stem = f"attachment-{stem}" if stem else "attachment"
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
        name = _unique(sanitise_name(_source_name(source)), taken)
        destination = target / name
        if isinstance(source, Upload):
            destination.write_bytes(source.content)
        else:
            shutil.copyfile(source, destination)
        stored.append(
            Attachment(
                name, destination.stat().st_size, destination.suffix.lower() in IMAGE_SUFFIXES
            )
        )
    return stored

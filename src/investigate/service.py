"""`execute_run`: the one path from an Item to a run folder, a Report and a History entry."""

from __future__ import annotations

import json
import os
import re
import shutil
import traceback
import uuid
from collections.abc import Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from investigate import factcheck, history
from investigate.attachments import AttachmentSource, check_attachments, store_attachments
from investigate.browser import is_logged_in, kill_profile_browsers, profile_dir
from investigate.config import Config, Depth, load_config
from investigate.profile_lock import ProfileBusy, profile_lock
from investigate.prompt import RESULT_FILE, build_prompt
from investigate.report import ReportInput, render_report
from investigate.result import parse_result
from investigate.runner import Runner, RunnerOutcome, RunRequest

KIND = "investigation"


@dataclass(frozen=True)
class RunResult:
    id: str
    folder: Path
    status: str  # completed | partial | failed
    report_path: Path
    title: str
    session_id: str | None


def slugify(item: str, limit: int = 40) -> str:
    """A short ASCII folder-name slug from the Item's first line."""
    first = next((line for line in item.splitlines() if line.strip()), "")
    match = re.search(r"https?://\S+", first)
    if match and match.group(0) == first.strip():
        parsed = urlparse(match.group(0))
        first = f"{parsed.netloc} {parsed.path}"
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")[:limit].strip("-")
    return slug or "item"


def _item_title(item: str) -> str:
    first = next((line.strip() for line in item.splitlines() if line.strip()), "Untitled")
    return first if len(first) <= 80 else first[:77] + "..."


def _new_run_folder(home: Path, now: datetime, item: str) -> Path:
    runs = home / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = f"{now:%Y-%m-%d}-{slugify(item)}"
    for n in range(1, 1000):
        folder = runs / (base if n == 1 else f"{base}-{n}")
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return folder
    raise RuntimeError(f"Could not create a run folder for {base}")


def _read_result(folder: Path, outcome: RunnerOutcome) -> str:
    path = folder / RESULT_FILE
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return text
    if outcome.text.strip():
        # The agent answered in its final message instead of the file. Keep what it said.
        path.write_text(outcome.text, encoding="utf-8")
    return outcome.text


def _label(item: str, attachments: Sequence[AttachmentSource]) -> str:
    # With no text, the first attachment's name stands in for the Item in folder name and title.
    return item if item.strip() else (getattr(attachments[0], "name", "") if attachments else "")


def reserve_run(
    item: str,
    depth: Depth,
    *,
    home: Path,
    attachments: Sequence[AttachmentSource] = (),
    now: datetime | None = None,
    kind: str = KIND,
    promoted_from: str | None = None,
) -> Path:
    """Make the run folder and list the Run as `queued`; `execute_run(reserved=...)` starts it.

    Pass the same `now` to both so the entry keeps the time the Item was handed in.
    """
    now = now or datetime.now(UTC)
    label = _label(item, attachments)
    folder = _new_run_folder(home, now.astimezone(), label)
    (folder / "item.md").write_text(item, encoding="utf-8")
    created = now.isoformat(timespec="seconds")
    entry = history.HistoryEntry(
        folder.name,
        kind,
        depth.value,
        "queued",
        _item_title(label),
        created,
        None,
        promoted_from=promoted_from,
    )
    history.upsert(home, entry)
    return folder


def fail_run(home: Path, run_id: str, error: str) -> None:
    """Mark a Run that crashed (still `queued` or `running`) as failed and log why."""
    try:
        entry = next(e for e in history.load_entries(home) if e.id == run_id)
        history.upsert(home, replace(entry, status="failed"))
        with open(home / "runs" / run_id / "agent.log", "a", encoding="utf-8") as log:
            log.write(f"{chr(10)}ERROR: {error}{chr(10)}")
    except (StopIteration, OSError):
        pass


def execute_run(
    item: str,
    depth: Depth,
    *,
    home: Path,
    runner: Runner,
    config: Config | None = None,
    now: datetime | None = None,
    attachments: Sequence[AttachmentSource] = (),
    reserved: Path | None = None,
    kind: str = KIND,
    notes: Sequence[str] = (),
    promoted_from: str | None = None,
) -> RunResult:
    """Run one Investigation (or, with `kind`, a Fact-check) end to end.

    Runner problems become a `failed` Run, not an exception.

    `notes` go into the run log and show as notices in the Report (a missing transcript, say).
    `promoted_from` is the id of the Fact-check an Investigation was promoted from.
    `attachments` are copied into the run folder. A missing or oversize one raises
    `AttachmentError` before any run folder exists. `reserved` is a folder from `reserve_run`.
    An exception after the Run is listed leaves it `failed`, never stuck on `running`.
    """
    started: list[str] = []
    try:
        return _execute_run(
            item,
            depth,
            home,
            runner,
            config,
            now,
            attachments,
            reserved,
            started,
            kind,
            tuple(notes),
            promoted_from,
        )
    except BaseException as exc:
        for run_id in started:
            fail_run(home, run_id, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # The pid only says a live process is working on this Run; once it ends it is stale.
        for run_id in started:
            with suppress(OSError):
                (home / "runs" / run_id / "running.pid").unlink()


def _execute_run(
    item: str,
    depth: Depth,
    home: Path,
    runner: Runner,
    config: Config | None,
    now: datetime | None,
    attachments: Sequence[AttachmentSource],
    reserved: Path | None,
    started: list[str],
    kind: str,
    notes: tuple[str, ...],
    promoted_from: str | None,
) -> RunResult:
    config = config or load_config(home)
    now = now or datetime.now(UTC)
    max_bytes = int(config.max_attachment_mb * 1_048_576)
    check_attachments(attachments, max_bytes)
    label = _label(item, attachments)
    folder = reserved or _new_run_folder(home, now.astimezone(), label)
    run_id = folder.name
    created = now.isoformat(timespec="seconds")
    (folder / "item.md").write_text(item, encoding="utf-8")
    try:
        stored = tuple(store_attachments(folder, attachments, max_bytes))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # no half-made run folder
        raise
    session_id = str(uuid.uuid4())

    def entry(
        status: str, title: str, sid: str | None, verdicts: dict[str, int] | None = None
    ) -> history.HistoryEntry:
        # A Follow-up fallback Run is linked when it is reserved; keep that link on every rewrite.
        prior = next((e for e in history.load_entries(home) if e.id == run_id), None)
        link = prior.followup_of if prior else None
        return history.HistoryEntry(
            run_id,
            kind,
            depth.value,
            status,
            title,
            created,
            sid,
            verdicts,
            followup_of=link,
            promoted_from=promoted_from,
        )

    # Recovery after a hard kill tells a dead Run from a live one by this pid.
    (folder / "running.pid").write_text(str(os.getpid()), encoding="utf-8")
    history.upsert(home, entry("running", _item_title(label), session_id))
    started.append(run_id)

    settings = config.depths[depth]
    # A Context file that is missing is noted in the run log and skipped, never fatal.
    contexts = tuple((c.name, Path(c.path)) for c in config.contexts if Path(c.path).is_file())
    present = {name for name, _ in contexts}
    context_notes = [f"NOTE: {note}" + chr(10) for note in notes] + [
        f"WARNING: Context {c.name!r} skipped, file not found: {c.path}" + chr(10)
        for c in config.contexts
        if c.name not in present
    ]
    context_files = tuple(path for _, path in contexts)
    # One Run at a time may drive the browser profile. A busy profile means this Run goes on
    # without the browser and says so in its prompt.
    stack = ExitStack()
    use_browser: bool | None = None
    if config.browser and not is_logged_in(home):
        use_browser = False
        context_notes.append(
            "WARNING: browser = true but no login yet; run `investigate login`. "
            "Running without the browser." + chr(10)
        )
    elif config.browser:
        try:
            stack.enter_context(profile_lock(home))
            stack.callback(kill_profile_browsers, profile_dir(home))
            use_browser = True
        except ProfileBusy:
            use_browser = False
    request = RunRequest(
        prompt=build_prompt(item, depth, config.language, contexts, stored, use_browser, kind),
        workdir=folder,
        session_id=session_id,
        timeout_seconds=settings.timeout_minutes * 60,
        model=settings.model,
        permission_mode=config.permission_mode,
        context_files=context_files,
        browser_profile=profile_dir(home) if use_browser else None,
    )
    try:
        with stack:
            outcome = runner.run(request)
    except Exception:  # a runner must not raise, but one that does becomes a failed Run
        error = traceback.format_exc()
        outcome = RunnerOutcome(False, False, "", session_id, error, error)

    log = "".join(context_notes) + (outcome.log or "")
    if outcome.error and outcome.error not in log:
        log += f"\nERROR: {outcome.error}\n"
    (folder / "agent.log").write_text(log, encoding="utf-8")
    parsed = parse_result(_read_result(folder, outcome), fact_check=kind == factcheck.KIND)
    if parsed.structured:
        (folder / "result.json").write_text(
            json.dumps(parsed.structured.to_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    if not outcome.ok:
        status = "failed"
    elif outcome.timed_out:
        status = "partial"
    else:
        status = "completed"
    title = parsed.structured.title if parsed.structured else _item_title(label)
    sid = outcome.session_id or session_id

    html = render_report(
        ReportInput(
            title=title,
            item=item,
            kind=kind,
            depth=depth.value,
            status=status,
            created=created,
            result=parsed,
            error=outcome.error if status == "failed" else None,
            contexts=tuple(name for name, _ in contexts),
            attachments=stored,
            notes=notes,
            run_id=run_id,
            promoted_from=promoted_from,
        )
    )
    report_path = folder / "report.html"
    report_path.write_text(html, encoding="utf-8")
    (folder / "run.json").write_text(
        json.dumps(
            {
                "id": run_id,
                "kind": kind,
                "depth": depth.value,
                "status": status,
                "title": title,
                "created": created,
                "session_id": sid,
                "runner": runner.name,
                "model": settings.model,
                "timeout_minutes": settings.timeout_minutes,
                "browser": use_browser,
                "workdir": str(folder),  # the agent's cwd; a Follow-up resumes in the same one
                "result_problems": list(parsed.problems),
                "notes": list(notes),
                "promoted_from": promoted_from,
                "attachments": [{"name": a.name, "size": a.size} for a in stored],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    verdicts = parsed.structured.verdict_counts() if parsed.structured else None
    history.upsert(home, entry(status, title, sid, verdicts))
    return RunResult(run_id, folder, status, report_path, title, sid)

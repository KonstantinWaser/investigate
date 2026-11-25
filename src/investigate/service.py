"""`execute_run`: the one path from an Item to a run folder, a Report and a History entry."""

from __future__ import annotations

import json
import re
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from investigate import history
from investigate.config import Config, Depth, load_config
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


def execute_run(
    item: str,
    depth: Depth,
    *,
    home: Path,
    runner: Runner,
    config: Config | None = None,
    now: datetime | None = None,
) -> RunResult:
    """Run one Investigation end to end. Runner problems become a `failed` Run, not an exception."""
    config = config or load_config(home)
    now = now or datetime.now(UTC)
    folder = _new_run_folder(home, now.astimezone(), item)
    run_id = folder.name
    created = now.isoformat(timespec="seconds")
    (folder / "item.md").write_text(item, encoding="utf-8")
    session_id = str(uuid.uuid4())

    def entry(status: str, title: str, sid: str | None) -> history.HistoryEntry:
        return history.HistoryEntry(run_id, KIND, depth.value, status, title, created, sid)

    history.upsert(home, entry("running", _item_title(item), session_id))

    settings = config.depths[depth]
    # A Context file that is missing is noted in the run log and skipped, never fatal.
    contexts = tuple((c.name, Path(c.path)) for c in config.contexts if Path(c.path).is_file())
    present = {name for name, _ in contexts}
    context_notes = [
        f"WARNING: Context {c.name!r} skipped, file not found: {c.path}" + chr(10)
        for c in config.contexts
        if c.name not in present
    ]
    context_files = tuple(path for _, path in contexts)
    request = RunRequest(
        prompt=build_prompt(item, depth, config.language, contexts),
        workdir=folder,
        session_id=session_id,
        timeout_seconds=settings.timeout_minutes * 60,
        model=settings.model,
        permission_mode=config.permission_mode,
        context_files=context_files,
    )
    try:
        outcome = runner.run(request)
    except Exception:  # a runner must not raise, but one that does becomes a failed Run
        error = traceback.format_exc()
        outcome = RunnerOutcome(False, False, "", session_id, error, error)

    log = "".join(context_notes) + (outcome.log or "")
    if outcome.error and outcome.error not in log:
        log += f"\nERROR: {outcome.error}\n"
    (folder / "agent.log").write_text(log, encoding="utf-8")
    parsed = parse_result(_read_result(folder, outcome))
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
    title = parsed.structured.title if parsed.structured else _item_title(item)
    sid = outcome.session_id or session_id

    html = render_report(
        ReportInput(
            title=title,
            item=item,
            kind=KIND,
            depth=depth.value,
            status=status,
            created=created,
            result=parsed,
            error=outcome.error if status == "failed" else None,
            contexts=tuple(name for name, _ in contexts),
        )
    )
    report_path = folder / "report.html"
    report_path.write_text(html, encoding="utf-8")
    (folder / "run.json").write_text(
        json.dumps(
            {
                "id": run_id,
                "kind": KIND,
                "depth": depth.value,
                "status": status,
                "title": title,
                "created": created,
                "session_id": sid,
                "runner": runner.name,
                "model": settings.model,
                "timeout_minutes": settings.timeout_minutes,
                "result_problems": list(parsed.problems),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    history.upsert(home, entry(status, title, sid))
    return RunResult(run_id, folder, status, report_path, title, sid)

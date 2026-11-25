import json
from datetime import UTC, datetime

from investigate import history
from investigate.cli import main
from investigate.config import Depth, config_path
from investigate.service import execute_run, slugify
from tests.fakes import GOOD_RESULT, FakeRunner

NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _run(home, runner, item="How does X work? https://example.com", depth=Depth.QUICK):
    return execute_run(item, depth, home=home, runner=runner, now=NOW)


def test_run_creates_folder_report_and_history_entry(home):
    runner = FakeRunner()
    result = _run(home, runner)

    assert result.status == "completed"
    folder = result.folder
    assert folder.parent == home / "runs"
    assert folder.name.startswith("2026-03-0")
    for name in ("item.md", "result.md", "result.json", "report.html", "agent.log", "run.json"):
        assert (folder / name).is_file(), name
    assert (folder / "item.md").read_text(
        encoding="utf-8"
    ) == "How does X work? https://example.com"
    assert json.loads((folder / "result.json").read_text(encoding="utf-8"))["title"] == (
        "Found a thing"
    )
    assert "Found a thing" in (folder / "report.html").read_text(encoding="utf-8")

    (entry,) = history.load_entries(home)
    assert entry.id == folder.name
    assert entry.kind == "investigation"
    assert entry.depth == "quick"
    assert entry.status == "completed"
    assert entry.title == "Found a thing"
    assert entry.created == "2026-03-04T12:00:00+00:00"
    assert entry.session_id == "fake-session"


def test_runner_gets_run_folder_depth_timeout_and_prompt(home):
    runner = FakeRunner()
    _run(home, runner, depth=Depth.DEEP)
    (request,) = runner.requests
    assert request.workdir.parent == home / "runs"
    assert request.timeout_seconds == 45 * 60
    assert request.model == "opus"
    assert "parallel subagents" in request.prompt
    assert "How does X work?" in request.prompt
    assert "sub-questions" in request.prompt
    assert "result.md" in request.prompt


def test_timeout_keeps_partial_output(home):
    result = _run(home, FakeRunner(timed_out=True))
    assert result.status == "partial"
    assert "Found a thing" in result.report_path.read_text(encoding="utf-8")
    assert history.load_entries(home)[0].status == "partial"


def test_runner_failure_is_failed_with_error_in_log(home):
    result = _run(home, FakeRunner(result_file=None, text="", ok=False, error="exit code 3"))
    assert result.status == "failed"
    assert "exit code 3" in (result.folder / "agent.log").read_text(encoding="utf-8")
    assert "exit code 3" in result.report_path.read_text(encoding="utf-8")
    assert history.load_entries(home)[0].status == "failed"


def test_runner_exception_is_failed_not_a_crash(home):
    result = _run(home, FakeRunner(raises=RuntimeError("kaboom")))
    assert result.status == "failed"
    assert "kaboom" in (result.folder / "agent.log").read_text(encoding="utf-8")
    assert result.report_path.exists()


def test_missing_result_still_produces_report(home):
    result = _run(home, FakeRunner(result_file=None, text=""))
    html = result.report_path.read_text(encoding="utf-8")
    assert result.status == "completed"
    assert "missing or malformed" in html
    assert not (result.folder / "result.json").exists()


def test_malformed_result_still_produces_report(home):
    result = _run(home, FakeRunner(result_file="text\n```json\n{broken\n```\n"))
    assert "missing or malformed" in result.report_path.read_text(encoding="utf-8")
    assert not (result.folder / "result.json").exists()


def test_result_in_final_message_is_used_when_file_missing(home):
    result = _run(home, FakeRunner(result_file=None, text=GOOD_RESULT))
    assert result.title == "Found a thing"
    assert (result.folder / "result.md").read_text(encoding="utf-8") == GOOD_RESULT


def test_same_item_twice_gets_two_folders_and_entries(home):
    first = _run(home, FakeRunner())
    second = _run(home, FakeRunner())
    assert first.folder != second.folder
    assert len(history.load_entries(home)) == 2


def test_slugify():
    assert slugify("Hello, World! Ünïcode") == "hello-world-n-code"
    assert slugify("https://example.com/a/b") == "example-com-a-b"
    assert slugify("???") == "item"
    assert len(slugify("x" * 200)) <= 40


def test_damaged_index_is_set_aside(home):
    home.mkdir()
    (home / "index.json").write_text("{nope", encoding="utf-8")
    _run(home, FakeRunner())
    assert len(list(home.glob("index.json.broken*"))) == 1
    assert len(history.load_entries(home)) == 1


def test_cli_run_and_history(home, capsys):
    code = main(["run", "some topic", "--depth", "quick"], runner=FakeRunner())
    out = capsys.readouterr().out
    assert code == 0
    assert "Report:" in out
    assert config_path(home).exists()
    main(["history"])
    assert "Found a thing" in capsys.readouterr().out


def test_cli_failed_run_exits_nonzero(home):
    runner = FakeRunner(result_file=None, text="", ok=False, error="x")
    assert main(["run", "topic"], runner=runner) == 1


def test_cli_bad_config_exits_2(home, capsys):
    home.mkdir()
    config_path(home).write_text('permission_mode = "nope"', encoding="utf-8")
    assert main(["run", "topic"], runner=FakeRunner()) == 2
    assert "permission_mode" in capsys.readouterr().err

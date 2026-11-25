import json
from datetime import UTC, datetime

import pytest

from investigate.config import ConfigError, Depth, config_path, load_config
from investigate.service import execute_run
from tests.fakes import FakeRunner

NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _result(relevance: list | None) -> str:
    data = {"schema_version": 1, "title": "T", "summary": "S", "findings": []}
    if relevance is not None:
        data["relevance"] = relevance
    return "Write-up\n\n```json\n" + json.dumps(data) + "\n```\n"


def _setup(home, entries: str, files: dict[str, str]):
    home.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (home / name).write_text(text, encoding="utf-8")
    config_path(home).write_text(f"context_files = {entries}", encoding="utf-8")


def _run(home, runner):
    return execute_run("topic", Depth.QUICK, home=home, runner=runner, now=NOW)


def _html(result) -> str:
    return result.report_path.read_text(encoding="utf-8")


def test_config_accepts_named_contexts_in_order(home):
    _setup(home, '[{name = "b", path = "b.md"}, {name = "a", path = "a.md"}]', {})
    config = load_config(home)
    assert [c.name for c in config.contexts] == ["b", "a"]
    assert config.context_files == (str((home / "b.md").resolve()), str((home / "a.md").resolve()))


def test_plain_path_is_still_accepted_and_named_by_stem(home):
    _setup(home, '["notes/employer.md"]', {})
    (context,) = load_config(home).contexts
    assert context.name == "employer"


@pytest.mark.parametrize(
    "entries",
    [
        '[{name = "a", path = "x.md"}, {name = "a", path = "y.md"}]',
        '[{name = "", path = "x.md"}]',
        '[{path = "x.md"}]',
        '[{name = "a"}]',
        "[5]",
    ],
)
def test_bad_context_entries_are_config_errors(home, entries):
    _setup(home, entries, {})
    with pytest.raises(ConfigError):
        load_config(home)


def test_report_has_one_subsection_per_context_in_config_order(home):
    _setup(
        home,
        '[{name = "employer", path = "e.md"}, {name = "studies", path = "s.md"}]',
        {"e.md": "work", "s.md": "uni"},
    )
    relevance = [
        {"context": "studies", "relevant": True, "text": "Matters for the thesis."},
        {"context": "employer", "relevant": True, "text": "Matters for the product."},
    ]
    runner = FakeRunner(_result(relevance))
    html = _html(_run(home, runner))
    assert "<h2>Relevance to your work</h2>" in html
    assert html.index("<h3>employer</h3>") < html.index("<h3>studies</h3>")
    assert "Matters for the product." in html
    assert "Matters for the thesis." in html
    (request,) = runner.requests
    assert "- employer:" in request.prompt
    assert "strictly separate" in request.prompt
    assert len(request.context_files) == 2


def test_structured_result_keeps_relevance_per_context(home):
    _setup(home, '[{name = "employer", path = "e.md"}]', {"e.md": "work"})
    relevance = [{"context": "employer", "relevant": False, "text": ""}]
    result = _run(home, FakeRunner(_result(relevance)))
    saved = json.loads((result.folder / "result.json").read_text(encoding="utf-8"))
    assert saved["relevance"] == relevance


def test_context_with_nothing_relevant_renders_not_relevant(home):
    _setup(home, '[{name = "studies", path = "s.md"}]', {"s.md": "uni"})
    for entry in (
        {"context": "studies", "relevant": False, "text": "Invented link."},
        {"context": "studies", "relevant": True, "text": ""},
    ):
        html = _html(_run(home, FakeRunner(_result([entry]))))
        assert "Not relevant" in html
        assert "Invented link." not in html


def test_context_the_agent_skipped_is_not_shown_as_relevant(home):
    _setup(home, '[{name = "studies", path = "s.md"}]', {"s.md": "uni"})
    html = _html(_run(home, FakeRunner(_result([]))))
    assert "<h3>studies</h3>" in html
    assert "reported nothing for this Context" in html


def test_unknown_context_names_from_the_agent_are_not_rendered(home):
    _setup(home, '[{name = "studies", path = "s.md"}]', {"s.md": "uni"})
    relevance = [{"context": "secret", "relevant": True, "text": "Leaked text."}]
    html = _html(_run(home, FakeRunner(_result(relevance))))
    assert "Leaked text." not in html
    assert "<h3>secret</h3>" not in html


def test_relevance_text_is_escaped(home):
    _setup(home, '[{name = "studies", path = "s.md"}]', {"s.md": "uni"})
    relevance = [{"context": "studies", "relevant": True, "text": "<script>x()</script>"}]
    html = _html(_run(home, FakeRunner(_result(relevance))))
    assert "<script>x()" not in html


def test_missing_context_file_is_logged_and_skipped(home):
    _setup(
        home,
        '[{name = "gone", path = "gone.md"}, {name = "here", path = "here.md"}]',
        {"here.md": "present"},
    )
    relevance = [{"context": "here", "relevant": True, "text": "Applies."}]
    runner = FakeRunner(_result(relevance))
    result = _run(home, runner)
    assert result.status == "completed"
    log = (result.folder / "agent.log").read_text(encoding="utf-8")
    assert "Context 'gone' skipped" in log
    html = _html(result)
    assert "<h3>here</h3>" in html
    assert "<h3>gone</h3>" not in html
    (request,) = runner.requests
    assert len(request.context_files) == 1
    assert "gone.md" not in request.prompt


def test_no_context_means_no_relevance_section(home):
    result = _run(home, FakeRunner(_result(None)))
    assert result.status == "completed"
    assert "Relevance to your work" not in _html(result)
    (request,) = (FakeRunner().requests,)
    assert request == []


def test_all_context_files_missing_omits_the_section_but_succeeds(home):
    _setup(home, '[{name = "gone", path = "gone.md"}]', {})
    result = _run(home, FakeRunner(_result(None)))
    assert result.status == "completed"
    assert "Relevance to your work" not in _html(result)


def test_malformed_relevance_makes_the_result_malformed_not_a_crash(home):
    _setup(home, '[{name = "a", path = "a.md"}]', {"a.md": "x"})
    result = _run(home, FakeRunner(_result([{"context": "a", "relevant": "yes", "text": ""}])))
    html = _html(result)
    assert "malformed" in html
    assert "<h3>a</h3>" in html

import json
from datetime import UTC, datetime

from investigate import history
from investigate.config import Depth
from investigate.prompt import build_prompt
from investigate.report import ReportInput, render_report
from investigate.result import VERDICTS, parse_result
from investigate.service import execute_run
from investigate.webpages import VERDICT_LABELS
from tests.fakes import GOOD_FACTCHECK, GOOD_RESULT, FakeRunner

NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def test_verdict_names_match_the_history_filter():
    assert tuple(VERDICT_LABELS) == VERDICTS


def test_parse_claims_normalises_verdicts_and_counts_them():
    parsed = parse_result(GOOD_FACTCHECK, fact_check=True)
    assert parsed.problems == ()
    structured = parsed.structured
    assert [c.verdict for c in structured.claims] == ["confirmed", "wrong", "partly_true"]
    assert structured.verdict_counts() == {"confirmed": 1, "partly_true": 1, "wrong": 1}
    assert structured.to_json()["claims"][0]["citations"][0]["url"] == "https://example.com/g"


def test_investigation_result_has_no_claims_and_no_counts():
    structured = parse_result(GOOD_RESULT).structured
    assert structured.claims is None
    assert structured.verdict_counts() is None
    assert "claims" not in structured.to_json()


def test_fact_check_without_claims_list_is_malformed():
    parsed = parse_result(GOOD_RESULT, fact_check=True)
    assert parsed.structured is None
    assert any("claims" in p for p in parsed.problems)


def test_fact_check_with_no_claims_is_valid_and_counts_nothing():
    text = GOOD_FACTCHECK.split("```json")[0] + (
        '```json\n{"title": "T", "summary": "Nothing to check.", "claims": []}\n```\n'
    )
    parsed = parse_result(text, fact_check=True)
    assert parsed.structured.claims == ()
    assert parsed.structured.verdict_counts() == {}


def test_bad_verdict_reason_and_citation_are_reported():
    text = (
        '```json\n{"title": "T", "summary": "S", "claims": ['
        '{"claim": "c", "verdict": "maybe", "reason": "", "citations": [{"title": "x"}]}]}\n```'
    )
    parsed = parse_result(text, fact_check=True)
    assert parsed.structured is None
    joined = " ".join(parsed.problems)
    assert "verdict" in joined and "reason" in joined and "url" in joined


def test_fact_check_prompt_has_task_schema_and_verdicts():
    prompt = build_prompt("item text", Depth.QUICK, "English", kind="fact-check")
    assert "fact-checking agent" in prompt
    assert '"claims"' in prompt and "partly_true" in prompt and "outdated" in prompt
    assert '"findings"' not in prompt
    assert "item text" in prompt


def test_investigation_prompt_is_unchanged_by_the_kind_switch():
    prompt = build_prompt("x", Depth.QUICK, "English")
    assert '"findings"' in prompt and "fact-checking" not in prompt


def test_fact_check_run_records_kind_verdicts_and_notes(home):
    runner = FakeRunner(GOOD_FACTCHECK)
    result = execute_run(
        "Video title\nhttps://example.com/v",
        Depth.QUICK,
        home=home,
        runner=runner,
        now=NOW,
        kind="fact-check",
        notes=("No transcript: ffmpeg is missing.",),
    )
    (entry,) = history.load_entries(home)
    assert entry.kind == "fact-check"
    assert entry.verdicts == {"confirmed": 1, "partly_true": 1, "wrong": 1}
    assert "fact-checking agent" in runner.requests[0].prompt
    log = (result.folder / "agent.log").read_text(encoding="utf-8")
    assert "No transcript: ffmpeg is missing." in log
    html = (result.folder / "report.html").read_text(encoding="utf-8")
    assert "No transcript: ffmpeg is missing." in html
    assert "Promote to Investigation" in html
    assert f'action="/runs/{result.id}/promote"' in html
    run_json = json.loads((result.folder / "run.json").read_text(encoding="utf-8"))
    assert run_json["kind"] == "fact-check"


def test_investigation_report_has_no_promote_form(home):
    result = execute_run("q", Depth.QUICK, home=home, runner=FakeRunner(), now=NOW)
    assert "Promote" not in (result.folder / "report.html").read_text(encoding="utf-8")
    (entry,) = history.load_entries(home)
    assert entry.verdicts is None and entry.promoted_from is None


def test_report_renders_badges_reasons_citations_and_escapes():
    text = GOOD_FACTCHECK.replace("Adults need", "<script>alert(1)</script> Adults need")
    html = render_report(
        ReportInput(
            title="t",
            item="i",
            kind="fact-check",
            depth="quick",
            status="completed",
            created="2026-03-04",
            result=parse_result(text, fact_check=True),
            run_id="r1",
        )
    )
    assert 'class="verdict v-confirmed">Confirmed<' in html
    assert 'class="verdict v-wrong">Wrong<' in html
    assert 'class="verdict v-partly_true">Partly true<' in html
    assert "Guidelines say so." in html
    assert 'href="https://example.com/g"' in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_promoted_investigation_links_back():
    html = render_report(
        ReportInput(
            title="t",
            item="i",
            kind="investigation",
            depth="quick",
            status="completed",
            created="c",
            result=parse_result(GOOD_RESULT),
            promoted_from="2026-03-04-video",
        )
    )
    assert 'href="/runs/2026-03-04-video/"' in html

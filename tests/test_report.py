import re

from investigate.report import ReportInput, render_report
from investigate.result import parse_result
from tests.fakes import GOOD_RESULT


def _render(result_text, *, status="completed", item="an item", error=None):
    return render_report(
        ReportInput(
            title="Fallback",
            item=item,
            kind="investigation",
            depth="quick",
            status=status,
            created="2026-01-01T00:00:00+00:00",
            result=parse_result(result_text),
            error=error,
        )
    )


def test_report_shows_structured_result():
    html = _render(GOOD_RESULT)
    assert "<title>Found a thing</title>" in html
    assert "The thing exists." in html
    assert "high confidence" in html
    assert 'href="https://example.com/docs"' in html


def test_report_is_self_contained():
    html = _render(GOOD_RESULT)
    assert "<script" not in html
    assert "<link" not in html
    assert "<img" not in html
    assert "@import" not in html
    assert "url(" not in html
    assert not re.search(r"\bsrc=", html)
    # The only URLs are citation anchors, never resources the page loads.
    assert re.findall(r'(?:href|src)="([^"]+)"', html) == ["https://example.com/docs"]


def test_report_escapes_agent_text():
    evil = "<script>alert(1)</script>"
    result = f"""\
raw <b>html</b> {evil} ![img](https://evil.example/x.png)

```json
{{"title": "{evil}", "summary": "{evil}", "findings": [{{"claim": "{evil}",
"detail": "{evil}", "confidence": "low",
"citations": [{{"title": "{evil}", "url": "javascript:alert(1)", "note": "{evil}"}},
{{"title": "ok", "url": "https://a.example/?q=\\"><x>", "note": ""}}]}}]}}
```
"""
    html = _render(result, item=evil)
    assert "<script>" not in html
    assert "<b>html</b>" not in html
    assert "<img" not in html
    assert 'href="javascript:' not in html
    assert "&lt;script&gt;" in html
    assert '"><x>' not in html


def test_markdown_body_renders_but_link_scheme_is_safe():
    html = _render("# Head\n\n[bad](javascript:alert(1)) and **bold**\n")
    assert "<strong>bold</strong>" in html
    assert 'href="javascript:' not in html


def test_malformed_result_report_says_so():
    html = _render("some text\n```json\n{oops}\n```\n")
    assert "missing or malformed" in html
    assert "some text" in html
    assert "not valid JSON" in html


def test_missing_result_report_says_so():
    html = _render(None)
    assert "missing or malformed" in html
    assert "produced no result" in html


def test_status_notices():
    assert "time limit" in _render(GOOD_RESULT, status="partial")
    failed = _render(None, status="failed", error="boom <b>")
    assert "Run failed" in failed
    assert "boom &lt;b&gt;" in failed

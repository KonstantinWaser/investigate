"""The agent's structured result: a Markdown body plus one JSON block.

`parse_result` never raises. Anything wrong comes back in `ParsedResult.problems`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
CONFIDENCES = ("high", "medium", "low")

# Shown to the agent in the prompt. Keep in step with `parse_result`.
SCHEMA_EXAMPLE = """\
```json
{
  "schema_version": 1,
  "title": "Short title of the Report",
  "summary": "Two to four sentences answering the question.",
  "findings": [
    {
      "claim": "One checkable statement.",
      "detail": "Optional context, dates, caveats.",
      "confidence": "high",
      "citations": [
        {"title": "Page title", "url": "https://example.com/page", "note": "What it shows"}
      ]
    }
  ]
}
```"""

_JSON_BLOCK = re.compile(r"^```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class Citation:
    title: str
    url: str
    note: str = ""


@dataclass(frozen=True)
class Finding:
    claim: str
    detail: str
    confidence: str
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class StructuredResult:
    title: str
    summary: str
    findings: tuple[Finding, ...]

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "title": self.title,
            "summary": self.summary,
            "findings": [
                {
                    "claim": f.claim,
                    "detail": f.detail,
                    "confidence": f.confidence,
                    "citations": [
                        {"title": c.title, "url": c.url, "note": c.note} for c in f.citations
                    ],
                }
                for f in self.findings
            ],
        }


@dataclass(frozen=True)
class ParsedResult:
    body: str  # Markdown text outside the JSON block
    structured: StructuredResult | None
    problems: tuple[str, ...] = field(default=())


def parse_result(text: str | None) -> ParsedResult:
    if not text or not text.strip():
        return ParsedResult("", None, ("The agent produced no result.",))
    matches = list(_JSON_BLOCK.finditer(text))
    if not matches:
        return ParsedResult(text.strip(), None, ("The result has no JSON block.",))
    last = matches[-1]
    body = (text[: last.start()] + text[last.end() :]).strip()
    try:
        data = json.loads(last.group(1))
    except json.JSONDecodeError as exc:
        return ParsedResult(body, None, (f"The JSON block is not valid JSON: {exc}",))
    problems: list[str] = []
    structured = _validate(data, problems)
    if problems:
        return ParsedResult(body, None, tuple(problems))
    return ParsedResult(body, structured)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) else None


def _validate(data: object, problems: list[str]) -> StructuredResult | None:
    if not isinstance(data, dict):
        problems.append("The JSON block is not an object.")
        return None
    title = _text(data.get("title"))
    if not title:
        problems.append("'title' is missing or not a non-empty string.")
    summary = _text(data.get("summary"))
    if summary is None:
        problems.append("'summary' is missing or not a string.")
    raw_findings = data.get("findings")
    findings: list[Finding] = []
    if not isinstance(raw_findings, list):
        problems.append("'findings' is missing or not a list.")
    else:
        for i, item in enumerate(raw_findings):
            finding = _finding(item, i, problems)
            if finding:
                findings.append(finding)
    if problems:
        return None
    return StructuredResult(title or "", summary or "", tuple(findings))


def _finding(item: object, index: int, problems: list[str]) -> Finding | None:
    where = f"findings[{index}]"
    if not isinstance(item, dict):
        problems.append(f"{where} is not an object.")
        return None
    claim = _text(item.get("claim"))
    if not claim:
        problems.append(f"{where}.claim is missing or not a non-empty string.")
    confidence = _text(item.get("confidence"))
    confidence = confidence.lower() if confidence else None
    if confidence not in CONFIDENCES:
        problems.append(f"{where}.confidence must be one of {', '.join(CONFIDENCES)}.")
    detail = item.get("detail", "")
    if not isinstance(detail, str):
        problems.append(f"{where}.detail is not a string.")
        detail = ""
    raw_citations = item.get("citations", [])
    citations: list[Citation] = []
    if not isinstance(raw_citations, list):
        problems.append(f"{where}.citations is not a list.")
    else:
        for j, raw in enumerate(raw_citations):
            url = _text(raw.get("url")) if isinstance(raw, dict) else None
            if not url:
                problems.append(f"{where}.citations[{j}] needs a string 'url'.")
                continue
            title = _text(raw.get("title")) or url
            note = _text(raw.get("note")) or ""
            citations.append(Citation(title, url, note))
    if not claim or confidence not in CONFIDENCES:
        return None
    return Finding(claim, detail.strip(), confidence, tuple(citations))

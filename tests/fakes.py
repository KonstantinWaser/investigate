from __future__ import annotations

import re

from investigate.prompt import RESULT_FILE
from investigate.runner import RunnerOutcome, RunRequest

GOOD_RESULT = """\
# Write-up

Something was found.

```json
{
  "schema_version": 1,
  "title": "Found a thing",
  "summary": "A short answer.",
  "findings": [
    {
      "claim": "The thing exists.",
      "detail": "Since 2024.",
      "confidence": "high",
      "citations": [{"title": "Docs", "url": "https://example.com/docs", "note": "primary"}]
    }
  ]
}
```
"""

GOOD_ANSWER = """The follow-up answer body.

```json
{
  "schema_version": 1,
  "title": "Answer",
  "summary": "A follow-up summary {n}.",
  "findings": [
    {
      "claim": "A follow-up claim {n}.",
      "detail": "",
      "confidence": "medium",
      "citations": [{"title": "Other", "url": "https://example.com/other{n}", "note": ""}]
    }
  ]
}
```
"""


class FakeRunner:
    """A runner that writes a canned result file, like the agent would, and reports an outcome."""

    name = "fake"

    def __init__(
        self,
        result_file: str | None = GOOD_RESULT,
        *,
        text: str = "done",
        ok: bool = True,
        timed_out: bool = False,
        error: str | None = None,
        raises: Exception | None = None,
        session_id: str | None = "fake-session",
        answer: str | None = GOOD_ANSWER,
        resume_unavailable: bool = False,
        resume_ok: bool = True,
        resume_timed_out: bool = False,
    ) -> None:
        self.result_file = result_file
        self.text = text
        self.ok = ok
        self.timed_out = timed_out
        self.error = error
        self.raises = raises
        self.session_id = session_id
        self.requests: list[RunRequest] = []
        self.answer = answer
        self.resume_unavailable = resume_unavailable
        self.resume_ok = resume_ok
        self.resume_timed_out = resume_timed_out
        self.resumes: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunnerOutcome:
        self.requests.append(request)
        if self.raises:
            raise self.raises
        if self.result_file is not None:
            (request.workdir / RESULT_FILE).write_text(self.result_file, encoding="utf-8")
        return RunnerOutcome(
            ok=self.ok,
            timed_out=self.timed_out,
            text=self.text,
            session_id=self.session_id,
            log="fake log\n" + (f"ERROR: {self.error}\n" if self.error else ""),
            error=self.error,
        )

    def resume(self, request: RunRequest) -> RunnerOutcome:
        """Writes the answer file the prompt names, with `{n}` filled in from its number."""
        self.resumes.append(request)
        if self.resume_unavailable:
            return RunnerOutcome(False, False, "", None, "", "No conversation found", True)
        match = re.search(r"followup-(\d+)\.md", request.prompt)
        if self.answer is not None and match:
            text = self.answer.replace("{n}", match.group(1))
            (request.workdir / match.group(0)).write_text(text, encoding="utf-8")
        return RunnerOutcome(
            ok=self.resume_ok,
            timed_out=self.resume_timed_out,
            text="done",
            session_id=request.session_id,
            log="fake resume log\n",
            error=None if self.resume_ok else "resume failed",
        )


GOOD_FACTCHECK = """\
# Write-up

Two claims were checked.

```json
{
  "schema_version": 1,
  "title": "A video about sleep",
  "summary": "Mostly reliable.",
  "claims": [
    {
      "claim": "Adults need 7 to 9 hours of sleep.",
      "verdict": "confirmed",
      "reason": "Guidelines say so.",
      "citations": [{"title": "Guideline", "url": "https://example.com/g", "note": "2015"}]
    },
    {
      "claim": "Everyone needs 12 hours.",
      "verdict": "Wrong",
      "reason": "No guideline says that.",
      "citations": []
    },
    {
      "claim": "Blue light ruins sleep.",
      "verdict": "partly true",
      "reason": "The effect is small.",
      "citations": []
    }
  ]
}
```
"""

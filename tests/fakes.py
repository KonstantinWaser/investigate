from __future__ import annotations

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
    ) -> None:
        self.result_file = result_file
        self.text = text
        self.ok = ok
        self.timed_out = timed_out
        self.error = error
        self.raises = raises
        self.session_id = session_id
        self.requests: list[RunRequest] = []

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

"""A stand-in for the `claude` command. Behaviour comes from the STUB_MODE env var."""

import json
import os
import sys
import time

prompt = sys.stdin.read()
mode = os.environ.get("STUB_MODE", "ok")
session_id = sys.argv[sys.argv.index("--session-id") + 1]

if mode == "hang":
    with open("result.md", "w", encoding="utf-8") as f:
        f.write("partial text")
    time.sleep(60)
elif mode == "fail":
    sys.stderr.write("boom\n")
    sys.exit(3)
elif mode == "agent_error":
    print(json.dumps({"is_error": True, "result": "rate limited", "session_id": session_id}))
else:
    print(
        json.dumps(
            {"is_error": False, "result": f"prompt chars: {len(prompt)}", "session_id": "s-1"}
        )
    )

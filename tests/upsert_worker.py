"""Helper for the concurrency test: upsert N entries into the History of a data folder."""

import sys
from pathlib import Path

from investigate import history

home, worker, count = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
for i in range(count):
    history.upsert(
        home,
        history.HistoryEntry(
            f"w{worker}-{i}", "investigation", "quick", "completed", "t", "c", None
        ),
    )

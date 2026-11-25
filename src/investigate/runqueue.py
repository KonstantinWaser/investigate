"""A first-in, first-out queue that runs at most `limit` jobs at once, each on its own thread.

It knows nothing about Runs beyond an id per job, so it is tested with plain callables. The server
owns one; the GUI never sees it.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable


class QueueClosed(RuntimeError):
    """The queue no longer takes jobs (the app is shutting down)."""


class RunQueue:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("the parallel limit must be at least 1")
        self.limit = limit
        self._cond = threading.Condition()
        self._pending: deque[tuple[str, Callable[[], None]]] = deque()
        self._running: set[str] = set()
        self._closed = False

    def submit(self, job_id: str, work: Callable[[], None]) -> None:
        """Queue `work`; it starts as soon as a slot is free. `work` must handle its own errors."""
        with self._cond:
            if self._closed:
                raise QueueClosed("shutting down")
            self._pending.append((job_id, work))
            self._dispatch()

    def set_limit(self, limit: int) -> None:
        """Change the parallel limit while running. Raising it starts waiting jobs at once;
        lowering it lets the running ones finish and holds the next ones back."""
        if limit < 1:
            raise ValueError("the parallel limit must be at least 1")
        with self._cond:
            self.limit = limit
            self._dispatch()

    def _dispatch(self) -> None:
        # Caller holds the lock.
        while self._pending and len(self._running) < self.limit:
            job_id, work = self._pending.popleft()
            self._running.add(job_id)
            threading.Thread(target=self._run, args=(job_id, work), daemon=True).start()

    def _run(self, job_id: str, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception:  # a job reports its own failures; the slot is freed regardless
            pass
        finally:
            with self._cond:
                self._running.discard(job_id)
                self._dispatch()
                self._cond.notify_all()

    def running_ids(self) -> set[str]:
        with self._cond:
            return set(self._running)

    def queued_ids(self) -> list[str]:
        with self._cond:
            return [job_id for job_id, _ in self._pending]

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until nothing is running or queued; False when the timeout ran out first."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._running and not self._pending, timeout)

    def close(self) -> list[str]:
        """Refuse new jobs and drop the ones not started. Returns the dropped ids."""
        with self._cond:
            self._closed = True
            dropped = [job_id for job_id, _ in self._pending]
            self._pending.clear()
            self._cond.notify_all()
            return dropped

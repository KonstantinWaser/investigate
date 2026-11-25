"""The tray's own inbox timer: scheduled checks, catch-up after a missed slot, check on demand.

No OS scheduler entry is involved. The scheduler wakes up every `poll_seconds`, asks whether a
scheduled slot has passed since the last recorded check, and runs the check if so. Right after it
starts, that same question catches up a slot missed while the app was not running. The clock is
injected, so the logic is tested without waiting. Only one check runs at a time.

A check that asks to be retried (offline, say) is retried after `retry_minutes`, then twice as
late, and so on, but never later than the next scheduled slot.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime, time, timedelta, tzinfo

from investigate.inbox import DEFAULT_RETRY_MINUTES, InboxState

# Called as run_check(manual). True: done, record the time. False: try again after a while.
CheckFn = Callable[[bool], bool]


def _local_now() -> datetime:
    return datetime.now().astimezone()


class InboxScheduler:
    def __init__(
        self,
        state: InboxState,
        times: Sequence[time],
        run_check: CheckFn,
        *,
        clock: Callable[[], datetime] = _local_now,
        poll_seconds: float = 30.0,
        retry_minutes: float = DEFAULT_RETRY_MINUTES,
        tz: tzinfo | None = None,
    ) -> None:
        if not times:
            raise ValueError("at least one check time is needed")
        self._state = state
        self._times = tuple(sorted(times))
        self._run_check = run_check
        self._clock = clock
        self._poll = poll_seconds
        self._retry = timedelta(minutes=retry_minutes)
        self._tz = tz  # None: the machine's local zone, DST included
        self._retry_at: datetime | None = None
        self._failures = 0  # consecutive checks that asked for a retry
        self._remembered: datetime | None = (
            None  # last check, when the state file cannot be written
        )
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # the schedule

    def _slot(self, day, at: time) -> datetime:
        """One scheduled moment, in the zone's own offset for that day (DST changes it)."""
        if self._tz is not None:
            return datetime.combine(day, at, tzinfo=self._tz)
        return datetime.combine(day, at).astimezone()  # a naive datetime is read as local time

    def _days(self, now: datetime) -> list:
        today = (now.astimezone(self._tz) if self._tz else now.astimezone()).date()
        return [today + timedelta(days=offset) for offset in (-1, 0, 1)]

    def latest_slot(self, now: datetime) -> datetime:
        """The most recent scheduled moment at or before `now`."""
        slots = [self._slot(day, t) for day in self._days(now) for t in self._times]
        return max(s for s in slots if s <= now)

    def next_slot(self, now: datetime) -> datetime:
        """The first scheduled moment after `now`."""
        slots = [self._slot(day, t) for day in self._days(now) for t in self._times]
        return min(s for s in slots if s > now)

    def is_due(self, now: datetime) -> bool:
        """A slot has passed since the last check (or there has never been a check)."""
        try:
            last = self._state.last_check()
        except Exception:
            last = None
        if self._remembered is not None and (last is None or self._remembered > last):
            last = self._remembered
        return last is None or last < self.latest_slot(now)

    # running checks

    @property
    def checking(self) -> bool:
        return self._busy.locked()

    def tick(self) -> bool:
        """Run a scheduled check now if one is due. True when a check ran."""
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return False
        if not self.is_due(now):
            return False
        return self._run(manual=False)

    def check_now(self) -> bool:
        """Run a check right now, on this thread. False when one is already running."""
        return self._run(manual=True)

    def check_now_in_background(self) -> bool:
        """Start a check on its own thread. False when one is already running."""
        if self.checking:
            return False
        threading.Thread(target=self.check_now, daemon=True).start()
        return True

    def _run(self, *, manual: bool) -> bool:
        if not self._busy.acquire(blocking=False):
            return False
        try:
            try:
                done = self._run_check(manual)
            except Exception:  # a broken check must not kill the timer thread
                done = False
            now = self._clock()
            if done:
                self._retry_at, self._failures = None, 0
                self._remembered = now
                try:
                    self._state.record(now)
                except Exception:  # an unwritable state file must not repeat the check every poll
                    pass
            else:
                delay = self._retry * (2**self._failures)
                self._failures = min(self._failures + 1, 20)
                self._retry_at = min(now + delay, self.next_slot(now))
        finally:
            self._busy.release()
        return True

    # the thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()  # the first pass is the catch-up after a missed slot
            except Exception:  # the timer outlives any single failure
                pass
            self._stop.wait(self._poll)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

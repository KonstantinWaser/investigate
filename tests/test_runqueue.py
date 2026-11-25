"""The parallel limit, with plain callables and no server."""

from __future__ import annotations

import threading

import pytest

from investigate.runqueue import QueueClosed, RunQueue


class Gate:
    """A job that reports it started and then waits to be released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        assert self.release.wait(10)


def test_never_more_than_the_limit_run_at_once_and_the_rest_wait_in_order():
    queue = RunQueue(2)
    gates = [Gate() for _ in range(4)]
    for i, gate in enumerate(gates):
        queue.submit(f"r{i}", gate)
    assert gates[0].started.wait(5) and gates[1].started.wait(5)
    assert queue.running_ids() == {"r0", "r1"}
    assert queue.queued_ids() == ["r2", "r3"]
    assert not gates[2].started.is_set()

    gates[0].release.set()
    assert gates[2].started.wait(5)  # first in, first out
    assert not gates[3].started.is_set()
    assert queue.running_ids() == {"r1", "r2"}

    for gate in gates:
        gate.release.set()
    assert queue.wait_idle(5)
    assert queue.running_ids() == set() and queue.queued_ids() == []


def test_a_job_that_raises_frees_its_slot():
    queue = RunQueue(1)

    def bad() -> None:
        raise RuntimeError("boom")

    queue.submit("bad", bad)
    after = Gate()
    after.release.set()
    queue.submit("after", after)
    assert after.started.wait(5)
    assert queue.wait_idle(5)


def test_close_drops_queued_jobs_returns_their_ids_and_refuses_new_ones():
    queue = RunQueue(1)
    first, second = Gate(), Gate()
    queue.submit("first", first)
    queue.submit("second", second)
    assert first.started.wait(5)

    assert queue.close() == ["second"]
    with pytest.raises(QueueClosed):
        queue.submit("third", Gate())
    first.release.set()
    assert queue.wait_idle(5)
    assert not second.started.is_set()


def test_limit_below_one_is_refused():
    with pytest.raises(ValueError):
        RunQueue(0)


def test_wait_idle_times_out_while_a_job_runs():
    queue = RunQueue(1)
    gate = Gate()
    queue.submit("a", gate)
    assert gate.started.wait(5)
    assert not queue.wait_idle(0.05)
    gate.release.set()
    assert queue.wait_idle(5)

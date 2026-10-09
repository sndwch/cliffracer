"""Stopping a timer lets a run that is already under way finish, within a grace period.

`Timer.stop()` set the stop event and then cancelled the task at once, so a cron job that was in
the middle of its work when the service stopped got a `CancelledError` in the middle of it, and a
scheduler for a daily report or a billing run wants "finish the current run, then stop", bounded by
the shutdown budget. The service now stops its timers with `shutdown_timeout` as the grace, all
at once. A timer that is only waiting for its next firing has nothing in flight and stops at once.
`Timer.stop()` with no argument still cancels immediately.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit


class Job:
    """A host with one slow method; records how each run ended."""

    def __init__(self, seconds: float = 0.3) -> None:
        self.seconds = seconds
        self.started = asyncio.Event()
        self.finished = 0
        self.cancelled = 0

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.sleep(self.seconds)
            self.finished += 1
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def _running_timer(job: Job, *, interval: float = 30.0, eager: bool = True) -> Timer:
    timer = Timer(interval=interval, eager=eager)
    timer.method_name = "run"
    await timer.start(job)
    return timer


async def test_a_run_in_flight_finishes_when_the_grace_covers_it():
    job = Job(0.3)
    timer = await _running_timer(job)
    await asyncio.wait_for(job.started.wait(), 2)

    started = time.monotonic()
    await timer.stop(grace=2.0)

    assert (job.finished, job.cancelled) == (1, 0)
    # Upper bound. CI p99 0.301 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 520x the overshoot; below 2 s (grace=2.0 waited out).
    # Lower bound: half the 0.3 s run; a stop that cancelled it at once falls under it. Load can
    # only lengthen it.
    assert 0.15 < time.monotonic() - started < 1.0


async def test_a_run_that_outlasts_the_grace_is_cancelled_when_the_grace_ends():
    job = Job(5.0)
    timer = await _running_timer(job)
    await asyncio.wait_for(job.started.wait(), 2)

    started = time.monotonic()
    await timer.stop(grace=0.15)

    assert (job.finished, job.cancelled) == (0, 1)
    # Upper bound. CI p99 0.151 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.15 s,
    # 839x the overshoot; below 5 s (Job(5.0) waited for).
    # Lower bound: two thirds of the 0.15 s grace; a stop that gave no grace falls under it. Load
    # can only lengthen it.
    assert 0.1 < time.monotonic() - started < 1.0


async def test_no_grace_cancels_at_once_as_stop_always_did():
    job = Job(5.0)
    timer = await _running_timer(job)
    await asyncio.wait_for(job.started.wait(), 2)

    started = time.monotonic()
    await timer.stop()

    assert (job.finished, job.cancelled) == (0, 1)
    # Upper bound. CI p99 0.000231 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 2164x
    # p99.
    assert time.monotonic() - started < 0.5


async def test_no_deadline_waits_for_the_run_however_long_it_takes():
    job = Job(0.4)
    timer = await _running_timer(job)
    await asyncio.wait_for(job.started.wait(), 2)

    await timer.stop(grace=None)

    assert (job.finished, job.cancelled) == (1, 0)


async def test_a_timer_between_runs_has_nothing_in_flight_and_stops_at_once():
    job = Job(0.3)
    timer = await _running_timer(job, interval=30.0, eager=False)
    await asyncio.sleep(0.05)

    started = time.monotonic()
    await timer.stop(grace=5.0)

    # Upper bound. CI p99 0.000477 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 1048x
    # p99.
    assert time.monotonic() - started < 0.5
    assert (job.finished, job.cancelled) == (0, 0)
    assert not job.started.is_set()


async def test_no_further_run_starts_once_the_one_in_flight_has_finished():
    job = Job(0.2)
    timer = await _running_timer(job, interval=0.01)
    await asyncio.wait_for(job.started.wait(), 2)

    await timer.stop(grace=2.0)
    ran = timer.execution_count
    await asyncio.sleep(0.3)

    assert timer.execution_count == ran
    assert job.finished == ran


class Host(CliffracerService):
    """A service with no broker: the steps that dial are stubbed, the timers are real."""

    def __init__(self, shutdown_timeout: float | None, seconds: float) -> None:
        super().__init__(
            ServiceConfig(name="timer_host", health_port=0, shutdown_timeout=shutdown_timeout)
        )
        self.jobs = [Job(seconds), Job(seconds)]

    async def job_a(self) -> None:
        await self.jobs[0].run()

    async def job_b(self) -> None:
        await self.jobs[1].run()


async def _service_with_two_runs_in_flight(
    shutdown_timeout: float | None, seconds: float = 0.3
) -> Host:
    host = Host(shutdown_timeout, seconds)
    for name in ("job_a", "job_b"):
        timer = Timer(interval=30.0, eager=True)
        timer.method_name = name
        host.container.registry.timers.append(timer)
    await host.container._start_timers()
    for job in host.jobs:
        await asyncio.wait_for(job.started.wait(), 2)
    return host


async def test_the_service_gives_its_timers_the_shutdown_timeout_all_at_once():
    host = await _service_with_two_runs_in_flight(shutdown_timeout=2.0)

    started = time.monotonic()
    await host.container._stop_timers()

    assert [(job.finished, job.cancelled) for job in host.jobs] == [(1, 0), (1, 0)]
    # Upper bound. CI p99 0.302 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 163x the overshoot; below 0.6 s (the two runs stopped in turn).
    assert time.monotonic() - started < 0.55, "the timers were stopped one after another"


async def test_the_service_cancels_a_run_that_outlasts_the_shutdown_timeout():
    host = await _service_with_two_runs_in_flight(shutdown_timeout=0.1, seconds=5.0)

    await host.container._stop_timers()

    assert [(job.finished, job.cancelled) for job in host.jobs] == [(0, 1), (0, 1)]


async def test_no_shutdown_timeout_means_no_deadline_and_waits_for_the_runs_as_the_drain_does():
    host = await _service_with_two_runs_in_flight(shutdown_timeout=None)

    await host.container._stop_timers()

    assert [(job.finished, job.cancelled) for job in host.jobs] == [(1, 0), (1, 0)]


async def test_a_timer_in_its_error_backoff_has_nothing_in_flight_and_stops_at_once():
    """A timer in its backoff has no run in flight, so a stop cancels it at once and does not wait
    out the grace, whatever the backoff would have done with the stop event."""
    job = Job(0.01)
    timer = Timer(interval=30.0, eager=True, error_backoff=30.0)
    timer.method_name = "run"
    real = timer._execute_method

    async def run_then_fail():
        await real()  # the real run sets and clears the in-flight flag
        raise RuntimeError("the firing failed outside the handler")

    timer._execute_method = run_then_fail  # type: ignore[method-assign]
    await timer.start(job)
    await asyncio.wait_for(job.started.wait(), 2)
    await asyncio.sleep(0.1)  # the run is done and the loop is in its 30s backoff
    assert job.finished == 1

    started = time.monotonic()
    await timer.stop(grace=5.0)

    # Upper bound. CI p99 0.00061 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 1640x p99.
    assert time.monotonic() - started < 1.0, "stop waited out the grace for a timer that was asleep"


async def test_stuck_runs_cost_the_service_one_grace_between_them_not_one_each():
    host = await _service_with_two_runs_in_flight(shutdown_timeout=0.4, seconds=5.0)

    started = time.monotonic()
    await host.container._stop_timers()

    elapsed = time.monotonic() - started
    assert [(job.finished, job.cancelled) for job in host.jobs] == [(0, 1), (0, 1)]
    # Upper bound. CI p99 0.401 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.4 s,
    # 228x the overshoot; below 0.8 s (one grace per run).
    assert elapsed < 0.7, f"{elapsed:.2f}s: the timers were stopped one grace after another"

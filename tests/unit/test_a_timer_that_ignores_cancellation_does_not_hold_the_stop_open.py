"""A timer run that catches its cancellation cannot make `stop()` last as long as the run.

`Timer.stop` cancelled a run that outlived its grace and then awaited the task with no deadline.
A run that swallows `CancelledError` (a retry loop, `except BaseException`, a library) kept the
stop waiting for as long as it ran, with nothing logged and nothing in `active_tasks`, and the
health listener, subscriptions, drain, `on_shutdown`, extensions and disconnect behind it did not
start. The documentation bounds a shutdown at three `shutdown_timeout` periods.

The service now hands a cancelled run that has not finished to the lifecycle, so the drain waits
for it, cancels it again, names it at error level and leaves it in `active_tasks` as it does any
task that refuses to stop. The stop ends inside the documented bound with the run still going.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit

SHUTDOWN_TIMEOUT = 0.3
# Three periods are the documented worst case; the slack is for a loaded host.
BOUND = 3 * SHUTDOWN_TIMEOUT + 0.6


class Stubborn(CliffracerService):
    """A timer whose run swallows cancellation until `release` is set."""

    def __init__(self, release: asyncio.Event, running: asyncio.Event, cancels: list[int]):
        super().__init__(
            ServiceConfig(name="stubborn", health_listener=False, shutdown_timeout=SHUTDOWN_TIMEOUT)
        )
        self.release, self.running, self.cancels = release, running, cancels

    @timer(interval=0.05, eager=True)
    async def tick(self) -> None:
        self.running.set()
        while not self.release.is_set():
            try:
                await asyncio.wait_for(self.release.wait(), 0.02)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                self.cancels.append(1)  # swallowed: the run carries on


def _connected(svc):
    nc = AsyncMock()
    nc.is_connected, nc.is_closed, nc.is_draining = True, False, False

    async def connect() -> None:
        svc.container.connection.nc = nc

    async def disconnect() -> None:
        svc.container.connection.nc = None

    svc.container.connection.connect = connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = disconnect  # type: ignore[method-assign]
    svc.container.setup_subscriptions = AsyncMock()  # type: ignore[method-assign]
    return svc


async def _stubborn_service():
    release, running, cancels = asyncio.Event(), asyncio.Event(), []
    svc = _connected(Stubborn(release, running, cancels))
    await svc.start()
    await asyncio.wait_for(running.wait(), 2)
    return svc, release, cancels


async def test_a_run_that_swallows_cancellation_does_not_hold_the_stop_open():
    svc, release, cancels = await _stubborn_service()
    asyncio.get_running_loop().call_later(2.5, release.set)  # the run ends on its own, late

    started = time.monotonic()
    await svc.stop()
    elapsed = time.monotonic() - started

    try:
        # Upper bound. CI p99 0.907 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.9
        # s, 89x the overshoot; below 2.5 s (the stubborn run's own end at 2.5).
        assert elapsed < BOUND, (
            f"stop() took {elapsed:.2f}s; the documented worst case is "
            f"{3 * SHUTDOWN_TIMEOUT:.1f}s ({SHUTDOWN_TIMEOUT}s x 3)"
        )
        assert cancels, "the run was never cancelled, so it did not refuse anything"
    finally:
        release.set()
        await asyncio.sleep(0.1)


async def test_the_run_that_refused_is_named_at_error_level_and_stays_in_active_tasks():
    svc, release, _ = await _stubborn_service()
    asyncio.get_running_loop().call_later(2.5, release.set)  # a bug would hold the stop till then
    records: list[str] = []
    sink = logger.add(lambda m: records.append(m.record["level"].name + " " + m.record["message"]))
    try:
        await asyncio.wait_for(svc.stop(), 10)
        errors = [r for r in records if r.startswith("ERROR") and "did not stop" in r]
        assert errors and "timer:tick" in errors[0], records
        assert any(t.get_name() == "timer:tick" for t in svc.container.lifecycle.active_tasks)
    finally:
        logger.remove(sink)
        release.set()

    await asyncio.sleep(0.2)
    assert not svc.container.lifecycle.active_tasks, "the run finished, so it is no longer active"


async def test_CONTROL_a_run_that_honours_cancellation_leaves_nothing_behind():
    svc = _connected(CooperativeService())
    await svc.start()
    await asyncio.sleep(0.15)

    started = time.monotonic()
    await svc.stop()

    # Upper bound. CI p99 0.302 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 5x p99.
    assert time.monotonic() - started < BOUND
    assert not svc.container.lifecycle.active_tasks
    assert all(t.task is not None and t.task.done() for t in svc.container.registry.timers)


class CooperativeService(CliffracerService):
    def __init__(self) -> None:
        super().__init__(
            ServiceConfig(
                name="cooperative", health_listener=False, shutdown_timeout=SHUTDOWN_TIMEOUT
            )
        )

    @timer(interval=0.05, eager=True)
    async def tick(self) -> None:
        await asyncio.sleep(30)


async def test_CONTROL_a_timer_stopped_on_its_own_still_waits_for_the_cancelled_run():
    """Without a hand-over, `Timer.stop` returns when the task has finished, as it always did."""
    done: list[int] = []

    class Host:
        async def run(self) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)  # cleanup that takes a moment
                done.append(1)
                raise

    t = Timer(interval=30.0, eager=True)
    t.method_name = "run"
    await t.start(Host())
    await asyncio.sleep(0.05)

    await t.stop()

    assert done == [1]
    assert t.task is not None and t.task.done()

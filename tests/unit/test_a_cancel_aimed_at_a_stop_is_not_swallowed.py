"""A cancellation aimed at a stop reaches it, whatever the stop is waiting on.

`LifecycleManager.stop()` waits for a start that is in flight, after cancelling it, with
`await start_task` inside `except (CancelledError, Exception): pass`. The handler is there to
absorb the START task's cancellation. A `CancelledError` delivered to `stop()` itself while it
waited was the same exception and was dropped as well, so `stop()` ran to completion,
`task.cancel()` had no effect, and `asyncio.timeout()` around it never fired. `Timer.stop` awaited
its cancelled task under the same shape of handler. Both now wait without awaiting the other task,
so the start's or the run's own outcome is read and discarded, and the caller's cancel propagates.
"""

from __future__ import annotations

import asyncio
import gc
import time
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit

CLEANUP_SECONDS = 1.5


class SlowStop(Extension):
    """An extension whose stop (the abortive cleanup of a cancelled start) takes a while."""

    async def stop(self) -> None:
        await asyncio.sleep(CLEANUP_SECONDS)


class Starting(CliffracerService):
    slow = SlowStop()

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="starting", health_listener=False))
        self.in_startup = asyncio.Event()

    async def on_startup(self) -> None:
        self.in_startup.set()
        await asyncio.sleep(3600)


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


async def _stop_waiting_for_a_cancelled_start():
    svc = _connected(Starting())
    starter = asyncio.create_task(svc.start())
    await asyncio.wait_for(svc.in_startup.wait(), 2)
    stopper = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.3)  # stop() is now waiting for the cancelled start's cleanup
    assert not stopper.done()
    return svc, starter, stopper


async def test_a_cancel_aimed_at_stop_while_it_waits_for_a_start_propagates():
    svc, starter, stopper = await _stop_waiting_for_a_cancelled_start()
    started = time.monotonic()

    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(stopper), CLEANUP_SECONDS + 5)

    # Upper bound. CI p99 0.000254 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 5896x
    # p99.
    assert time.monotonic() - started < CLEANUP_SECONDS, "the stop ran to the end of the cleanup"
    assert stopper.cancelled()
    starter.cancel()
    await asyncio.gather(starter, return_exceptions=True)
    await asyncio.sleep(CLEANUP_SECONDS)  # let the cancelled start's own cleanup finish


async def test_a_timeout_around_stop_fires_while_it_waits_for_a_start():
    svc = _connected(Starting())
    starter = asyncio.create_task(svc.start())
    await asyncio.wait_for(svc.in_startup.wait(), 2)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.4):
                await svc.stop()
        # Upper bound. CI p99 0.401 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.4
        # s, 1242x the overshoot.
        assert time.monotonic() - started < CLEANUP_SECONDS
    finally:
        starter.cancel()
        await asyncio.gather(starter, return_exceptions=True)
        await asyncio.sleep(CLEANUP_SECONDS)


async def test_CONTROL_a_stop_that_is_not_cancelled_still_waits_for_the_start_and_returns():
    svc, starter, stopper = await _stop_waiting_for_a_cancelled_start()

    await asyncio.wait_for(stopper, CLEANUP_SECONDS + 5)

    assert svc.container.lifecycle.is_stopped
    await asyncio.gather(starter, return_exceptions=True)


class FailingCleanup(Starting):
    """A start whose cleanup of the cancel fails, so the start task ends in an exception."""

    async def on_startup(self) -> None:
        self.in_startup.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None


async def test_a_start_that_ends_in_an_exception_is_not_reported_as_never_retrieved():
    loop = asyncio.get_running_loop()
    messages: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: messages.append(context["message"]))
    try:
        svc = _connected(FailingCleanup())
        starter = asyncio.create_task(svc.start())
        await asyncio.wait_for(svc.in_startup.wait(), 2)

        await asyncio.wait_for(svc.stop(), 5)

        assert starter.done() and not starter.cancelled()
        del starter
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert "Task exception was never retrieved" not in messages


async def test_a_cancel_aimed_at_timer_stop_while_it_waits_for_a_run_propagates():
    class Host:
        async def run(self) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(CLEANUP_SECONDS)  # cleanup that takes a while
                raise

    timer = Timer(interval=30.0, eager=True)
    timer.method_name = "run"
    await timer.start(Host())
    await asyncio.sleep(0.1)

    stopper = asyncio.create_task(timer.stop())
    await asyncio.sleep(0.3)  # waiting for the cancelled run's cleanup
    assert not stopper.done()
    started = time.monotonic()
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(stopper), CLEANUP_SECONDS + 5)

    # Upper bound. CI p99 0.000314 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 4783x
    # p99.
    assert time.monotonic() - started < CLEANUP_SECONDS
    assert timer.task is not None
    await asyncio.wait({timer.task})

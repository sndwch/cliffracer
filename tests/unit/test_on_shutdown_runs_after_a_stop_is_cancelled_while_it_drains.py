"""`on_shutdown` still runs when the stop is cancelled before it got there.

A service's `on_shutdown` is where it releases what `on_startup` built. The stop ran timers, the
health listener, subscriptions, the task drain and then `on_shutdown` inside one block that caught a
cancellation once for all of them, so a cancel that landed during the drain jumped past
`on_shutdown`: it never ran, the shielded steps after it did, the service was marked stopped, and a
later `stop()` returned at once. The closed-connection callback cancels its stop at a fixed bound, so
a service whose drain needs longer than that lost `on_shutdown` every time.

Now `on_shutdown` runs after such a cancel, before the extensions stop and the connection is
disconnected. It is bounded by `shutdown_timeout`, so a hung `on_shutdown` cannot hold the stop
open, and a further cancel while it runs does not abandon it. It runs once: not again when the stop
is cancelled during it, and not on a later `stop()`.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, SharedDependency

pytestmark = pytest.mark.unit


class Order(Extension):
    def __init__(self, log: list[str]):
        self.log = log

    async def stop(self):
        self.log.append("extensions.stop")


def _service(log: list[str], *, shutdown_timeout: float | None = 5.0, on_shutdown=None):
    class Svc(CliffracerService):
        ext = Order(SharedDependency(log))

        async def on_shutdown(self) -> None:
            log.append("on_shutdown.begin")
            if on_shutdown is not None:
                await on_shutdown(self)
            log.append("on_shutdown.end")

    svc = Svc(
        ServiceConfig(
            name="cancelled_stop",
            health_port=0,
            health_listener=False,
            shutdown_timeout=shutdown_timeout,
        )
    )
    nc = AsyncMock()
    nc.is_connected, nc.is_closed, nc.is_draining = True, False, False

    async def connect() -> None:
        svc.container.connection.nc = nc

    async def disconnect() -> None:
        log.append("disconnect")
        svc.container.connection.nc = None

    svc.container.connection.connect = connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = disconnect  # type: ignore[method-assign]
    svc.container.setup_subscriptions = AsyncMock()  # type: ignore[method-assign]
    return svc


async def _started_with_a_long_task(svc) -> asyncio.Event:
    await svc.start()
    running = asyncio.Event()

    async def long_task() -> None:
        running.set()
        await asyncio.sleep(3600)

    svc.container.lifecycle.spawn_supervised_task(long_task(), name="long")
    await running.wait()
    return running


async def _cancel_the_stop_during_the_drain(svc) -> asyncio.Task:
    stop = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.2)  # inside the drain: the long task is still running
    assert not stop.done()
    stop.cancel()
    return stop


async def test_on_shutdown_runs_once_after_a_cancel_during_the_drain_and_before_the_rest():
    log: list[str] = []
    svc = _service(log)
    await _started_with_a_long_task(svc)

    stop = await _cancel_the_stop_during_the_drain(svc)
    with pytest.raises(asyncio.CancelledError):
        await stop

    assert log.count("on_shutdown.begin") == 1 and log.count("on_shutdown.end") == 1, log
    assert log.index("on_shutdown.end") < log.index("extensions.stop") < log.index("disconnect"), (
        log
    )


async def test_a_later_stop_does_not_run_on_shutdown_again():
    log: list[str] = []
    svc = _service(log)
    await _started_with_a_long_task(svc)
    stop = await _cancel_the_stop_during_the_drain(svc)
    with pytest.raises(asyncio.CancelledError):
        await stop

    await svc.stop()

    assert log.count("on_shutdown.begin") == 1, log


async def test_a_second_cancel_while_on_shutdown_runs_does_not_abandon_it():
    log: list[str] = []
    in_on_shutdown = asyncio.Event()

    async def slow_on_shutdown(_svc) -> None:
        in_on_shutdown.set()
        await asyncio.sleep(0.4)

    svc = _service(log, on_shutdown=slow_on_shutdown)
    await _started_with_a_long_task(svc)
    stop = await _cancel_the_stop_during_the_drain(svc)
    await asyncio.wait_for(in_on_shutdown.wait(), timeout=3)

    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop

    assert "on_shutdown.end" in log, f"the second cancel abandoned on_shutdown: {log}"
    assert log.index("on_shutdown.end") < log.index("extensions.stop") < log.index("disconnect"), (
        log
    )


async def test_a_hung_on_shutdown_is_abandoned_at_shutdown_timeout_and_logged():
    log: list[str] = []

    async def hangs(_svc) -> None:
        await asyncio.Event().wait()

    svc = _service(log, shutdown_timeout=0.3, on_shutdown=hangs)
    await _started_with_a_long_task(svc)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        stop = await _cancel_the_stop_during_the_drain(svc)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, timeout=5)
    finally:
        logger.remove(sink)

    assert "on_shutdown.end" not in log
    assert log.index("extensions.stop") < log.index("disconnect"), log
    assert any("on_shutdown" in line and "0.3" in line and "abandoned" in line for line in lines), (
        lines
    )


async def test_an_on_shutdown_that_raises_after_the_cancel_is_logged_and_the_rest_still_runs():
    log: list[str] = []

    async def breaks(_svc) -> None:
        raise RuntimeError("pool already closed")

    svc = _service(log, on_shutdown=breaks)
    await _started_with_a_long_task(svc)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    try:
        stop = await _cancel_the_stop_during_the_drain(svc)
        with pytest.raises(asyncio.CancelledError):
            await stop
    finally:
        logger.remove(sink)

    assert "on_shutdown.begin" in log, f"on_shutdown never ran: {log}"
    assert "on_shutdown.end" not in log
    assert log.index("extensions.stop") < log.index("disconnect"), log
    assert any("on_shutdown" in line and "pool already closed" in line for line in lines), lines


async def test_a_stop_cancelled_during_on_shutdown_itself_does_not_run_it_a_second_time():
    log: list[str] = []
    in_on_shutdown = asyncio.Event()

    async def waits(_svc) -> None:
        in_on_shutdown.set()
        await asyncio.sleep(3600)

    svc = _service(log, shutdown_timeout=None, on_shutdown=waits)
    await svc.start()
    stop = asyncio.create_task(svc.stop())
    await asyncio.wait_for(in_on_shutdown.wait(), timeout=3)

    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop

    assert log.count("on_shutdown.begin") == 1, log


async def test_CONTROL_an_uncancelled_stop_runs_on_shutdown_once_in_the_same_place():
    log: list[str] = []
    svc = _service(log, shutdown_timeout=0.2)
    await svc.start()

    await svc.stop()

    assert log.count("on_shutdown.begin") == 1
    assert log.index("on_shutdown.end") < log.index("extensions.stop") < log.index("disconnect"), (
        log
    )


async def test_with_no_shutdown_timeout_a_hung_on_shutdown_still_lets_the_stop_end(monkeypatch):
    """`shutdown_timeout=None` sets no deadline for the drain, not for a hook run after a cancel.

    A stop that swallows every further cancel and has no bound can never be ended, and the
    closed-connection callback waits for it. The hook gets a fixed ceiling when there is no setting.
    """
    from cliffracer.core import lifecycle

    monkeypatch.setattr(lifecycle, "ON_SHUTDOWN_CEILING", 0.3, raising=False)
    log: list[str] = []

    async def hangs(_svc) -> None:
        await asyncio.Event().wait()

    svc = _service(log, shutdown_timeout=None, on_shutdown=hangs)
    await _started_with_a_long_task(svc)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        stop = await _cancel_the_stop_during_the_drain(svc)
        for _ in range(5):
            await asyncio.sleep(0.02)
            stop.cancel()
        done, _ = await asyncio.wait({stop}, timeout=3)
    finally:
        logger.remove(sink)

    assert done, (
        "a stop with no shutdown_timeout and a hung on_shutdown never ended, whatever cancelled it"
    )
    assert "on_shutdown.end" not in log
    assert log.index("extensions.stop") < log.index("disconnect"), log
    assert any("on_shutdown" in line and "0.3" in line and "abandoned" in line for line in lines), (
        lines
    )


async def test_the_closed_connection_callback_returns_when_there_is_no_shutdown_timeout(
    monkeypatch,
):
    """The shape the blocker was found in: nats-py awaits this callback inside its own close."""
    from cliffracer.core import connection, lifecycle

    monkeypatch.setattr(connection, "_CLOSED_STOP_TIMEOUT", 0.1)
    monkeypatch.setattr(lifecycle, "ON_SHUTDOWN_CEILING", 0.3, raising=False)
    log: list[str] = []

    async def hangs(_svc) -> None:
        await asyncio.Event().wait()

    svc = _service(log, shutdown_timeout=None, on_shutdown=hangs)
    await _started_with_a_long_task(svc)
    svc.nc = svc.container.connection.nc
    svc._running = True

    returned = asyncio.create_task(svc.container.connection._closed_callback())
    done, _ = await asyncio.wait({returned}, timeout=3)

    assert done, "the close callback never returned"
    assert log.count("on_shutdown.begin") == 1, log

"""A stop that is cancelled while it waits on a timer still releases the health port and the intake.

The stop ran the timers, the health listener, the subscriptions and the drain inside one block that
caught a cancellation once for all of them. A cancel that landed while it waited on a timer's run
(the closed-connection callback cancels its stop at a fixed ten seconds, and a run in flight gets
`shutdown_timeout`, thirty by default) jumped past the health-listener stop and the subscription
cancel. The service then reported stopped with its HTTP listener still accepting, its subscriptions
still delivering, and nothing left to run them, because that stop marks the service stopped. A
replacement on the same `health_port` could not bind.

The two quick steps now run after such a cancel, before `on_shutdown`, the extensions and the
disconnect. The drain does not: a cancel asks the stop to stop waiting for work in flight.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, timer

pytestmark = pytest.mark.unit


class Slow(CliffracerService):
    """One timer whose run is in flight for a long time but stops when cancelled."""

    def __init__(self, order: list[str], shutdown_timeout: float | None = 30.0) -> None:
        super().__init__(
            ServiceConfig(name="slow_stop", health_port=0, shutdown_timeout=shutdown_timeout)
        )
        self.order = order
        self.running = asyncio.Event()

    @timer(interval=30.0, eager=True)
    async def tick(self) -> None:
        self.running.set()
        await asyncio.sleep(3600)

    async def on_shutdown(self) -> None:
        self.order.append("on_shutdown")


def _connected(svc, order: list[str]):
    nc = AsyncMock()
    nc.is_connected, nc.is_closed, nc.is_draining = True, False, False

    async def connect() -> None:
        svc.container.connection.nc = nc

    async def disconnect() -> None:
        order.append("disconnect")
        svc.container.connection.nc = None

    svc.container.connection.connect = connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = disconnect  # type: ignore[method-assign]
    svc.container.setup_subscriptions = AsyncMock()  # type: ignore[method-assign]

    cancel_subscriptions = svc.container.connection.unsubscribe_all

    async def recording_cancel_subscriptions() -> None:
        order.append("cancel_subscriptions")
        await cancel_subscriptions()

    svc.container.connection.unsubscribe_all = recording_cancel_subscriptions  # type: ignore[method-assign]
    stop_health_listener = svc.health_listener.stop

    async def recording_stop_health_listener() -> None:
        order.append("stop_health_listener")
        await stop_health_listener()

    svc.health_listener.stop = recording_stop_health_listener  # type: ignore[method-assign]
    return svc


async def _accepting(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection(host, port)
    except OSError:
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _cancelled_while_waiting_on_the_timer(shutdown_timeout: float | None = 30.0):
    order: list[str] = []
    svc = _connected(Slow(order, shutdown_timeout), order)
    await svc.start()
    await asyncio.wait_for(svc.running.wait(), 2)
    listener = svc.health_listener
    host, port = listener.host, listener.port
    assert port is not None and await _accepting(host, port), "no listener to stop"

    stop = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.2)  # inside the timer wait: the run is still in flight
    assert not stop.done()
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    return svc, order, host, port


@pytest.mark.parametrize(
    "shutdown_timeout", [30.0, 0.5, None], ids=["30s", "half-a-second", "none"]
)
async def test_the_health_listener_is_stopped_and_its_port_released(shutdown_timeout):
    """For any `shutdown_timeout`: one that waits without a bound, and one shorter than a second."""
    svc, order, host, port = await _cancelled_while_waiting_on_the_timer(shutdown_timeout)

    assert svc.health_listener.port is None, "the listener still holds its port"
    assert not await _accepting(host, port), "the port still accepts connections"
    assert order.count("stop_health_listener") == 1, order


async def test_the_subscriptions_are_cancelled():
    _, order, _, _ = await _cancelled_while_waiting_on_the_timer()

    assert order.count("cancel_subscriptions") == 1, order


async def test_the_quick_steps_run_before_on_shutdown_and_the_disconnect():
    _, order, _, _ = await _cancelled_while_waiting_on_the_timer()

    assert order.index("stop_health_listener") < order.index("on_shutdown"), order
    assert (
        order.index("cancel_subscriptions") < order.index("on_shutdown") < order.index("disconnect")
    ), order


async def test_a_later_stop_does_not_run_them_again():
    svc, order, _, _ = await _cancelled_while_waiting_on_the_timer()

    await svc.stop()

    assert order.count("stop_health_listener") == 1 and order.count("cancel_subscriptions") == 1


async def test_the_timer_run_that_was_cancelled_is_left_finished():
    svc, _, _, _ = await _cancelled_while_waiting_on_the_timer()
    await asyncio.sleep(0.1)

    assert all(t.task is None or t.task.done() for t in svc.container.registry.timers)


async def test_CONTROL_a_stop_that_is_not_cancelled_runs_each_step_once_in_order():
    order: list[str] = []
    svc = _connected(Slow(order), order)
    svc.config.shutdown_timeout = 0.1
    await svc.start()
    await asyncio.wait_for(svc.running.wait(), 2)

    await svc.stop()

    assert order == ["stop_health_listener", "cancel_subscriptions", "on_shutdown", "disconnect"]

"""Unfinished order processing cannot hold service shutdown indefinitely."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.lifecycle import LifecycleManager

pytestmark = pytest.mark.unit

GRACE = 0.05


class OrderService(CliffracerService):
    def __init__(self, mode: str, processing_timeout: float | None = None) -> None:
        super().__init__(
            ServiceConfig(
                name="orders",
                shutdown_timeout=GRACE,
                health_listener=False,
                jetstream_enabled=True,
                max_processing_time=processing_timeout,
            )
        )
        self.mode = mode
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    @listener("orders.fulfill", durable="fulfillment")
    async def fulfill(self, order_id: str) -> None:
        self.entered.set()
        while True:
            try:
                await self.release.wait()
                return
            except asyncio.CancelledError:
                self.cancelled.set()
                if self.mode == "cooperative":
                    raise
                if self.mode == "cleanup":
                    await asyncio.sleep(0)
                    return


@pytest.mark.parametrize(
    ("mode", "processing_timeout"),
    [("stubborn", None), ("stubborn", 0.02), ("cooperative", None), ("cleanup", None)],
)
async def test_shutdown_reports_only_orders_that_remain_unfinished(
    mode: str,
    processing_timeout: float | None,
) -> None:
    service = OrderService(mode, processing_timeout)
    service.nc = AsyncMock()
    service.js = AsyncMock()
    service._discover_handlers()
    message = AsyncMock()
    message.subject = "orders.fulfill"
    message.data = b'{"order_id": "order-a"}'
    message.headers = None
    message.metadata = SimpleNamespace(num_delivered=1)
    await service.container.dispatcher.make_jetstream_event_callback("orders.fulfill")(message)
    await asyncio.wait_for(service.entered.wait(), timeout=2)
    if processing_timeout is not None:
        await asyncio.wait_for(service.cancelled.wait(), timeout=2)
    manager = service.container.lifecycle
    original = set(manager.active_tasks)
    assert len(original) == 1
    task = next(iter(original))
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    shutdown = asyncio.create_task(service.stop())
    try:
        done, _ = await asyncio.wait({shutdown}, timeout=2)
        assert done, "service shutdown waited forever for unfinished order processing"
        await shutdown
        assert service.cancelled.is_set()
        assert service.container.lifecycle.is_stopped
        warnings = [r["message"] for r in records if "Shutdown timeout" in r["message"]]
        assert len(warnings) == 1
        assert "Cancelling 1 remaining task" in warnings[0]
        assert task.get_name() in warnings[0]
        errors = [r["message"] for r in records if r["level"].name == "ERROR"]
        if mode == "stubborn":
            assert task in manager.active_tasks and not task.done()
            assert any(task.get_name() in error and "did not stop" in error for error in errors)
            message.ack.assert_not_awaited()
            message.nak.assert_not_awaited()
            message.term.assert_not_awaited()
            with pytest.raises(ServiceLifecycleError, match="unfinished shutdown tasks"):
                await service.start()
            # Repeated shutdown must not grant unfinished work another grace.
            await asyncio.wait_for(manager.drain_active_tasks(timeout=10), timeout=1)
        else:
            assert not manager.active_tasks
            assert not errors
    finally:
        logger.remove(sink)
        service.mode = "cooperative"
        service.release.set()
        for active in original:
            active.cancel()
        await asyncio.gather(*original, return_exceptions=True)
        shutdown.cancel()
        await asyncio.gather(shutdown, return_exceptions=True)
    assert not manager.active_tasks


async def test_orders_spawned_during_cancellation_share_the_cleanup_deadline() -> None:
    manager = LifecycleManager(ServiceConfig(name="orders", health_listener=False))
    entered = asyncio.Event()
    release = asyncio.Event()
    spawned = []

    async def persist_receipt() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    async def fulfill() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            spawned.append(manager.spawn_supervised_task(persist_receipt(), name="persist-receipt"))
            await asyncio.sleep(0)

    order = manager.spawn_supervised_task(fulfill(), name="fulfill-order")
    await entered.wait()
    shutdown = asyncio.create_task(manager.drain_active_tasks(timeout=GRACE))
    try:
        done, _ = await asyncio.wait({shutdown}, timeout=2)
        assert done, "cleanup spawned work that extended shutdown indefinitely"
        await shutdown
        assert len(spawned) == 1
        assert order.cancelled()
        assert spawned[0].cancelling() > 0, "the receipt escaped shutdown cancellation"
        assert spawned[0] in manager.active_tasks
    finally:
        release.set()
        await asyncio.gather(order, *spawned, return_exceptions=True)
        shutdown.cancel()
        await asyncio.gather(shutdown, return_exceptions=True)


@pytest.mark.parametrize("timeout", [None, 10.0])
async def test_cancelling_shutdown_still_cancels_order_processing(timeout: float | None) -> None:
    manager = LifecycleManager(ServiceConfig(name="orders", health_listener=False))
    entered = asyncio.Event()

    async def fulfill() -> None:
        entered.set()
        await asyncio.Event().wait()

    order = manager.spawn_supervised_task(fulfill(), name="fulfill-order")
    await entered.wait()
    shutdown = asyncio.create_task(manager.drain_active_tasks(timeout=timeout))
    await asyncio.sleep(0)
    shutdown.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        await asyncio.sleep(0)
        assert order.cancelled(), "cancelling shutdown left its order processing running"
    finally:
        order.cancel()
        await asyncio.gather(order, return_exceptions=True)

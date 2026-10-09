"""Warehouse intake closes before finishing orders, even during partial startup."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("delivery", ["core", "push", "pull"])
@pytest.mark.parametrize("yield_after_start", [False, True])
async def test_warehouse_closes_every_intake_before_finishing_orders(delivery, yield_after_start):
    service, nc, handles = warehouse(delivery)
    observed = []

    async def finish_orders(*, timeout):
        observed.append([sub.unsubscribe.await_count for sub in handles])
        nc.drain.assert_not_awaited()

    service.container.lifecycle.drain_active_tasks = finish_orders
    with (
        patch("cliffracer.core.dial.connect", return_value=nc),
        patch("cliffracer.core.container.ensure_streams", new_callable=AsyncMock),
    ):
        await service.start()
        if yield_after_start:
            await asyncio.sleep(0)
        await service.stop()
        await service.stop()

    assert observed == [[1, 1, 1, 1]]
    for sub in handles:
        sub.unsubscribe.assert_awaited_once()
    nc.drain.assert_awaited_once()
    nc.close.assert_awaited_once()


@pytest.mark.parametrize("delivery", ["push", "pull"])
async def test_cancelled_consumer_inspection_closes_partial_warehouse_intake(delivery):
    service, nc, handles = warehouse(delivery)

    async def interrupted_inspection(sub, *args, **kwargs):
        raise asyncio.CancelledError

    service.container.dispatcher.report_consumer_drift = interrupted_inspection
    with (
        patch("cliffracer.core.dial.connect", return_value=nc),
        patch("cliffracer.core.container.ensure_streams", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await service.start()

    assert len(handles) == 4
    for sub in handles:
        sub.unsubscribe.assert_awaited_once()
    nc.close.assert_awaited_once()
    assert not service.container.is_running


def warehouse(delivery):
    class Warehouse(CliffracerService):
        @listener(
            "orders.placed",
            durable="warehouse" if delivery != "core" else None,
            pull=delivery == "pull",
            fanout=delivery == "core",
        )
        async def receive_order(self, subject: str) -> None:
            pass

    service = Warehouse(
        ServiceConfig(
            name="warehouse",
            health_port=0,
            jetstream_enabled=delivery != "core",
            jetstream_streams=[StreamSpec(name="orders", subjects=["orders.>", "dlq.>"])],
        )
    )
    nc = AsyncMock()
    nc.is_connected = True
    nc.is_closed = nc.is_connecting = nc.is_reconnecting = nc.is_draining = False
    js = AsyncMock()
    nc.jetstream = lambda: js
    handles = []

    async def fetch(*args, **kwargs):
        await asyncio.Future()

    async def subscribe(*args, **kwargs):
        sub = AsyncMock()
        sub.fetch = fetch
        handles.append(sub)
        return sub

    nc.subscribe = js.subscribe = js.pull_subscribe = subscribe
    return service, nc, handles

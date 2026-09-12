"""Overlapping listener single execution on live NATS broker."""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener

pytestmark = pytest.mark.integration


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_overlapping_listeners_no_duplication():
    wildcard_received = []
    created_received = []

    class OrderTrackingService(CliffracerService):
        @listener("orders.*", fanout=True)
        async def on_any_order(self, subject: str, order_id: str = "") -> None:
            wildcard_received.append({"order_id": order_id})

        @listener("orders.created", fanout=True)
        async def on_created_order(self, subject: str, order_id: str = "") -> None:
            created_received.append({"order_id": order_id})

    svc = OrderTrackingService(ServiceConfig(name="order_tracker"))
    await svc.start()

    try:
        await svc.publish_event("orders.created", order_id="ord-42")
        await asyncio.sleep(0.3)

        assert len(wildcard_received) == 1, f"Expected 1, got {len(wildcard_received)}"
        assert len(created_received) == 1, f"Expected 1, got {len(created_received)}"
        assert wildcard_received[0]["order_id"] == "ord-42"
        assert created_received[0]["order_id"] == "ord-42"
    finally:
        await svc.stop()

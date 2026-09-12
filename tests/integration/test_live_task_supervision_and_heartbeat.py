"""Live broker integration test for JetStream heartbeating and namespaced DLQ decoupling."""

import asyncio
import json
import time

import nats
import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener, validated_listener
from tests.conftest import broker_url

pytestmark = pytest.mark.integration


class ItemMessage(BaseModel):
    item_id: str
    price: float


@pytest.fixture(autouse=True)
async def _clean_live_test_streams():
    """Purge any live streams created by this suite before and after."""
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    for name in ("LIVE_HB_STREAM", "LIVE_HB_DLQ", "LIVE_NS_STREAM", "LIVE_NS_DLQ"):
        try:
            await js.delete_stream(name)
        except Exception:
            pass
    await nc.close()
    yield
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    for name in ("LIVE_HB_STREAM", "LIVE_HB_DLQ", "LIVE_NS_STREAM", "LIVE_NS_DLQ"):
        try:
            await js.delete_stream(name)
        except Exception:
            pass
    await nc.close()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_jetstream_heartbeat_long_handler_survives():
    """Over live JetStream: handler running 2.5x ack_wait pulses in_progress without redelivery."""
    executions = []

    class LongRunningService(CliffracerService):
        @listener("live.hb.work", durable="live-hb-worker")
        async def on_work(self, subject: str, task_id: str = "") -> None:
            executions.append(time.time())
            # Handler sleeps for 2.2s; ack_wait is 1.0s.
            # Without heartbeat, broker redelivers at 1.0s and 2.0s.
            await asyncio.sleep(2.2)

    cfg = ServiceConfig(
        name="live_hb_svc",
        jetstream_enabled=True,
        jetstream_ack_wait=1.0,
        jetstream_max_deliver=3,
        jetstream_streams=[
            StreamSpec(name="LIVE_HB_STREAM", subjects=["live.hb.*"]),
            StreamSpec(name="LIVE_HB_DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = LongRunningService(cfg)
    await svc.start()

    raw_nc = await nats.connect(broker_url())
    raw_js = raw_nc.jetstream()

    try:
        # Publish single work message
        await raw_js.publish("live.hb.work", b'{"task_id": "endurance_1"}')

        # Wait 3.0s for the handler to complete and ack
        await asyncio.sleep(3.0)

        # Invariant: exactly 1 execution (no duplicate redeliveries triggered by ack_wait expiration)
        assert len(executions) == 1, (
            f"Expected exactly 1 execution due to heartbeat pulses, observed {len(executions)}"
        )

        # Confirm consumer has no outstanding unacked messages
        cinfo = await raw_js.consumer_info("LIVE_HB_STREAM", "live-hb-worker")
        assert cinfo.num_ack_pending == 0
        assert cinfo.num_pending == 0

    finally:
        await raw_nc.close()
        await svc.stop()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_namespaced_service_dlq_decoupling():
    """Over live JetStream: namespaced service dead-letters unnamespaced to dlq.<service>."""
    dlq_messages = []

    class NamespacedOrderService(CliffracerService):
        @validated_listener("orders.create", ItemMessage, durable="live-ns-durable")
        async def on_order(self, message: ItemMessage):
            pass

    cfg = ServiceConfig(
        name="order_service",
        namespace="prod",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="LIVE_NS_STREAM", subjects=["prod.orders.*"]),
            StreamSpec(name="LIVE_NS_DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = NamespacedOrderService(cfg)
    await svc.start()

    raw_nc = await nats.connect(broker_url())
    raw_js = raw_nc.jetstream()

    try:
        # Subscribe to unnamespaced dlq subject
        async def on_dlq(msg):
            dlq_messages.append((msg.subject, json.loads(msg.data.decode(errors="replace"))))

        await raw_nc.subscribe("dlq.order_service", cb=on_dlq)
        await raw_nc.flush()

        # Publish invalid poison payload to namespaced subject
        await raw_js.publish("prod.orders.create", b"CORRUPTED_NON_JSON_DATA!@#$")

        # Wait for dead-lettering
        for _ in range(50):
            if dlq_messages:
                break
            await asyncio.sleep(0.1)

        assert len(dlq_messages) == 1, "Expected 1 dead-lettered message on DLQ"
        subject, data = dlq_messages[0]
        assert subject == "dlq.order_service"
        assert "prod" not in subject
        assert data["original_subject"] == "prod.orders.create"

    finally:
        await raw_nc.close()
        await svc.stop()

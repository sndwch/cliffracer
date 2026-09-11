"""End-to-end: a malformed event dead-letters to the DLQ subject over real NATS."""

import asyncio
import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener


class Ping(BaseModel):
    seq: int


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_invalid_event_is_dead_lettered():
    received: list = []

    class Listener(CliffracerService):
        @validated_listener("ping.events", Ping, fanout=True)
        async def on_ping(self, message: Ping):
            received.append(message)

    svc = Listener(ServiceConfig(name="ping_listener"))
    await svc.start()

    # Subscribe to the DLQ to capture the dead-letter
    dlq_msgs: list = []

    async def _dlq_cb(msg):
        dlq_msgs.append(json.loads(msg.data.decode()))

    await svc.nc.subscribe("dlq.ping_listener", cb=_dlq_cb)
    await asyncio.sleep(0.1)

    try:
        # valid -> handler runs
        await svc.publish_event("ping.events", seq=1)
        # invalid (seq not an int-coercible) -> dead-letter
        await svc.publish_event("ping.events", seq="not-a-number")
        await asyncio.sleep(0.3)

        assert len(received) == 1 and received[0].seq == 1
        assert len(dlq_msgs) == 1
        assert dlq_msgs[0]["original_subject"] == "ping.events"
        assert dlq_msgs[0]["service"] == "ping_listener"
    finally:
        await svc.stop()

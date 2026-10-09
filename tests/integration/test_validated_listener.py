"""End-to-end: a malformed event dead-letters to the DLQ subject over real NATS."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.testing.waiting import wait_until

pytestmark = pytest.mark.integration


class Ping(BaseModel):
    seq: int


TRACE_ID = "dlq-wire-proof"


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
    dlq_headers: list = []

    async def _dlq_cb(msg):
        dlq_msgs.append(json.loads(msg.data.decode()))
        dlq_headers.append(dict(msg.headers or {}))

    dlq_subscription = await svc.nc.subscribe(HandlerDiscovery.dlq_subject(svc.config), cb=_dlq_cb)
    # Registered at the server before anything is published: a flush is the round trip
    # that proves it, where a sleep only hoped.
    await svc.nc.flush()

    try:
        # valid -> handler runs
        await svc.publish_event("ping.events", seq=1)
        # invalid (seq not an int-coercible) -> dead-letter
        await svc.publish_event("ping.events", seq="not-a-number", correlation_id=TRACE_ID)
        await wait_until(
            lambda: len(received) >= 1 and len(dlq_msgs) >= 1,
            within=10.0,
            reason="the valid event to reach the handler and the invalid one to be dead-lettered",
        )
        await svc.nc.flush()

        assert len(received) == 1 and received[0].seq == 1
        assert len(dlq_msgs) == 1
        assert dlq_msgs[0]["original_subject"] == HandlerDiscovery.with_namespace(
            svc.config, "ping.events"
        )
        assert dlq_msgs[0]["service"] == "ping_listener"

        # What a dead-letter consumer parses, read off the wire: which model refused the message,
        # what the message was, why it was refused, and the trace it belongs to.
        letter = dlq_msgs[0]
        assert letter["schema"] == "Ping"
        assert letter["payload"]["data"] == {"seq": "not-a-number"}
        assert letter["payload"]["source_service"] == "ping_listener"
        assert [(e["loc"], e["type"], e["input"]) for e in letter["errors"]] == [
            (["seq"], "int_parsing", "not-a-number")
        ]
        assert letter["correlation_id"] == TRACE_ID
        assert dlq_headers[0]["X-Correlation-ID"] == TRACE_ID
        assert dlq_headers[0]["correlation_id"] == TRACE_ID
        assert dlq_headers[0]["Content-Type"] == "application/json"
    finally:
        await dlq_subscription.unsubscribe()
        await svc.stop()

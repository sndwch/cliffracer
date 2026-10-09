"""A relayed message keeps its producer; the envelope names the hop.

An event envelope has two levels, and they answer different questions. The top
level's `source_service` and `timestamp` say which service published THIS
message and when. `data` is the domain payload, carried untouched, so a
`BroadcastMessage`'s own `source_service` and `timestamp` -- the producer's --
travel there.

A service that receives a broadcast and re-publishes it therefore emits two
`source_service` values, and both are correct: the relay at the top, the
producer in `data`. Listeners are handed `data`, so a listener reads the
producer.

Both levels are asserted on the wire as well as at the listener. The wire tests
pin the envelope itself, so a publisher that starts writing the caller's
values into the envelope, or stops carrying them in `data`, is caught even if
dispatch changes too; the listener tests pin what a handler is given.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from cliffracer import (
    BroadcastMessage,
    CliffracerService,
    ServiceConfig,
    listener,
    validated_listener,
)
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "notifications.sent"
PRODUCER = "upstream-producer"
PRODUCED_AT = datetime(2020, 1, 1, tzinfo=UTC)

PUBLISHERS = pytest.mark.parametrize("publisher", ["publish_event", "broadcast_message"])


class Notification(BroadcastMessage):
    notification_id: str


async def _relayed_wire(publisher: str) -> tuple[bytes, dict]:
    """What a service named `relay` puts on the wire when it re-publishes a notification."""
    relay = CliffracerService(ServiceConfig(name="relay"))
    relay.nc = AsyncMock()
    received = Notification(source_service=PRODUCER, timestamp=PRODUCED_AT, notification_id="n1")

    await getattr(relay, publisher)(SUBJECT, **received.model_dump(mode="json"))

    call = relay.nc.publish.await_args
    return call.args[1], dict(call.kwargs.get("headers") or {})


@PUBLISHERS
@pytest.mark.asyncio
async def test_the_envelope_names_the_relay_and_data_keeps_the_producer(publisher):
    before = datetime.now(UTC)
    raw, _headers = await _relayed_wire(publisher)
    wire = json.loads(raw)

    assert wire["source_service"] == "relay", wire
    published_at = datetime.fromisoformat(wire["timestamp"])
    assert before - timedelta(seconds=5) <= published_at <= datetime.now(UTC), wire

    assert wire["data"]["source_service"] == PRODUCER, wire
    assert datetime.fromisoformat(wire["data"]["timestamp"]) == PRODUCED_AT, wire
    assert wire["data"]["notification_id"] == "n1", wire


async def _dispatch(service_class, raw: bytes, headers: dict) -> None:
    receiver = service_class(ServiceConfig(name="receiver"))
    receiver.nc = AsyncMock()
    receiver._discover_handlers()
    msg = MockMessage(subject=SUBJECT, data=raw, headers=headers)
    await receiver.container.dispatcher.events.handle_event(msg, pattern=SUBJECT)


@PUBLISHERS
@pytest.mark.asyncio
async def test_a_validated_listener_reads_the_producer(publisher):
    seen: list[Notification] = []

    class Receiver(CliffracerService):
        @validated_listener(SUBJECT, Notification, fanout=True)
        async def on_notification(self, message: Notification) -> None:
            seen.append(message)

    await _dispatch(Receiver, *await _relayed_wire(publisher))

    (message,) = seen
    assert message.source_service == PRODUCER
    assert message.timestamp == PRODUCED_AT
    assert message.notification_id == "n1"


@PUBLISHERS
@pytest.mark.asyncio
async def test_a_plain_listener_reads_the_producer(publisher):
    seen: list[dict] = []

    class Receiver(CliffracerService):
        @listener(SUBJECT, fanout=True)
        async def on_notification(
            self, notification_id: str, source_service: str, timestamp: str
        ) -> None:
            seen.append({"source_service": source_service, "timestamp": timestamp})

    await _dispatch(Receiver, *await _relayed_wire(publisher))

    (kwargs,) = seen
    assert kwargs["source_service"] == PRODUCER
    assert datetime.fromisoformat(kwargs["timestamp"]) == PRODUCED_AT

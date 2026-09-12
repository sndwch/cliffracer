"""Unit tests for malformed JSON dead-lettering on durable JetStream listeners.

Verifies that when a durable consumer receives malformed JSON:
1. The message is dead-lettered to dlq.{service} with decode error details.
2. The message is terminated (msg.term()), not acknowledged (msg.ack()) and not nak'd.
3. Even if DLQ publication fails, the message is still terminated to prevent poison loops.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class SampleEvent(BaseModel):
    id: str


def _mock_msg(subject="events.items", data=b"{bad json", num_delivered=1):
    msg = AsyncMock()
    msg.subject = subject
    msg.data = data
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


def _create_service():
    class ItemService(CliffracerService):
        @listener("events.items", durable="item-processor")
        async def on_item(self) -> None:
            pass

        @validated_listener("events.validated", SampleEvent, durable="val-processor")
        async def on_validated(self, message: SampleEvent):
            pass

    config = ServiceConfig(
        name="item_service",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = ItemService(config)
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc._discover_handlers()
    return svc


@pytest.mark.asyncio
async def test_malformed_json_dead_letters_and_terminates():
    """Malformed JSON payload publishes to DLQ and calls msg.term()."""
    svc = _create_service()
    published_deadletters = []

    async def mock_publish_event(subject, **kwargs):
        published_deadletters.append((subject, kwargs))

    svc.publish_event = mock_publish_event
    svc.container._publish_dlq = mock_publish_event

    msg = _mock_msg("events.items", data=b"{this is not valid json")
    await svc.container._handle_jetstream_event(msg)

    # 1. Message terminated
    assert msg.term.await_count == 1
    # 2. Message NOT ack'd and NOT nak'd
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0

    # 3. Dead letter published to DLQ
    assert len(published_deadletters) == 1
    dlq_subject, kwargs = published_deadletters[0]
    assert dlq_subject == "dlq.item_service"
    assert kwargs["original_subject"] == "events.items"
    assert "Decode error" in kwargs["error"]
    assert kwargs["payload"] == {"raw": "{this is not valid json"}
    assert kwargs["service"] == "item_service"


@pytest.mark.asyncio
async def test_malformed_json_on_validated_listener_terminates():
    """Malformed JSON on a @validated_listener terminates without calling ack or nak."""
    svc = _create_service()
    published_deadletters = []

    async def mock_publish_event(subject, **kwargs):
        published_deadletters.append((subject, kwargs))

    svc.publish_event = mock_publish_event
    svc.container._publish_dlq = mock_publish_event

    msg = _mock_msg("events.validated", data=b"<<not json>>")
    await svc.container._handle_jetstream_event(msg)

    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0
    assert len(published_deadletters) == 1
    assert published_deadletters[0][0] == "dlq.item_service"


@pytest.mark.asyncio
async def test_dlq_publish_failure_still_terminates_malformed_message():
    """If publishing to DLQ fails, malformed message is still terminated to avoid redelivery loops."""
    svc = _create_service()

    async def failing_publish_event(subject, **kwargs):
        raise RuntimeError("DLQ stream full or network unreachable")

    svc.publish_event = failing_publish_event
    svc.container._publish_dlq = failing_publish_event

    msg = _mock_msg("events.items", data=b"broken-json-data")
    await svc.container._handle_jetstream_event(msg)

    # Must still terminate
    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0

"""Unit tests for malformed JSON dead-lettering on durable JetStream listeners.

Verifies that when a durable consumer receives malformed JSON:
1. The message is dead-lettered to dlq.{service} with decode error details.
2. The message is terminated (msg.term()), not acknowledged (msg.ack()) and not nak'd.
3. Even if DLQ publication fails, the message is still terminated to prevent poison loops.

Two things the handler-level tests below take as given are checked on their own. The listeners
here are durable ones only if subscription setup binds them as manual-ack JetStream consumers,
which `test_the_listeners_are_bound_as_durable_manual_ack_consumers` reads; and most tests replace
`_publish_dlq` with a recorder, so the real publish (its stream-coverage check and its choice of
JetStream over core NATS) is read by the last tests, which do not.
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


def _create_service(*, dlq_stream: bool = True):
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
            *([StreamSpec(name="DLQ", subjects=["dlq.*"])] if dlq_stream else []),
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
    """Malformed JSON on a @validated_listener terminates without calling ack or nak.

    The decode failure happens before any schema is consulted, so this reads the same path as
    the test above for a validated subject. The validated path itself is the next test.
    """
    svc = _create_service()
    published_deadletters = []

    async def mock_publish_event(subject, **kwargs):
        published_deadletters.append((subject, kwargs))

    svc.container._publish_dlq = mock_publish_event

    msg = _mock_msg("events.validated", data=b"<<not json>>")
    await svc.container._handle_jetstream_event(msg)

    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0
    assert len(published_deadletters) == 1
    assert published_deadletters[0][0] == "dlq.item_service"
    assert "Decode error" in published_deadletters[0][1]["error"]


@pytest.mark.asyncio
async def test_well_formed_json_that_violates_the_schema_is_dead_lettered_with_its_errors():
    """The path that is about `@validated_listener`: valid JSON, wrong shape.

    `SampleEvent` needs a string `id`. The schema registered by the decorator is what rejects
    this, so a listener that lost its schema would accept it. The dead letter carries the
    validation errors and the schema's name, and the message is terminated, not retried.
    """
    svc = _create_service()
    published_deadletters = []

    async def mock_publish_event(subject, **kwargs):
        published_deadletters.append((subject, kwargs))

    svc.container._publish_dlq = mock_publish_event
    msg = _mock_msg("events.validated", data=b'{"id": ["not", "a", "string"]}')

    await svc.container._handle_jetstream_event(msg)

    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0
    ((subject, kwargs),) = published_deadletters
    assert subject == "dlq.item_service"
    assert kwargs.get("schema") == "SampleEvent", kwargs
    assert kwargs.get("errors"), kwargs
    assert "Decode error" not in kwargs.get("error", "")


@pytest.mark.asyncio
async def test_CONTROL_well_formed_json_that_satisfies_the_schema_is_acked_not_dead_lettered():
    """The other side: the same subject, a valid payload, no dead letter."""
    svc = _create_service()
    published_deadletters = []

    async def mock_publish_event(subject, **kwargs):
        published_deadletters.append((subject, kwargs))

    svc.container._publish_dlq = mock_publish_event
    msg = _mock_msg("events.validated", data=b'{"id": "abc"}')

    await svc.container._handle_jetstream_event(msg)

    assert msg.ack.await_count == 1
    assert msg.term.await_count == 0
    assert published_deadletters == []


@pytest.mark.asyncio
async def test_dlq_publish_failure_still_terminates_malformed_message():
    """If publishing to DLQ fails, malformed message is still terminated to avoid redelivery loops."""
    svc = _create_service()

    async def failing_publish_event(subject, **kwargs):
        raise RuntimeError("DLQ stream full or network unreachable")

    svc.container._publish_dlq = failing_publish_event

    msg = _mock_msg("events.items", data=b"broken-json-data")
    await svc.container._handle_jetstream_event(msg)

    # Must still terminate
    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0


@pytest.mark.asyncio
async def test_the_listeners_are_bound_as_durable_manual_ack_consumers():
    """Why `_handle_jetstream_event` is the handler at all: durable listeners are routed to
    JetStream consumers with manual acks, and non-durable ones to core subscriptions whose
    dispatch never acks, naks or terms. With the durable declaration inert, a malformed payload
    would be redelivered for ever and every test above would still pass."""
    svc = _create_service()
    assert svc.container.registry.event_durables == {
        "events.items": "item-processor",
        "events.validated": "val-processor",
    }

    await svc.container._setup_subscriptions()

    bound = {c.args[0]: c.kwargs for c in svc.js.subscribe.call_args_list}
    assert set(bound) == {"events.items", "events.validated"}
    assert bound["events.items"]["durable"] == "item-processor"
    assert bound["events.validated"]["durable"] == "val-processor"
    assert all(kwargs["manual_ack"] is True for kwargs in bound.values())


@pytest.mark.asyncio
async def test_the_real_dead_letter_publish_goes_to_jetstream_not_core_nats():
    """No stub: the dead letter is written by `publish_dlq` itself, to the declared DLQ stream's
    subject through JetStream. A regression that sent it over core NATS would lose it whenever no
    subscriber is attached, and the stubbed tests above could not see that."""
    svc = _create_service()
    msg = _mock_msg("events.items", data=b"{this is not valid json")

    await svc.container._handle_jetstream_event(msg)

    svc.js.publish.assert_awaited_once()
    subject, body = svc.js.publish.await_args.args[:2]
    assert subject == "dlq.item_service"
    assert b"Decode error" in body
    assert b"events.items" in body
    svc.nc.publish.assert_not_called()
    assert msg.term.await_count == 1


@pytest.mark.asyncio
async def test_a_dead_letter_no_declared_stream_covers_is_not_published_and_the_message_is_still_terminated():
    """The DLQ stream in the fixture is what lets the publish through: without it the real
    publish refuses (`StreamDeclarationError`), nothing is sent, and the poison message is
    terminated all the same."""
    svc = _create_service(dlq_stream=False)
    msg = _mock_msg("events.items", data=b"{this is not valid json")

    await svc.container._handle_jetstream_event(msg)

    svc.js.publish.assert_not_called()
    svc.nc.publish.assert_not_called()
    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0

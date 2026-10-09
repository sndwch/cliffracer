"""A dead letter carries the correlation id of the message it records.

Every dead-letter path works out the message's correlation id and hands it to
`publish_dlq` in the headers. `publish_dlq` chose its own id from the record
fields and the ambient correlation context, and minted a new one when neither
had it -- then wrote that new id over the header it had been given. On the
decode-error and last-delivery paths the ambient context is not set, so their
dead letters carried an id no log line or trace shared. The invalid-payload
path kept its id only because it runs while the context is still set.

Each path is driven through the container with its own id, and the dead letter
is read off the wire: the body and both headers must carry that id.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.correlation import correlation_id_var
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class Ping(BaseModel):
    seq: int


class DeadLetteringService(CliffracerService):
    @listener("events.boom", durable="boomer")
    async def on_boom(self, subject: str, seq: int = 0) -> None:
        raise RuntimeError("always fails")

    @validated_listener("events.typed", Ping, fanout=True)
    async def on_typed(self, message: Ping) -> None:
        pass

    @listener("events.bad", fanout=True)
    async def on_bad(self, item: str = "") -> None:
        pass


def _service() -> DeadLetteringService:
    svc = DeadLetteringService(
        ServiceConfig(
            name="dlq_svc",
            jetstream_enabled=True,
            jetstream_max_deliver=2,
            dlq_subject="dlq.{service}",
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()
    return svc


def _dead_letter(svc) -> tuple[dict, dict]:
    """The one dead letter published, as (body, headers)."""
    rows = [
        (json.loads(call.args[1]), dict(call.kwargs.get("headers") or {}))
        for mock in (svc.nc.publish, svc.js.publish)
        for call in mock.await_args_list
        if str(call.args[0]).startswith("dlq.")
    ]
    assert len(rows) == 1, rows
    return rows[0]


def _assert_carries(body: dict, headers: dict, correlation_id: str) -> None:
    assert body["correlation_id"] == correlation_id, body
    assert headers["X-Correlation-ID"] == correlation_id, headers
    assert headers["correlation_id"] == correlation_id, headers


@pytest.mark.asyncio
async def test_a_decode_error_dead_letter_keeps_the_messages_id():
    svc = _service()
    msg = MockMessage(
        subject="events.bad", data=b"not valid json {{{", headers={"X-Correlation-ID": "c-bad"}
    )

    await svc.container._dispatch_event(msg, pattern="events.bad")

    body, headers = _dead_letter(svc)
    assert body["error"].startswith("Decode error:"), body
    _assert_carries(body, headers, "c-bad")


@pytest.mark.asyncio
async def test_a_last_delivery_dead_letter_keeps_the_messages_id():
    svc = _service()
    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = (
        "events.boom",
        b'{"seq": 1}',
        {"X-Correlation-ID": "c-term"},
    )
    msg.metadata = SimpleNamespace(num_delivered=2)

    await svc.container._handle_jetstream_event(msg, pattern="events.boom")

    body, headers = _dead_letter(svc)
    assert body["deliveries"] == 2, body
    _assert_carries(body, headers, "c-term")


@pytest.mark.asyncio
async def test_an_invalid_payload_dead_letter_keeps_the_messages_id():
    """This path kept its id before, because it runs while the context is set."""
    svc = _service()
    msg = MockMessage(
        subject="events.typed",
        data=b'{"seq": "not-an-int"}',
        headers={"X-Correlation-ID": "c-inv", "Content-Type": "application/json"},
    )

    await svc.container._dispatch_event(msg, pattern="events.typed")

    body, headers = _dead_letter(svc)
    _assert_carries(body, headers, "c-inv")


@pytest.mark.asyncio
async def test_publish_dlq_keeps_an_id_it_is_given_in_headers_with_no_context_set():
    svc = _service()
    assert correlation_id_var.get() is None

    await svc.container.dispatcher.dlq.publish_dlq(
        "dlq.dlq_svc", payload={"raw": "x"}, headers={"X-Correlation-ID": "c-given"}, error="e"
    )

    body, headers = _dead_letter(svc)
    _assert_carries(body, headers, "c-given")


@pytest.mark.asyncio
async def test_an_id_given_in_headers_wins_over_a_different_ambient_id():
    """The id a caller hands over names the message; the ambient one names whatever
    request is running, which is not necessarily the message being dead-lettered."""
    svc = _service()
    token = correlation_id_var.set("c-ambient")
    try:
        await svc.container.dispatcher.dlq.publish_dlq(
            "dlq.dlq_svc", payload={"raw": "x"}, headers={"X-Correlation-ID": "c-given"}, error="e"
        )
    finally:
        correlation_id_var.reset(token)

    body, headers = _dead_letter(svc)
    _assert_carries(body, headers, "c-given")


@pytest.mark.asyncio
async def test_a_dead_letter_for_a_message_with_no_id_carries_one_id_throughout():
    """The control: with nothing to keep, one id is minted and used everywhere."""
    svc = _service()
    msg = MockMessage(subject="events.bad", data=b"not valid json {{{", headers={})

    await svc.container._dispatch_event(msg, pattern="events.bad")

    body, headers = _dead_letter(svc)
    minted = body["correlation_id"]
    assert minted, body
    _assert_carries(body, headers, minted)

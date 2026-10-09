"""`publish_dlq` picks its transport from the connection, and passes bytes and headers through.

Holds: a connection that does not say JetStream is active publishes on core NATS; a `bytes`
payload is sent as it is, with the caller's headers untouched; a record with no payload has no
`payload` key; a Content-Type the caller supplied (in any letter case) is not replaced.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


def _core_publisher(**conn_fields) -> tuple[DeadLetterPublisher, AsyncMock]:
    nc = AsyncMock()
    cfg = ServiceConfig(name="orders", dlq_subject="dlq.{service}")
    conn = SimpleNamespace(nc=nc, **conn_fields)
    return DeadLetterPublisher(cfg, lambda: conn), nc


async def test_a_connection_that_does_not_report_jetstream_active_publishes_on_core_nats():
    publisher, nc = _core_publisher()  # no `jetstream_active` attribute at all

    await publisher.publish_dlq("dlq.orders", {"x": 1})

    (call,) = nc.publish.await_args_list
    assert call.args[0] == "dlq.orders"


async def test_CONTROL_a_connection_reporting_jetstream_active_publishes_on_the_stream():
    js = AsyncMock()
    cfg = ServiceConfig(
        name="orders",
        dlq_subject="dlq.{service}",
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.>"])],
    )
    nc = AsyncMock()
    conn = SimpleNamespace(nc=nc, js=js, jetstream_active=True)
    publisher = DeadLetterPublisher(cfg, lambda: conn)

    await publisher.publish_dlq("dlq.orders", {"x": 1})

    assert js.publish.await_count == 1
    assert nc.publish.await_count == 0


async def test_a_bytes_payload_is_sent_exactly_as_given_with_the_callers_headers():
    publisher, nc = _core_publisher(jetstream_active=False)

    await publisher.publish_dlq("dlq.orders", b"\x00raw-bytes", headers={"X-Trace": "t-1"})

    (call,) = nc.publish.await_args_list
    assert call.args[1] == b"\x00raw-bytes"
    assert call.kwargs["headers"] == {"X-Trace": "t-1"}


async def test_CONTROL_a_dict_payload_is_serialised_and_stamped():
    publisher, nc = _core_publisher(jetstream_active=False)

    await publisher.publish_dlq("dlq.orders", {"a": 1}, headers={"X-Trace": "t-1"})

    (call,) = nc.publish.await_args_list
    assert json.loads(call.args[1])["payload"] == {"a": 1}
    assert call.kwargs["headers"]["Content-Type"] == "application/json"
    assert call.kwargs["headers"]["correlation_id"]


async def test_a_record_published_with_no_payload_has_no_payload_key():
    publisher, nc = _core_publisher(jetstream_active=False)

    await publisher.publish_dlq("dlq.orders", None, error="boom")

    (call,) = nc.publish.await_args_list
    record = json.loads(call.args[1])
    assert record["error"] == "boom"
    assert "payload" not in record


@pytest.mark.parametrize("name", ["Content-Type", "content-type", "CONTENT-TYPE"])
async def test_a_content_type_the_caller_supplied_is_kept_and_not_added_beside(name):
    publisher, nc = _core_publisher(jetstream_active=False)

    await publisher.publish_dlq("dlq.orders", {"a": 1}, headers={name: "application/x-custom"})

    (call,) = nc.publish.await_args_list
    sent = call.kwargs["headers"]
    content_types = {k: v for k, v in sent.items() if k.lower() == "content-type"}
    assert content_types == {name: "application/x-custom"}


async def test_CONTROL_with_no_content_type_supplied_one_is_added():
    publisher, nc = _core_publisher(jetstream_active=False)

    await publisher.publish_dlq("dlq.orders", {"a": 1}, headers={"X-Trace": "t-1"})

    (call,) = nc.publish.await_args_list
    assert call.kwargs["headers"]["Content-Type"] == "application/json"

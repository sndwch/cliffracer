"""`DeadLetterPublisher.origin` reports what the message carried, and a malformed one costs no letter.

Holds: a message whose headers are all credentials gets `withheld_headers` and no (empty)
`original_headers`; a delivery with the three JetStream coordinates and no headers at all still
gets its `Nats-Msg-Id`; headers that are not a mapping are ignored, not a reason to lose the dead
letter.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.dispatch.dlq import DeadLetterPublisher

pytestmark = pytest.mark.unit


def _publisher() -> tuple[DeadLetterPublisher, AsyncMock]:
    nc = AsyncMock()
    cfg = ServiceConfig(name="orders", dlq_subject="dlq.{service}")
    conn = SimpleNamespace(nc=nc, js=None, jetstream_active=False)
    return DeadLetterPublisher(cfg, lambda: conn), nc


def _delivery(headers, *, with_coordinates=True):
    metadata = (
        SimpleNamespace(
            num_delivered=2,
            stream="EVENTS",
            consumer="durable",
            sequence=SimpleNamespace(stream=42),
        )
        if with_coordinates
        else None
    )
    return SimpleNamespace(
        subject="events.order", data=b'{"n": 1}', headers=headers, metadata=metadata
    )


def test_a_message_with_only_credential_headers_gets_withheld_names_and_no_original_headers():
    publisher, _ = _publisher()

    fields, _ = publisher.origin(_delivery({"Authorization": "Bearer s3cret"}))

    assert fields["withheld_headers"] == ["Authorization"]
    assert "original_headers" not in fields


def test_CONTROL_a_message_with_a_plain_header_beside_the_credential_keeps_original_headers():
    publisher, _ = _publisher()

    fields, _ = publisher.origin(_delivery({"Authorization": "Bearer s3cret", "X-Trace": "t-1"}))

    assert fields["original_headers"] == {"X-Trace": "t-1"}
    assert fields["withheld_headers"] == ["Authorization"]


@pytest.mark.parametrize("headers", [None, {}, {"X-Trace": "t-1"}])
def test_a_delivery_with_all_three_coordinates_gets_its_msg_id_whatever_headers_it_had(headers):
    publisher, _ = _publisher()

    fields, out_headers = publisher.origin(_delivery(headers))

    assert (fields["stream"], fields["stream_sequence"], fields["consumer"]) == (
        "EVENTS",
        42,
        "durable",
    )
    assert out_headers == {"Nats-Msg-Id": "dlq:orders:EVENTS:42:durable"}


def test_CONTROL_a_delivery_without_coordinates_gets_no_msg_id():
    publisher, _ = _publisher()

    fields, out_headers = publisher.origin(_delivery(None, with_coordinates=False))

    assert fields == {}
    assert out_headers == {}


@pytest.mark.parametrize("headers", ["not-a-mapping", [("X-Trace", "t-1")]])
def test_headers_that_are_not_a_mapping_add_no_header_fields(headers):
    publisher, _ = _publisher()

    fields, _ = publisher.origin(_delivery(headers, with_coordinates=False))

    assert fields == {}


@pytest.mark.parametrize("headers", ["not-a-mapping", [("X-Trace", "t-1")]])
@pytest.mark.parametrize("handler", ["terminated", "decode"])
async def test_a_message_whose_headers_are_not_a_mapping_is_still_dead_lettered(handler, headers):
    publisher, nc = _publisher()
    msg = _delivery(headers, with_coordinates=False)

    if handler == "terminated":
        delivered = await publisher.dead_letter_terminated(msg, "boom", 3)
    else:
        delivered = await publisher.dead_letter_decode_error(msg, ValueError("bad"))

    assert delivered is True
    assert publisher.lost == 0
    (call,) = nc.publish.await_args_list
    record = json.loads(call.args[1])
    assert record["original_subject"] == "events.order"
    assert "original_headers" not in record

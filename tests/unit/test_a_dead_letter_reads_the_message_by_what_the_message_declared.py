"""A dead letter reads the delivery by its own headers and body, never by an assumption.

Holds: the Content-Type header (the first one, whatever its position among the headers) decides
how a delivery's body is read for its record; an undecodable body is recorded as raw text, also
when the message has no `data` at all; `delivery_limit` is in the record only when one was given;
a correlation id carried in a dict payload is the record's id, and a payload that is not a dict
is no reason to lose the dead letter; the same holds for an invalid message's payload id.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import ServiceConfig
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.validation import deserialize_payload

pytestmark = pytest.mark.unit

# The one byte 0x35: JSON reads it as the number 5, MessagePack as the integer 53.
FIVE_OR_FIFTY_THREE = b"5"


def _publisher(fmt: str = "json") -> tuple[DeadLetterPublisher, AsyncMock]:
    nc = AsyncMock()
    cfg = ServiceConfig(name="orders", dlq_subject="dlq.{service}", serialization_format=fmt)
    conn = SimpleNamespace(nc=nc, js=None, jetstream_active=False)
    return DeadLetterPublisher(cfg, lambda: conn), nc


def _msg(data=b'{"n": 1}', headers=None):
    return SimpleNamespace(subject="events.order", data=data, headers=headers, metadata=None)


def _published(nc: AsyncMock) -> tuple[dict, dict[str, str]]:
    (call,) = nc.publish.await_args_list
    return json.loads(call.args[1]), call.kwargs["headers"]


def _published_as_declared(nc: AsyncMock) -> tuple[dict, dict[str, str]]:
    """The record read by the Content-Type the publisher declared on it, whatever it is."""
    (call,) = nc.publish.await_args_list
    headers = call.kwargs["headers"]
    record = deserialize_payload(
        call.args[1], content_type=headers.get("Content-Type"), fallback_format="json"
    )
    return record, headers


@pytest.fixture(autouse=True)
def _no_ambient_id():
    CorrelationContext.clear()
    yield
    CorrelationContext.clear()


async def test_a_msgpack_content_type_makes_the_record_read_the_body_as_msgpack():
    publisher, nc = _publisher()

    await publisher.dead_letter_terminated(
        _msg(FIVE_OR_FIFTY_THREE, {"Content-Type": "application/msgpack"}), "boom", 3
    )

    assert _published(nc)[0]["payload"] == 53


async def test_CONTROL_a_json_content_type_makes_the_record_read_the_same_body_as_json():
    publisher, nc = _publisher()

    await publisher.dead_letter_terminated(
        _msg(FIVE_OR_FIFTY_THREE, {"Content-Type": "application/json"}), "boom", 3
    )

    assert _published(nc)[0]["payload"] == 5


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_CONTROL_a_json_content_type_reads_the_body_as_json_whatever_the_service_writes(fmt):
    """The delivery's declared Content-Type decides how its body is read, not the service's own
    serialization format: a JSON body under a JSON header reads as 5 for a msgpack service too."""
    publisher, nc = _publisher(fmt)

    await publisher.dead_letter_terminated(
        _msg(FIVE_OR_FIFTY_THREE, {"Content-Type": "application/json"}), "boom", 3
    )

    record, _ = _published_as_declared(nc)
    assert record["payload"] == 5, record


async def test_the_content_type_is_found_behind_headers_that_are_not_one():
    publisher, nc = _publisher()
    headers = {"X-Trace": "t-1", "Content-Type": "application/msgpack"}

    await publisher.dead_letter_terminated(_msg(FIVE_OR_FIFTY_THREE, headers), "boom", 3)

    assert _published(nc)[0]["payload"] == 53


async def test_the_first_content_type_header_decides_when_there_are_two():
    publisher, nc = _publisher()
    headers = {"Content-Type": "application/msgpack", "content-type": "application/json"}

    await publisher.dead_letter_terminated(_msg(FIVE_OR_FIFTY_THREE, headers), "boom", 3)

    assert _published(nc)[0]["payload"] == 53


async def test_a_body_that_cannot_be_decoded_is_recorded_as_raw_text():
    publisher, nc = _publisher()

    await publisher.dead_letter_terminated(_msg(b"{not json", None), "boom", 3)

    assert _published(nc)[0]["payload"] == {"raw": "{not json"}


async def test_a_message_with_no_data_attribute_is_recorded_with_empty_raw_text():
    publisher, nc = _publisher()
    msg = SimpleNamespace(subject="events.order", headers=None, metadata=None)  # no `data`

    delivered = await publisher.dead_letter_terminated(msg, "boom", 3)

    assert delivered is True
    assert _published(nc)[0]["payload"] == {"raw": ""}


async def test_a_decode_error_on_a_message_whose_data_is_none_records_empty_raw_text():
    publisher, nc = _publisher()

    delivered = await publisher.dead_letter_decode_error(_msg(None), ValueError("bad"))

    assert delivered is True
    assert _published(nc)[0]["payload"] == {"raw": ""}


async def test_CONTROL_a_decode_error_records_the_raw_text_of_the_data_it_has():
    publisher, nc = _publisher()

    await publisher.dead_letter_decode_error(_msg(b"{not json"), ValueError("bad"))

    assert _published(nc)[0]["payload"] == {"raw": "{not json"}


async def test_a_dead_letter_with_no_delivery_limit_has_no_delivery_limit_field():
    publisher, nc = _publisher()

    await publisher.dead_letter_terminated(_msg(), "boom", 3)

    assert "delivery_limit" not in _published(nc)[0]


async def test_CONTROL_a_dead_letter_with_a_delivery_limit_records_it():
    publisher, nc = _publisher()

    await publisher.dead_letter_terminated(_msg(), "boom", 3, delivery_limit="server max_deliver 3")

    assert _published(nc)[0]["delivery_limit"] == "server max_deliver 3"


async def test_a_correlation_id_in_the_payload_is_the_id_of_the_dead_letter_at_the_limit():
    publisher, nc = _publisher()
    msg = _msg(b'{"correlation_id": "from-the-payload", "n": 1}', None)

    await publisher.dead_letter_terminated(msg, "boom", 3)

    record, headers = _published(nc)
    assert headers["correlation_id"] == "from-the-payload"
    assert headers["X-Correlation-ID"] == "from-the-payload"
    assert record["correlation_id"] == "from-the-payload"


async def test_a_payload_that_is_not_a_dict_gets_a_new_id_and_is_still_dead_lettered():
    publisher, nc = _publisher()

    delivered = await publisher.dead_letter_terminated(_msg(b"[1, 2]", None), "boom", 3)

    assert delivered is True
    record, headers = _published(nc)
    assert record["payload"] == [1, 2]
    assert headers["correlation_id"].startswith("corr_")


class _Schema(BaseModel):
    number: int


def _invalid():
    try:
        _Schema(number="x")  # type: ignore[arg-type]
    except Exception as exc:
        return exc
    raise AssertionError


async def test_a_correlation_id_in_an_invalid_payload_is_the_id_of_its_dead_letter():
    publisher, nc = _publisher()
    payload = {"number": "x", "correlation_id": "from-the-payload"}

    delivered = await publisher.handle_invalid_message(
        "events.order", payload, _invalid(), _Schema, "deadletter"
    )

    assert delivered is True
    record, headers = _published(nc)
    assert headers["correlation_id"] == "from-the-payload"
    assert record["correlation_id"] == "from-the-payload"


@pytest.mark.parametrize(
    ("payload", "expected_id"),
    [
        pytest.param([1, 2], None, id="a-list-payload"),
        pytest.param(
            {"correlation_id": "abc-123", "number": "x"}, "abc-123", id="a-string-id-in-a-dict"
        ),
    ],
)
async def test_an_invalid_message_is_dead_lettered_with_the_payloads_string_id_or_a_new_one(
    payload, expected_id
):
    """A correlation id is taken from the payload only when the payload is a dict; any other
    payload is still dead-lettered, under a new id."""
    publisher, nc = _publisher()

    delivered = await publisher.handle_invalid_message(
        "events.order", payload, _invalid(), _Schema, "deadletter"
    )

    assert delivered is True
    record, headers = _published(nc)
    assert record["payload"] == payload
    if expected_id is None:
        assert headers["correlation_id"].startswith("corr_")
    else:
        assert record["correlation_id"] == expected_id == headers["correlation_id"]


async def test_CONTROL_an_invalid_payload_with_no_id_anywhere_gets_a_new_one():
    publisher, nc = _publisher()

    await publisher.handle_invalid_message(
        "events.order", {"number": "x"}, _invalid(), _Schema, "deadletter"
    )

    _, headers = _published(nc)
    assert headers["correlation_id"].startswith("corr_")

"""A dead letter says which delivery it records: the headers, the stream, the sequence, the consumer.

The record carried the payload, the error and the subject the message arrived on, and nothing that
identifies the delivery. A dead letter could not be traced to the message in its stream, and a
second publish of the same one was stored twice. It now carries `original_headers` (without the
ones that hold a credential, which `withheld_headers` names), `stream`, `stream_sequence` and
`consumer`, and is published with a `Nats-Msg-Id` built from the three, so the stream stores one
record per delivery inside its duplicate window.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.msg import Msg
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

TOKEN = "eyJhbGciOiJIUzI1NiJ9.secret-bearer-token"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}",
    "Nats-Msg-Id": "orders.created:o-1",
    "traceparent": "00-abc-def-01",
}


def _config(**extra) -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **extra,
    )


def _publisher(service=None) -> tuple[DeadLetterPublisher, AsyncMock]:
    js = AsyncMock()
    conn = SimpleNamespace(jetstream_active=True, js=js, nc=AsyncMock())
    return DeadLetterPublisher(_config(), lambda: conn, service=service), js


def _delivery(*, stream="EVENTS", seq=42, consumer="orders-durable", headers=HEADERS, **extra):
    metadata = SimpleNamespace(
        num_delivered=extra.pop("num_delivered", 3),
        stream=stream,
        consumer=consumer,
        sequence=SimpleNamespace(stream=seq, consumer=7),
    )
    return SimpleNamespace(
        subject="events.order", data=b'{"number": 1}', headers=headers, metadata=metadata
    )


class _Schema(BaseModel):
    number: int


def _invalid():
    try:
        _Schema(number="x")  # type: ignore[arg-type]
    except Exception as exc:
        return exc
    raise AssertionError


async def _dead_letter(publisher: DeadLetterPublisher, handler: str, msg) -> None:
    if handler == "terminated":
        await publisher.dead_letter_terminated(msg, "boom", 3)
    elif handler == "decode":
        await publisher.dead_letter_decode_error(msg, ValueError("bad"))
    else:
        await publisher.handle_invalid_message(
            "events.order", {"number": "x"}, _invalid(), _Schema, "deadletter", msg=msg
        )


def _published(js: AsyncMock) -> tuple[dict, dict[str, str]]:
    (call,) = js.publish.await_args_list
    return json.loads(call.args[1]), call.kwargs["headers"]


HANDLERS = ["terminated", "decode", "invalid"]


@pytest.mark.parametrize("handler", HANDLERS)
async def test_the_record_names_the_delivery_in_its_stream(handler):
    publisher, js = _publisher()

    await _dead_letter(publisher, handler, _delivery())

    record, _ = _published(js)
    assert (record["stream"], record["stream_sequence"], record["consumer"]) == (
        "EVENTS",
        42,
        "orders-durable",
    )


@pytest.mark.parametrize("handler", HANDLERS)
async def test_the_record_carries_the_original_headers_but_not_a_credential(handler):
    publisher, js = _publisher()

    await _dead_letter(publisher, handler, _delivery())

    record, headers = _published(js)
    assert record["original_headers"] == {
        "Content-Type": "application/json",
        "Nats-Msg-Id": "orders.created:o-1",
        "traceparent": "00-abc-def-01",
    }
    assert record["withheld_headers"] == ["Authorization"]
    assert TOKEN not in json.dumps(record) and TOKEN not in json.dumps(headers)


@pytest.mark.parametrize("handler", HANDLERS)
async def test_the_dead_letter_is_published_with_an_id_made_from_the_delivery(handler):
    publisher, js = _publisher()

    await _dead_letter(publisher, handler, _delivery())

    _, headers = _published(js)
    assert headers["Nats-Msg-Id"] == "dlq:orders:EVENTS:42:orders-durable"
    # The original message's own id is in the record; the dead letter's differs from it.
    assert headers["Nats-Msg-Id"] != HEADERS["Nats-Msg-Id"]


async def test_two_consumers_of_one_stream_message_each_get_their_own_dead_letter():
    ids = []
    for consumer in ("orders-durable", "audit-durable", "orders-durable"):
        publisher, js = _publisher()
        await publisher.dead_letter_terminated(_delivery(consumer=consumer), "boom", 3)
        ids.append(_published(js)[1]["Nats-Msg-Id"])

    assert ids[0] == ids[2], (
        "the same delivery must get the same id, so a republish is deduplicated"
    )
    assert ids[0] != ids[1], (
        "another consumer's dead letter of the same message must not be dropped"
    )


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "Cookie",
        "X-Api-Key",
        "x-refresh-token",
        "X-Client-Secret",
        "X-Password",
    ],
)
async def test_a_header_that_names_a_credential_is_withheld(name):
    publisher, js = _publisher()

    await publisher.dead_letter_terminated(
        _delivery(headers={name: "hunter2", "X-Trace": "t-1"}), "boom", 3
    )

    record, _ = _published(js)
    assert record["original_headers"] == {"X-Trace": "t-1"}
    assert record["withheld_headers"] == [name]


async def test_the_header_an_installed_extension_reads_a_credential_from_is_withheld():
    service = SimpleNamespace(
        container=SimpleNamespace(extensions=[SimpleNamespace(header="X-Session")])
    )
    publisher, js = _publisher(service)

    await publisher.dead_letter_terminated(
        _delivery(headers={"X-Session": "s-123", "X-Trace": "t-1"}), "boom", 3
    )

    record, _ = _published(js)
    assert record["original_headers"] == {"X-Trace": "t-1"}
    assert record["withheld_headers"] == ["X-Session"]


@pytest.mark.parametrize("handler", HANDLERS)
async def test_a_core_message_has_no_stream_and_no_id_and_the_record_is_what_it_was(handler):
    publisher, js = _publisher()
    msg = SimpleNamespace(subject="events.order", data=b"{}", headers=None, metadata=None)

    await _dead_letter(publisher, handler, msg)

    record, headers = _published(js)
    for field in ("stream", "stream_sequence", "consumer", "original_headers", "withheld_headers"):
        assert field not in record
    assert "Nats-Msg-Id" not in headers


@pytest.mark.parametrize("missing", ["stream", "seq", "consumer"])
@pytest.mark.parametrize("handler", HANDLERS)
async def test_a_delivery_missing_one_coordinate_gets_no_id_and_is_still_dead_lettered(
    handler, missing
):
    publisher, js = _publisher()
    coordinates = {"stream": "EVENTS", "seq": 42, "consumer": "orders-durable"} | {missing: None}

    await _dead_letter(publisher, handler, _delivery(**coordinates))

    record, headers = _published(js)
    assert "Nats-Msg-Id" not in headers
    assert record["original_subject"] == "events.order"
    assert len(js.publish.await_args_list) == 1


async def test_a_message_whose_metadata_is_not_the_servers_adds_no_fields():
    publisher, js = _publisher()
    msg = AsyncMock()  # every attribute is a mock, as in the suite's own message doubles
    msg.subject, msg.data, msg.headers = "events.order", b"{}", None

    await publisher.dead_letter_terminated(msg, "boom", 3)

    record, headers = _published(js)
    assert not {"stream", "stream_sequence", "consumer"} & record.keys()
    assert "Nats-Msg-Id" not in headers


def _service():
    class Svc(CliffracerService):
        @validated_listener("events.order", _Schema, fanout=True)
        async def on_order(self, message: _Schema) -> None:
            raise AssertionError("an invalid message must not reach the handler")

    svc = Svc(_config())
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    return svc


async def test_an_invalid_message_through_the_dispatcher_carries_its_delivery_in_the_record():
    svc = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = "events.order", b'{"number": "x"}', dict(HEADERS)
    msg.metadata = _delivery().metadata

    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )

    record, headers = _published(svc.js)
    assert record["errors"] and record["stream_sequence"] == 42
    assert record["withheld_headers"] == ["Authorization"]
    assert headers["Nats-Msg-Id"] == "dlq:orders:EVENTS:42:orders-durable"
    assert msg.term.await_count == 1


CORE = Msg(
    _client=MagicMock(),
    subject="events.order",
    reply="",
    data=b'{"number": 1}',
    headers={"Content-Type": "application/json", "X-Trace": "t-1"},
)
STREAMED = Msg(
    _client=MagicMock(),
    subject="events.order",
    reply="$JS.ACK.EVENTS.orders-durable.3.42.7.1700000000000000000.0",
    data=b'{"number": 1}',
    headers={"Content-Type": "application/json", "X-Trace": "t-1"},
)


def test_CONTROL_a_real_core_message_raises_on_metadata_and_a_real_stream_message_does_not():
    from nats.errors import NotJSMessageError

    with pytest.raises(NotJSMessageError):
        CORE.metadata  # noqa: B018
    assert STREAMED.metadata.stream == "EVENTS" and STREAMED.metadata.sequence.stream == 42


@pytest.mark.parametrize("handler", HANDLERS)
async def test_a_real_core_message_is_dead_lettered_with_its_headers_and_no_stream_fields(handler):
    publisher, js = _publisher()

    await _dead_letter(publisher, handler, CORE)

    record, headers = _published(js)
    assert record["original_headers"] == {"Content-Type": "application/json", "X-Trace": "t-1"}
    assert not {"stream", "stream_sequence", "consumer"} & record.keys()
    assert "Nats-Msg-Id" not in headers


@pytest.mark.parametrize("handler", HANDLERS)
async def test_a_real_stream_message_is_dead_lettered_with_the_coordinates_its_ack_subject_carries(
    handler,
):
    publisher, js = _publisher()

    await _dead_letter(publisher, handler, STREAMED)

    record, headers = _published(js)
    assert (record["stream"], record["stream_sequence"], record["consumer"]) == (
        "EVENTS",
        42,
        "orders-durable",
    )
    assert headers["Nats-Msg-Id"] == "dlq:orders:EVENTS:42:orders-durable"


async def test_the_decode_path_reads_the_delivery_count_of_a_real_stream_message():
    publisher, js = _publisher()

    await publisher.dead_letter_decode_error(STREAMED, ValueError("bad"))

    assert _published(js)[0]["deliveries"] == 3


async def test_the_decode_path_of_a_real_core_message_counts_one_delivery():
    publisher, js = _publisher()

    await publisher.dead_letter_decode_error(CORE, ValueError("bad"))

    assert _published(js)[0]["deliveries"] == 1


@pytest.mark.parametrize(
    "name",
    [
        "X-Authorization",
        "x-apikey",
        "X-ApiKey",
        "jwt",
        "X-JWT-Assertion",
        "X-Bearer",
        "session",
        "X-Session-Id",
    ],
)
async def test_the_wider_credential_names_are_withheld(name):
    publisher, js = _publisher()

    await publisher.dead_letter_terminated(
        _delivery(headers={name: "secret-value", "X-Trace": "t-1"}), "boom", 3
    )

    record, _ = _published(js)
    assert record["original_headers"] == {"X-Trace": "t-1"}
    assert record["withheld_headers"] == [name]


async def test_a_credential_under_a_name_no_rule_matches_is_copied_as_it_arrived():
    publisher, js = _publisher()

    await publisher.dead_letter_terminated(
        _delivery(headers={"X-Custom-Cred": "value", "X-Trace": "t-1"}), "boom", 3
    )

    record, _ = _published(js)
    assert record["original_headers"] == {"X-Custom-Cred": "value", "X-Trace": "t-1"}
    assert "withheld_headers" not in record

"""A message whose payload carries a `correlation_id` that is not a string is still handled and settled.

The id of a message comes from a header, which is always text, or from a payload field, which is any
JSON value. A number or an object there made the id resolution raise `TypeError` before any handler
ran, and the dead letter for a delivery on its last attempt raised it again before publishing, so the
delivery got no ack, no nak and no term, and no dead letter, and `dead_letters_lost` stayed at zero.
A value that is not a string is not an id and is treated as absent, as one holding a CR or LF is; and
a dead letter that cannot be built is counted and the delivery is terminated all the same.

The JetStream deliveries here are real `nats.aio.msg.Msg` objects with an ack subject, and what each
settled it with is read from what it published on that subject.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.aio.msg import Msg
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

NOT_STRINGS = [
    pytest.param(123, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param(["a"], id="list"),
    pytest.param({"id": 1}, id="object"),
]

SEEN: dict[str, object] = {}


class Order(BaseModel):
    number: int


def _config(**extra):
    return ServiceConfig(
        name="orders",
        health_port=0,
        jetstream_enabled=True,
        jetstream_max_deliver=5,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **extra,
    )


class Handling(CliffracerService):
    fail = False

    @listener("events.order", durable="orders-d")
    async def on_order(self, subject: str, number: int = 0) -> None:
        SEEN["id"] = CorrelationContext.get()
        if self.fail:
            raise RuntimeError("the handler failed")


class Validating(CliffracerService):
    @validated_listener("events.order", Order, durable="orders-d")
    async def on_order(self, message: Order) -> None:
        raise AssertionError("an invalid message must not reach the handler")


class Core(CliffracerService):
    @listener("events.order", fanout=True)
    async def on_order(self, subject: str, number: int = 0) -> None:
        SEEN["id"] = CorrelationContext.get()


def _delivery(payload, *, num_delivered=1):
    client = MagicMock()
    client.publish = AsyncMock()
    reply = f"$JS.ACK.EVENTS.orders-d.{num_delivered}.1.1.1700000000000000000.0"
    msg = Msg(
        _client=client,
        subject="events.order",
        reply=reply,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return msg, client


def _settled_with(client) -> list[str]:
    out = []
    for call in client.publish.await_args_list:
        body = call.args[1] if len(call.args) > 1 else b""
        out.append(
            "nak"
            if body.startswith(b"-NAK")
            else "term"
            if body.startswith(b"+TERM")
            else "progress"
            if body.startswith(b"+WPI")
            else "ack"
        )
    return [kind for kind in out if kind != "progress"]


async def _jetstream(service_class, payload, *, num_delivered=1, fail=False):
    svc = service_class(_config())
    svc.fail = fail
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    msg, client = _delivery(payload, num_delivered=num_delivered)
    SEEN.clear()
    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )
    return svc, client


def _dead_letters(svc):
    return [json.loads(call.args[1]) for call in svc.js.publish.await_args_list]


@pytest.mark.parametrize("value", NOT_STRINGS)
def test_a_value_that_is_not_a_string_is_not_an_id_and_a_new_one_is_made(value):
    for resolved in (
        CorrelationContext.for_message({}, {"correlation_id": value}),
        CorrelationContext.new_id_unless_given(value),
        CorrelationContext.get_or_create_id(value),
    ):
        assert isinstance(resolved, str) and resolved.startswith("corr_")


@pytest.mark.parametrize("value", NOT_STRINGS)
def test_a_header_id_wins_over_a_payload_field_that_is_not_one(value):
    assert (
        CorrelationContext.for_message(
            {"X-Correlation-ID": "from-the-wire"}, {"correlation_id": value}
        )
        == "from-the-wire"
    )


def test_CONTROL_a_string_payload_id_is_still_the_id():
    assert CorrelationContext.for_message({}, {"correlation_id": "abc-123"}) == "abc-123"


@pytest.mark.parametrize("value", NOT_STRINGS)
async def test_a_core_event_is_handled(value):
    svc = Core(ServiceConfig(name="orders", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    msg = AsyncMock()
    msg.subject, msg.headers = "events.order", {"Content-Type": "application/json"}
    msg.data = json.dumps({"correlation_id": value, "number": 1}).encode()
    SEEN.clear()

    await svc.container.event_dispatcher.handle_event(msg)

    assert isinstance(SEEN.get("id"), str)


@pytest.mark.parametrize("value", NOT_STRINGS)
async def test_a_first_delivery_is_handled_and_acknowledged(value):
    _, client = await _jetstream(Handling, {"correlation_id": value, "number": 1})

    assert _settled_with(client) == ["ack"]
    assert isinstance(SEEN.get("id"), str)


@pytest.mark.parametrize("value", NOT_STRINGS)
async def test_a_failing_delivery_on_its_last_attempt_is_dead_lettered_and_terminated(value):
    svc, client = await _jetstream(
        Handling, {"correlation_id": value, "number": 1}, num_delivered=5, fail=True
    )

    assert _settled_with(client) == ["term"]
    (record,) = _dead_letters(svc)
    assert isinstance(record["correlation_id"], str) and record["error"] == "RuntimeError"
    assert svc.container.dead_letters_lost == 0


@pytest.mark.parametrize("value", NOT_STRINGS)
async def test_an_invalid_message_is_dead_lettered_under_a_string_id(value):
    svc, client = await _jetstream(Validating, {"correlation_id": value, "number": "x"})

    assert _settled_with(client) == ["term"]
    (record,) = _dead_letters(svc)
    assert record["errors"]
    headers = svc.js.publish.await_args_list[0].kwargs["headers"]
    assert all(isinstance(v, str) for v in headers.values())


async def test_a_dead_letter_that_cannot_be_built_is_counted_and_the_delivery_is_terminated(
    monkeypatch,
):
    svc = Handling(_config())
    svc.fail = True
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the record cannot be built")

    monkeypatch.setattr(svc.container.dlq_publisher, "origin", refuse)
    msg, client = _delivery({"number": 1}, num_delivered=5)

    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )

    assert _settled_with(client) == ["term"]
    assert svc.js.publish.await_count == 0
    assert svc.container.dead_letters_lost == 1


async def test_a_dead_letter_call_that_raises_still_terminates_and_is_counted(monkeypatch):
    svc = Handling(_config())
    svc.fail = True
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("the dead letter publisher is broken")

    monkeypatch.setattr(svc.container.dlq_publisher, "dead_letter_terminated", explode)
    msg, client = _delivery({"number": 1}, num_delivered=5)

    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.order"), timeout=5
    )

    assert _settled_with(client) == ["term"]
    assert svc.container.dead_letters_lost == 1


@pytest.mark.parametrize("value", NOT_STRINGS)
async def test_the_invalid_message_publisher_given_no_id_does_not_take_a_payload_field_that_is_not_one(
    value,
):
    js = AsyncMock()
    connection = SimpleNamespace(jetstream_active=True, js=js, nc=AsyncMock())
    publisher = DeadLetterPublisher(_config(), lambda: connection)
    try:
        Order(number="x")  # type: ignore[arg-type]
    except Exception as error:
        refusal = error

    kept = await publisher.handle_invalid_message(
        "events.order", {"correlation_id": value, "number": "x"}, refusal, Order, "deadletter"
    )

    headers = js.publish.await_args_list[0].kwargs["headers"]
    assert kept is True
    assert all(isinstance(v, str) for v in headers.values()), headers
    recorded = json.loads(js.publish.await_args_list[0].args[1])["correlation_id"]
    # A new id, not the value read as text: a number in a payload field is not an id.
    assert recorded == headers["correlation_id"] and recorded.startswith("corr_")

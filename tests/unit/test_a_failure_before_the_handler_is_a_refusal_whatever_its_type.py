"""The phase decides whether a JetStream delivery is retried, not the type of the exception.

Everything that happens to a message before its handler is entered is a judgement of the message:
decoding it, validating it against a schema, building the model the handler is given. A redelivery
repeats it, so a failure there terminates the delivery whatever exception raised it. A model
validator that raises `TypeError`, which pydantic does not wrap in its own `ValidationError`, is
such a failure. Inside the handler, the same exception is the handler failing, and the delivery is
NAKed for another try.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import BaseModel, field_validator

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

SUBJECT = "events.order"


class Order(BaseModel):
    number: int

    @field_validator("number")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 0:
            raise ValueError("negative")
        # Not a ValueError or an AssertionError, so pydantic lets it through unwrapped.
        if value == 13:
            raise TypeError("unlucky")
        return value


def _service(kind: str, *, handler_raises: bool = False):
    handled: list[int] = []

    def body(number: int) -> None:
        if handler_raises:
            raise TypeError("the handler itself")
        handled.append(number)

    if kind == "validated":

        class Svc(CliffracerService):
            @validated_listener(SUBJECT, Order, fanout=True)
            async def on_order(self, message: Order) -> None:
                body(message.number)

    else:

        class Svc(CliffracerService):  # type: ignore[no-redef]
            @listener(SUBJECT, fanout=True)
            async def on_order(self, message: Order) -> None:
                body(message.number)

    svc = Svc(
        ServiceConfig(
            name="orders",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    return svc, handled


async def _deliver(kind: str, body: bytes, *, num_delivered: int = 1, handler_raises=False):
    svc, handled = _service(kind, handler_raises=handler_raises)
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = SUBJECT, body, {"Content-Type": "application/json"}
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    await asyncio.wait_for(svc.container._handle_jetstream_event(msg, pattern=SUBJECT), timeout=5)
    return svc, msg, handled


def _record(svc) -> dict:
    (call,) = svc.js.publish.await_args_list
    assert call.args[0] == "dlq.orders"
    return json.loads(call.args[1])


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_a_validator_that_raises_a_type_error_terminates_the_delivery_for_good(kind):
    svc, msg, handled = await _deliver(kind, b'{"number": 13}')

    assert (msg.term.await_count, msg.nak.await_count, msg.ack.await_count) == (1, 0, 0)
    assert handled == []
    record = _record(svc)
    assert record["original_subject"] == SUBJECT
    assert record["payload"] == {"number": 13}
    (error,) = record["errors"]
    assert error["type"] == "validator_raised" and "TypeError: unlucky" in error["msg"]


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_the_first_delivery_is_enough_no_redelivery_is_waited_for(kind):
    # The delivery limit is 5; a phase rule needs none of the five.
    _, msg, _ = await _deliver(kind, b'{"number": 13}', num_delivered=1)

    assert msg.nak.await_count == 0 and msg.term.await_count == 1


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_CONTROL_a_failure_pydantic_reports_still_terminates_with_its_own_errors(kind):
    svc, msg, handled = await _deliver(kind, b'{"number": -1}')

    assert (msg.term.await_count, msg.nak.await_count) == (1, 0)
    assert handled == []
    (error,) = _record(svc)["errors"]
    assert error["type"] == "value_error" and "negative" in error["msg"]


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_the_same_exception_inside_the_handler_is_the_handler_failing_and_is_retried(kind):
    svc, msg, handled = await _deliver(kind, b'{"number": 1}', handler_raises=True)

    assert (msg.nak.await_count, msg.term.await_count, msg.ack.await_count) == (1, 0, 0)
    assert handled == []
    assert svc.js.publish.await_count == 0, "nothing is dead-lettered before the delivery limit"


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_a_handler_failure_on_the_last_delivery_is_dead_lettered_as_a_terminated_message(
    kind,
):
    svc, msg, _ = await _deliver(kind, b'{"number": 1}', num_delivered=5, handler_raises=True)

    assert (msg.term.await_count, msg.nak.await_count) == (1, 0)
    record = _record(svc)
    assert record["deliveries"] == 5 and "errors" not in record


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_CONTROL_a_valid_message_is_handled_and_acknowledged(kind):
    svc, msg, handled = await _deliver(kind, b'{"number": 2}')

    assert handled == [2]
    assert (msg.ack.await_count, msg.nak.await_count, msg.term.await_count) == (1, 0, 0)
    assert svc.js.publish.await_count == 0


async def test_a_body_that_cannot_be_decoded_is_refused_whatever_the_decoder_raised(monkeypatch):
    def broken(*_args, **_kwargs):
        raise TypeError("the decoder is wrong, not the message")

    monkeypatch.setattr("cliffracer.core.dispatch.events.deserialize_payload", broken)

    svc, msg, handled = await _deliver("typed", b'{"number": 2}')

    assert (msg.term.await_count, msg.nak.await_count) == (1, 0)
    assert handled == []
    assert "Decode error: the decoder is wrong" in _record(svc)["error"]


def _capture():
    lines: list[tuple[str, str]] = []
    sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])))
    return lines, sink


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_the_defect_in_the_validator_is_logged_at_error_naming_the_exception(kind):
    lines, sink = _capture()
    try:
        await _deliver(kind, b'{"number": 13}')
    finally:
        logger.remove(sink)

    named = [
        (level, text) for level, text in lines if "TypeError: unlucky" in text and SUBJECT in text
    ]
    assert [level for level, _ in named] == ["ERROR"], lines
    assert "refused as invalid" in named[0][1]


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_CONTROL_a_message_pydantic_reports_invalid_is_not_an_error_line(kind):
    lines, sink = _capture()
    try:
        await _deliver(kind, b'{"number": -1}')
    finally:
        logger.remove(sink)

    assert not [text for level, text in lines if level == "ERROR"], lines


class _Unbuilt(BaseModel):
    number: "NotDefinedAnywhere"  # type: ignore[name-defined]  # noqa: F821


@pytest.mark.parametrize("kind", ["validated", "typed"])
async def test_a_schema_pydantic_cannot_build_is_refused_when_the_service_starts(kind):
    """So no message is ever validated against one: the TypeError it would raise cannot be reached."""
    if kind == "validated":

        class Svc(CliffracerService):
            @validated_listener(SUBJECT, _Unbuilt, fanout=True)
            async def on_order(self, message: _Unbuilt) -> None: ...

    else:

        class Svc(CliffracerService):  # type: ignore[no-redef]
            @listener(SUBJECT, fanout=True)
            async def on_order(self, message: _Unbuilt) -> None: ...

    with pytest.raises(TypeError, match="not fully defined"):
        Svc(ServiceConfig(name="orders")).container.discover_handlers()


async def test_with_on_invalid_drop_the_message_is_terminated_and_dead_letters_nothing():
    class Svc(CliffracerService):
        @validated_listener(SUBJECT, Order, on_invalid="drop", fanout=True)
        async def on_order(self, message: Order) -> None:
            raise AssertionError("an invalid message must not reach the handler")

    svc = Svc(
        ServiceConfig(
            name="orders",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject, msg.data = SUBJECT, b'{"number": 13}'
    msg.headers = {"Content-Type": "application/json"}
    msg.metadata = SimpleNamespace(num_delivered=1)

    await asyncio.wait_for(svc.container._handle_jetstream_event(msg, pattern=SUBJECT), timeout=5)

    assert (msg.term.await_count, msg.nak.await_count) == (1, 0)
    assert svc.js.publish.await_count == 0 and svc.nc.publish.await_count == 0

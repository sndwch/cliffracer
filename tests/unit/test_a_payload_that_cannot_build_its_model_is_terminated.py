"""A payload that can never be turned into the handler's model is terminated, not redelivered."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgpack
import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.dispatch.events import DispatchOutcome
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class Ping(BaseModel):
    seq: int


class Typed(CliffracerService):
    @listener("events.ping", durable="pinger")
    async def on_ping(self, seq: int) -> None:
        pass


class TypedModel(CliffracerService):
    @listener("events.ping", durable="pinger")
    async def on_ping(self, ping: Ping) -> None:
        pass


class Validated(CliffracerService):
    @validated_listener("events.ping", Ping, durable="pinger")
    async def on_ping(self, message: Ping) -> None:
        pass


SERVICES = [Typed, TypedModel, Validated]


def _msg(data: bytes, content_type: str, num_delivered: int = 1) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = data
    msg.headers = {"Content-Type": content_type}
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


def _service(service_class, **config):
    service = service_class(
        ServiceConfig(
            name="pinger",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.>"]),
            ],
            **config,
        )
    )
    service._discover_handlers()
    service.container.js = AsyncMock()
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("service_class", SERVICES, ids=lambda c: c.__name__)
async def test_a_msgpack_map_with_a_bytes_key_is_terminated_on_its_first_delivery(service_class):
    """`Ping(**{b"seq": 1})` is a TypeError, which was read as a failing handler: NAKed and
    redelivered until the limit. Pydantic reports the same payload as a missing field."""
    service = _service(service_class, serialization_format="msgpack")
    msg = _msg(msgpack.packb({b"seq": 1}), "application/msgpack")

    await service.container._handle_jetstream_event(msg, pattern="events.ping")

    msg.term.assert_awaited_once()
    msg.nak.assert_not_awaited()
    msg.ack.assert_not_awaited()
    subject, body = service.container.js.publish.await_args.args[:2]
    assert subject == "dlq.pinger"
    record = msgpack.unpackb(body, raw=False)
    assert "keywords must be strings" not in str(record), record


@pytest.mark.asyncio
@pytest.mark.parametrize("service_class", SERVICES, ids=lambda c: c.__name__)
async def test_the_dispatcher_reports_such_a_payload_as_invalid(service_class):
    service = _service(service_class, serialization_format="msgpack")
    msg = _msg(msgpack.packb({b"seq": 1}), "application/msgpack")

    outcome = await service.container.dispatcher.events.handle_event(
        msg, pattern="events.ping", raise_on_error=True
    )

    assert outcome is DispatchOutcome.INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("service_class", SERVICES, ids=lambda c: c.__name__)
async def test_CONTROL_a_string_keyed_payload_of_the_wrong_type_is_still_terminated(service_class):
    service = _service(service_class)
    msg = _msg(json.dumps({"seq": "no"}).encode(), "application/json")

    await service.container._handle_jetstream_event(msg, pattern="events.ping")

    msg.term.assert_awaited_once()
    msg.nak.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("service_class", SERVICES, ids=lambda c: c.__name__)
async def test_CONTROL_a_valid_msgpack_payload_is_acked(service_class):
    service = _service(service_class, serialization_format="msgpack")
    msg = _msg(msgpack.packb({"seq": 1}), "application/msgpack")

    await service.container._handle_jetstream_event(msg, pattern="events.ping")

    msg.ack.assert_awaited_once()
    msg.term.assert_not_awaited()
    msg.nak.assert_not_awaited()

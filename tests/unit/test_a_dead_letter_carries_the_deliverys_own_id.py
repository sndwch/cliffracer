"""A dead-letter record carries the id of the delivery it records, and never an ambient one."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.extension import Extension, RetryMessage
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

AMBIENT = "the-id-start-left-in-the-context"


def _config(**overrides) -> ServiceConfig:
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
        **overrides,
    )


def _msg(data: bytes = b'{"seq": 1}', headers=None, num_delivered: int = 1) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = data
    msg.headers = headers
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


def _record(service) -> tuple[str, str]:
    """The dead-letter publish's correlation id, from its header and from its body."""
    call = service.container.js.publish.await_args
    headers = call.kwargs["headers"]
    return headers["correlation_id"], json.loads(call.args[1])["correlation_id"]


def _service(service_class, **config):
    service = service_class(_config(**config))
    service._discover_handlers()
    service.container.js = AsyncMock()
    return service


def _failing(seen: dict):
    class Failing(CliffracerService):
        @listener("events.ping", durable="pinger")
        async def on_ping(self, seq: int) -> None:
            seen["handler"] = CorrelationContext.get()
            raise RuntimeError("boom")

    return Failing


@pytest.mark.asyncio
async def test_a_message_dead_lettered_at_the_limit_carries_the_id_its_handler_ran_under():
    seen: dict = {}
    service = _service(_failing(seen), jetstream_max_deliver=1)
    CorrelationContext.clear()

    await service.container._handle_jetstream_event(_msg(), pattern="events.ping")

    header_id, body_id = _record(service)
    assert seen["handler"], "the handler ran under an id"
    assert header_id == body_id == seen["handler"]


@pytest.mark.asyncio
async def test_a_wire_id_is_the_one_the_handler_ran_under_and_the_record_carries():
    seen: dict = {}
    service = _service(_failing(seen), jetstream_max_deliver=1)
    msg = _msg(headers={"X-Correlation-ID": "from-the-wire"})

    await service.container._handle_jetstream_event(msg, pattern="events.ping")

    assert seen["handler"] == "from-the-wire"
    assert _record(service) == ("from-the-wire", "from-the-wire")


@pytest.mark.asyncio
async def test_a_deferral_dead_lettered_at_the_limit_carries_the_id_of_its_delivery():
    ran_under: dict = {}

    class Defer(Extension):
        async def worker_setup(self, ctx) -> None:
            ran_under["id"] = ctx.correlation_id
            raise RetryMessage("quota exhausted")

    class Svc(CliffracerService):
        defer = Defer()

        @listener("events.ping", durable="pinger")
        async def on_ping(self, seq: int) -> None:
            pass

    service = Svc(_config(jetstream_max_deliver=1))
    await service.container._setup_extensions()
    service._discover_handlers()
    service.container.js = AsyncMock()

    await service.container._handle_jetstream_event(_msg(), pattern="events.ping")

    assert ran_under["id"]
    assert _record(service) == (ran_under["id"], ran_under["id"])


@pytest.mark.asyncio
async def test_an_ambient_id_does_not_stamp_a_message_dead_lettered_at_the_limit():
    """What start() left in the context belongs to no message."""
    seen: dict = {}
    service = _service(_failing(seen), jetstream_max_deliver=1)
    CorrelationContext.set(AMBIENT)
    try:
        await asyncio.create_task(
            service.container._handle_jetstream_event(_msg(), pattern="events.ping")
        )
    finally:
        CorrelationContext.clear()

    header_id, _ = _record(service)
    assert header_id != AMBIENT
    assert header_id == seen["handler"]


@pytest.mark.asyncio
async def test_undecodable_messages_are_not_stamped_with_an_ambient_id_or_with_each_other():
    service = _service(_failing({}))
    ids = []
    CorrelationContext.set(AMBIENT)
    try:
        for _ in range(2):
            service.container.js.publish.reset_mock()
            msg = _msg(data=b"{not json", headers={"Content-Type": "application/json"})
            await asyncio.create_task(
                service.container._handle_jetstream_event(msg, pattern="events.ping")
            )
            ids.append(_record(service))
    finally:
        CorrelationContext.clear()

    (first_header, first_body), (second_header, second_body) = ids
    assert first_header == first_body and second_header == second_body
    assert AMBIENT not in (first_header, second_header)
    assert first_header != second_header


@pytest.mark.asyncio
async def test_CONTROL_an_undecodable_message_with_a_wire_id_keeps_it():
    service = _service(_failing({}))
    msg = _msg(
        data=b"{not json",
        headers={"Content-Type": "application/json", "X-Correlation-ID": "from-the-wire"},
    )

    await service.container._handle_jetstream_event(msg, pattern="events.ping")

    assert _record(service) == ("from-the-wire", "from-the-wire")


@pytest.mark.asyncio
async def test_an_id_given_to_the_dead_letter_directly_is_used_over_the_wire_id():
    service = _service(_failing({}))
    msg = _msg(headers={"X-Correlation-ID": "from-the-wire"})

    await service.container._dead_letter_terminated(
        msg, RuntimeError("x"), 3, correlation_id="given-explicitly"
    )

    assert _record(service) == ("given-explicitly", "given-explicitly")


@pytest.mark.asyncio
async def test_a_dead_letter_with_no_id_of_its_own_gets_a_new_one_not_the_ambient_one():
    """No carried id, no wire id, no payload id: the record still must not take the ambient id.

    This is the last fallback. Every delivery above brings an id of its own, so none of them
    reaches it; a dead letter handed a bare message (a deadline overrun, whose exception was
    raised by the wrapper that never saw the dispatch's id) does.
    """
    service = _service(_failing({}))
    CorrelationContext.set(AMBIENT)
    try:
        await service.container._dead_letter_terminated(_msg(), RuntimeError("deadline"), 3)
    finally:
        CorrelationContext.clear()

    header_id, body_id = _record(service)
    assert header_id == body_id
    assert header_id and header_id != AMBIENT
    assert header_id.startswith("corr_")

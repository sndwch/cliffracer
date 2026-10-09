"""A `Message` the framework hands over carries the correlation id of its request.

`Message.correlation_id` is declared on every request, response and broadcast
model. The id itself travels in the envelope and the headers, and nothing
copied it into the model: an RPC reply shipped `"correlation_id": null` inside
its result, beside the envelope's real id, and a handler's request model or a
listener's message read `None` for a request that had one.

The framework now fills a `Message`'s `correlation_id` from the dispatch's id
wherever it builds one for a handler or takes one back as an RPC result, when
the field is `None`. Each assertion compares against the id the framework
itself used -- the envelope's, read off the wire -- rather than against "not
None", so a fill from the wrong source is caught too.

A value set explicitly is kept. Only `Message` is filled: another model may
declare a field of that name with a type a correlation id is not.
"""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import (
    BroadcastMessage,
    CliffracerService,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    async_rpc,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.core.correlation import CorrelationContext, correlation_id_var
from cliffracer.core.extension import Extension
from cliffracer.testing import ServiceTestHarness
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

RPC_ID = "corr-rpc"
EVENT_ID = "corr-ev"
EVENT_SUBJECT = "notifications.sent"


class OrderRequest(RPCRequest):
    item: str


class OrderResponse(RPCResponse):
    order_id: str


class PlainRequest(BaseModel):
    item: str
    correlation_id: str | None = None


class Notification(BroadcastMessage):
    notification_id: str


seen: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _clear_seen():
    seen.clear()
    yield
    seen.clear()


class Orders(CliffracerService):
    @rpc
    async def create(self, item: str) -> OrderResponse:
        return OrderResponse(order_id="o1")

    @rpc
    async def create_with_id(self, item: str) -> OrderResponse:
        return OrderResponse(order_id="o2", correlation_id="handler-set")

    @rpc
    async def read_request(self, request: OrderRequest) -> str:
        seen["request"] = request.correlation_id
        return "ok"

    @rpc
    async def read_plain_request(self, request: PlainRequest) -> str:
        seen["plain"] = request.correlation_id
        return "ok"

    @async_rpc
    async def read_request_async(self, request: OrderRequest) -> None:
        seen["async_request"] = request.correlation_id

    @async_rpc
    async def create_async(self, item: str) -> OrderResponse:
        return OrderResponse(order_id="o3")


class ResultRecorder(Extension):
    """What a `worker_result` hook is handed: the only place an async result goes."""

    name = "result_recorder"

    async def worker_result(self, ctx, result, exc) -> None:
        seen.setdefault("results", []).append(result)


async def _call(method: str, **payload):
    harness = ServiceTestHarness(Orders(ServiceConfig(name="orders")))
    try:
        reply = await harness.rpc(method, headers={"X-Correlation-ID": RPC_ID}, **payload)
    finally:
        await harness.teardown()
    return reply.data


@pytest.mark.asyncio
async def test_an_rpc_result_carries_the_envelopes_correlation_id():
    wire = await _call("create", item="x")

    assert wire["correlation_id"] == RPC_ID, wire
    assert wire["result"]["correlation_id"] == wire["correlation_id"], wire


@pytest.mark.asyncio
async def test_an_rpc_result_keeps_a_correlation_id_its_handler_set():
    wire = await _call("create_with_id", item="x")

    assert wire["result"]["correlation_id"] == "handler-set", wire
    assert wire["correlation_id"] == RPC_ID, wire


@pytest.mark.asyncio
async def test_an_rpc_request_model_carries_the_envelopes_correlation_id():
    wire = await _call("read_request", request={"item": "x"})

    assert wire["success"] is True, wire
    assert seen["request"] == wire["correlation_id"] == RPC_ID


@pytest.mark.asyncio
async def test_an_rpc_request_model_keeps_a_correlation_id_the_caller_set():
    wire = await _call("read_request", request={"item": "x", "correlation_id": "caller-set"})

    assert wire["success"] is True, wire
    assert seen["request"] == "caller-set"


@pytest.mark.asyncio
async def test_a_model_that_is_not_a_message_is_left_alone():
    wire = await _call("read_plain_request", request={"item": "x"})

    assert wire["success"] is True, wire
    assert seen["plain"] is None


@pytest.mark.asyncio
async def test_an_async_rpc_request_model_carries_its_correlation_id():
    harness = ServiceTestHarness(Orders(ServiceConfig(name="orders")))
    try:
        await harness.setup()
        msg = MockMessage(
            subject="orders.async.read_request_async",
            data=json.dumps({"request": {"item": "x"}}).encode(),
            headers={"X-Correlation-ID": RPC_ID, "Content-Type": "application/json"},
        )
        await harness._container.dispatcher.handle_async_request(msg)
    finally:
        await harness.teardown()

    assert seen["async_request"] == RPC_ID


@pytest.mark.asyncio
async def test_an_async_rpc_result_carries_its_correlation_id_to_the_result_hooks():
    """Async RPC sends no reply, so the result's only reader is a `worker_result` hook."""
    service = Orders(ServiceConfig(name="orders"))
    service.add_extension(ResultRecorder())
    harness = ServiceTestHarness(service)
    try:
        await harness.setup()
        msg = MockMessage(
            subject="orders.async.create_async",
            data=json.dumps({"item": "x"}).encode(),
            headers={"X-Correlation-ID": RPC_ID, "Content-Type": "application/json"},
        )
        await harness._container.dispatcher.handle_async_request(msg)
    finally:
        await harness.teardown()

    (result,) = seen["results"]
    assert isinstance(result, OrderResponse), result
    assert result.correlation_id == RPC_ID


async def _published_notification() -> tuple[bytes, dict]:
    """The wire bytes of a Notification published with no id of its own, under EVENT_ID."""
    publisher = CliffracerService(ServiceConfig(name="producer"))
    publisher.nc = AsyncMock()
    token = CorrelationContext.set(EVENT_ID)
    try:
        await publisher.publish_event(
            EVENT_SUBJECT,
            **Notification(source_service="producer", notification_id="n1").model_dump(mode="json"),
        )
    finally:
        correlation_id_var.reset(token)
    call = publisher.nc.publish.await_args
    return call.args[1], dict(call.kwargs.get("headers") or {})


async def _dispatch(service_class) -> dict:
    raw, headers = await _published_notification()
    receiver = service_class(ServiceConfig(name="receiver"))
    receiver.nc = AsyncMock()
    receiver._discover_handlers()
    msg = MockMessage(subject=EVENT_SUBJECT, data=raw, headers=headers)
    await receiver.container.dispatcher.events.handle_event(msg, pattern=EVENT_SUBJECT)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_a_validated_listener_message_carries_the_envelopes_correlation_id():
    class Receiver(CliffracerService):
        @validated_listener(EVENT_SUBJECT, Notification, fanout=True)
        async def on_notification(self, message: Notification) -> None:
            seen["validated"] = message.correlation_id

    wire = await _dispatch(Receiver)

    assert wire["correlation_id"] == EVENT_ID, wire
    assert seen["validated"] == wire["correlation_id"]


@pytest.mark.asyncio
async def test_a_typed_listener_message_carries_the_envelopes_correlation_id():
    class Receiver(CliffracerService):
        @listener(EVENT_SUBJECT, fanout=True)
        async def on_notification(self, message: Notification) -> None:
            seen["typed"] = message.correlation_id

    wire = await _dispatch(Receiver)

    assert wire["correlation_id"] == EVENT_ID, wire
    assert seen["typed"] == wire["correlation_id"]

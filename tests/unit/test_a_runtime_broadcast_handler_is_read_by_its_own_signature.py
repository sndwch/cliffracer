"""A handler added with `register_broadcast_handler` is validated against its own signature.

Discovery records an event spec under every subject a `@listener` or `@broadcast` declares.
`register_broadcast_handler` recorded none, so dispatch found a spec by the handler's NAME,
in an index of the discovered methods. A handler that merely shared a name with a discovered
method was called with that method's arguments.

Every case dispatches through `container._dispatch_event`, as a subscription does.
"""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class Order(BaseModel):
    order_id: str
    total: float | None = None


async def _emit(svc: CliffracerService, subject: str, body: dict) -> DispatchOutcome:
    message = MockMessage(
        subject=subject,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return await svc.container._dispatch_event(message, pattern=subject)


def _service(received: list) -> CliffracerService:
    class Svc(CliffracerService):
        @listener("orders.drafted", fanout=True)
        async def on_order(self, message: Order) -> None:
            received.append(("method", message))

    svc = Svc(ServiceConfig(name="svc", subject_prefix=None))
    svc._discover_handlers()
    return svc


@pytest.mark.asyncio
async def test_a_function_that_shares_a_discovered_methods_name_gets_its_own_arguments():
    received: list = []
    svc = _service(received)

    async def on_order(order_id: str) -> None:
        received.append(("function", order_id))

    svc.register_broadcast_handler("admin.orders", on_order)

    assert await _emit(svc, "admin.orders", {"order_id": "o1"}) == DispatchOutcome.OK
    assert received == [("function", "o1")]


@pytest.mark.asyncio
async def test_a_typed_method_registered_on_another_subject_receives_its_model():
    received: list = []
    svc = _service(received)

    svc.register_broadcast_handler("admin.orders", svc.on_order)

    assert await _emit(svc, "admin.orders", {"order_id": "o1"}) == DispatchOutcome.OK
    assert received == [("method", Order(order_id="o1"))]


def test_the_registration_records_a_spec_under_the_subject_as_discovery_does():
    svc = _service([])

    svc.register_broadcast_handler("admin.orders", svc.on_order)

    spec = svc.container.registry.event_specs_by_subject["admin.orders"]
    assert spec.name == "on_order" and spec.payload_model is Order


@pytest.mark.asyncio
async def test_a_handler_without_annotations_still_receives_the_payload_as_keyword_arguments():
    """Runtime registration accepts a handler discovery would refuse; it has no spec."""
    received: list = []
    svc = _service(received)

    async def untyped(order_id, total=None):
        received.append((order_id, total))

    svc.register_broadcast_handler("admin.untyped", untyped)

    assert "admin.untyped" not in svc.container.registry.event_specs_by_subject
    assert await _emit(svc, "admin.untyped", {"order_id": "o1", "total": 2.5}) == DispatchOutcome.OK
    assert received == [("o1", 2.5)]


@pytest.mark.asyncio
async def test_registering_an_untyped_handler_over_a_typed_one_drops_the_typed_spec():
    received: list = []
    svc = _service(received)
    svc.register_broadcast_handler("admin.orders", svc.on_order)

    async def untyped(order_id, total=None):
        received.append(("untyped", order_id, total))

    svc.register_broadcast_handler("admin.orders", untyped)

    assert "admin.orders" not in svc.container.registry.event_specs_by_subject
    assert await _emit(svc, "admin.orders", {"order_id": "o1"}) == DispatchOutcome.OK
    assert received == [("untyped", "o1", None)]


@pytest.mark.asyncio
async def test_a_typed_handler_that_is_given_a_payload_it_does_not_declare_is_judged_invalid():
    received: list = []
    svc = _service(received)

    async def typed(order_id: str) -> None:
        received.append(order_id)

    svc.register_broadcast_handler("admin.typed", typed)

    assert await _emit(svc, "admin.typed", {"order_id": 7}) == DispatchOutcome.INVALID
    assert received == []


@pytest.mark.asyncio
async def test_the_spec_is_recorded_under_the_namespaced_subject_dispatch_looks_up():
    received: list = []

    class Svc(CliffracerService):
        pass

    svc = Svc(ServiceConfig(name="svc", namespace="prod", subject_prefix=None))

    async def typed(order_id: str) -> None:
        received.append(order_id)

    svc.register_broadcast_handler("admin.typed", typed)

    registry = svc.container.registry
    assert list(registry.event_specs_by_subject) == ["prod.admin.typed"]
    assert await _emit(svc, "prod.admin.typed", {"order_id": 7}) == DispatchOutcome.INVALID
    assert received == []


@pytest.mark.asyncio
async def test_registering_a_second_typed_handler_on_a_subject_replaces_the_first_ones_spec():
    received: list = []
    svc = _service(received)

    async def first(order_id: str) -> None:
        received.append(("first", order_id))

    async def second(sku: str) -> None:
        received.append(("second", sku))

    svc.register_broadcast_handler("admin.orders", first)
    svc.register_broadcast_handler("admin.orders", second)

    assert await _emit(svc, "admin.orders", {"sku": "s1"}) == DispatchOutcome.OK
    assert received == [("second", "s1")]

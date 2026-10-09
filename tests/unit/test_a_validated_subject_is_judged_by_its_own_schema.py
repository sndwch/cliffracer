"""Each subject a `@validated_listener` declares is validated against the schema declared for it.

A method may carry several event declarations. The schema for one subject used
to be stored against the method, so with two `@validated_listener` decorators
the last one applied judged both subjects, and a valid message on the other was
dead-lettered. And when the same method also carried a `@listener`, the typed
listener's signature was found for the validated subject by method name before
any schema was consulted, so the schema was never applied at all.

Every case dispatches through `container._dispatch_event`, as a subscription
does.
"""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class LenientOrder(BaseModel):
    order_id: str
    total: float | None = None


class Order(LenientOrder):
    total: float


class Payment(BaseModel):
    payment_id: str


async def _emit(svc: CliffracerService, subject: str, body: dict) -> DispatchOutcome:
    message = MockMessage(
        subject=subject,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return await svc.container._dispatch_event(message, pattern=subject)


def _started(service_class) -> CliffracerService:
    svc = service_class(ServiceConfig(name="svc"))
    svc._discover_handlers()
    return svc


async def test_stacked_validated_listeners_each_judge_their_own_subject():
    received: list[BaseModel] = []

    class Svc(CliffracerService):
        @validated_listener("orders.created", Order, fanout=True)
        @validated_listener("payments.made", Payment, fanout=True)
        async def on_event(self, message: BaseModel) -> None:
            received.append(message)

    svc = _started(Svc)

    assert await _emit(svc, "payments.made", {"payment_id": "p1"}) == DispatchOutcome.OK
    assert (
        await _emit(svc, "orders.created", {"order_id": "o1", "total": 2.5}) == DispatchOutcome.OK
    )
    assert received == [Payment(payment_id="p1"), Order(order_id="o1", total=2.5)]


async def test_a_stacked_subject_still_refuses_what_its_own_schema_refuses():
    received: list[BaseModel] = []

    class Svc(CliffracerService):
        @validated_listener("orders.created", Order, fanout=True)
        @validated_listener("payments.made", Payment, fanout=True)
        async def on_event(self, message: BaseModel) -> None:
            received.append(message)

    svc = _started(Svc)

    assert await _emit(svc, "orders.created", {"order_id": "o1"}) == DispatchOutcome.INVALID
    assert await _emit(svc, "payments.made", {"order_id": "o1"}) == DispatchOutcome.INVALID
    assert received == []


async def test_a_validated_subject_on_a_method_that_also_listens_applies_its_schema():
    """The plain subject reads the handler's signature; the validated one its schema."""
    received: list[BaseModel] = []

    class Svc(CliffracerService):
        @listener("orders.drafted", fanout=True)
        @validated_listener("orders.created", Order, fanout=True)
        async def on_order(self, message: LenientOrder) -> None:
            received.append(message)

    svc = _started(Svc)

    assert await _emit(svc, "orders.created", {"order_id": "o1"}) == DispatchOutcome.INVALID
    assert received == []

    assert await _emit(svc, "orders.drafted", {"order_id": "o2"}) == DispatchOutcome.OK
    assert received == [LenientOrder(order_id="o2", total=None)]


async def test_CONTROL_a_single_validated_listener_is_unchanged():
    received: list[BaseModel] = []

    class Svc(CliffracerService):
        @validated_listener("orders.created", Order, fanout=True)
        async def on_order(self, message: Order) -> None:
            received.append(message)

    svc = _started(Svc)

    assert await _emit(svc, "orders.created", {"order_id": "o1"}) == DispatchOutcome.INVALID
    assert (
        await _emit(svc, "orders.created", {"order_id": "o1", "total": 1.0}) == DispatchOutcome.OK
    )
    assert received == [Order(order_id="o1", total=1.0)]


async def test_a_typed_method_registered_at_runtime_receives_its_model():
    """`register_broadcast_handler` records a spec under the subject it registers, read from
    the method's own signature, as discovery does for a declared one."""
    received: list[BaseModel] = []

    class Svc(CliffracerService):
        @listener("orders.drafted", fanout=True)
        async def on_order(self, message: LenientOrder) -> None:
            received.append(message)

    svc = Svc(ServiceConfig(name="svc", subject_prefix=None))
    svc._discover_handlers()
    svc.register_broadcast_handler("admin.orders", svc.on_order)

    assert await _emit(svc, "admin.orders", {"order_id": "o1"}) == DispatchOutcome.OK
    assert received == [LenientOrder(order_id="o1", total=None)]

"""Validated event handlers have one startup-checked invocation shape."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener
from cliffracer.core.typed_rpc import UntypedHandler

pytestmark = pytest.mark.unit


class OrderReleased(BaseModel):
    order_id: str


class OrderCreated(BaseModel):
    order_id: str


class InventoryAdjusted(BaseModel):
    sku: str


class _Message:
    subject = "orders.released"
    headers = {"correlation-id": "dispatch-17"}

    def __init__(self) -> None:
        self.data = json.dumps({"order_id": "order-17"}).encode()


async def test_a_validated_listener_uses_its_declared_payload_parameter_name() -> None:
    received: list[OrderReleased] = []

    class Fulfilment(CliffracerService):
        @validated_listener("orders.released", OrderReleased, fanout=True)
        async def record_release(self, order: OrderReleased) -> None:
            received.append(order)

    service = Fulfilment(ServiceConfig(name="fulfilment"))
    service._discover_handlers()

    await service.container._handle_event(_Message())

    assert received == [OrderReleased(order_id="order-17")]


async def test_startup_refuses_a_scalar_payload_before_connecting(monkeypatch) -> None:
    class Fulfilment(CliffracerService):
        @validated_listener("orders.released", OrderReleased, fanout=True)
        async def record_release(self, message: str) -> None:
            pass

    service = Fulfilment(ServiceConfig(name="fulfilment"))
    connected: list[bool] = []

    class BrokerContacted(RuntimeError):
        pass

    async def connect() -> None:
        connected.append(True)
        raise BrokerContacted("startup reached the broker connection step")

    monkeypatch.setattr(service, "connect", connect)

    with pytest.raises(UntypedHandler, match=r"record_release.*OrderReleased"):
        await service.start()

    assert connected == []


def test_a_validated_listener_refuses_an_unannotated_message() -> None:
    class Fulfilment(CliffracerService):
        @validated_listener("orders.released", OrderReleased, fanout=True)
        async def record_release(self, message) -> None:
            pass

    service = Fulfilment(ServiceConfig(name="fulfilment"))

    with pytest.raises(UntypedHandler, match=r"record_release.*'message'.*no annotation"):
        service._discover_handlers()


def test_a_validated_listener_refuses_additional_payload_parameters() -> None:
    class Fulfilment(CliffracerService):
        @validated_listener("orders.released", OrderReleased, fanout=True)
        async def record_release(self, message: OrderReleased, warehouse: str) -> None:
            pass

    service = Fulfilment(ServiceConfig(name="fulfilment"))

    with pytest.raises(UntypedHandler, match=r"record_release.*exactly one payload parameter"):
        service._discover_handlers()


def test_a_validated_listener_refuses_an_unrelated_model_annotation() -> None:
    class Fulfilment(CliffracerService):
        @validated_listener("orders.released", OrderReleased, fanout=True)
        async def record_release(self, adjustment: InventoryAdjusted) -> None:
            pass

    service = Fulfilment(ServiceConfig(name="fulfilment"))

    with pytest.raises(UntypedHandler, match=r"OrderReleased.*not compatible.*InventoryAdjusted"):
        service._discover_handlers()


async def test_the_documented_validated_listener_shape_receives_model_and_metadata() -> None:
    received: list[tuple[OrderReleased, str, str | None]] = []

    class Fulfilment(CliffracerService):
        @validated_listener("orders.released", OrderReleased, fanout=True)
        async def record_release(
            self,
            message: OrderReleased,
            subject: str,
            correlation_id: str | None,
        ) -> None:
            received.append((message, subject, correlation_id))

    service = Fulfilment(ServiceConfig(name="fulfilment"))
    service._discover_handlers()

    await service.container._handle_event(_Message())

    assert received == [(OrderReleased(order_id="order-17"), "orders.released", "dispatch-17")]


async def test_overlapping_validated_routes_keep_their_own_parameter_names_and_models() -> None:
    received: list[tuple[str, BaseModel]] = []

    class Fulfilment(CliffracerService):
        @validated_listener("orders.*", OrderReleased, fanout=True)
        async def audit_release(self, release: OrderReleased) -> None:
            received.append(("release", release))

        @validated_listener("orders.released", OrderCreated, fanout=True)
        async def reserve_stock(self, created: OrderCreated) -> None:
            received.append(("created", created))

    service = Fulfilment(ServiceConfig(name="fulfilment"))
    service._discover_handlers()

    await service.container._handle_event(_Message())

    assert received == [
        ("release", OrderReleased(order_id="order-17")),
        ("created", OrderCreated(order_id="order-17")),
    ]

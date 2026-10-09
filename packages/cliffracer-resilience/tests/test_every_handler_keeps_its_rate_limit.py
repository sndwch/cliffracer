"""Every declared rate limit remains attached to its own business operation."""

from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import (
    RateLimitConfig,
    RateLimitExceeded,
    ResilienceExtension,
    rate_limit,
)
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, broadcast, listener, validated_listener
from cliffracer.core.extension import WorkerContext
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


class InvoiceApproved(BaseModel):
    invoice_id: str


class FulfillmentService(CliffracerService):
    resilience = ResilienceExtension()

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="fulfillment", namespace="store"))
        self.handled: list[tuple[str, str]] = []

    @listener("orders.*", fanout=True)
    @rate_limit(calls=1, window=60)
    async def on_order(self, order_id: str) -> None:
        self.handled.append(("order", order_id))

    @listener("orders.created", fanout=True)
    @rate_limit(calls=2, window=60)
    async def on_created_order(self, order_id: str) -> None:
        self.handled.append(("created", order_id))

    @validated_listener("invoices.approved", InvoiceApproved, fanout=True)
    @rate_limit(calls=1, window=60)
    async def on_invoice(self, message: InvoiceApproved) -> None:
        self.handled.append(("invoice", message.invoice_id))

    @broadcast("inventory.changed")
    @rate_limit(calls=1, window=60)
    async def on_inventory(self, sku: str) -> None:
        self.handled.append(("inventory", sku))


async def dispatch(service: FulfillmentService, pattern: str, subject: str, payload: bytes) -> None:
    message = MockMessage(subject=subject, data=payload, headers={})
    await service.container.dispatcher.events.handle_event(message, pattern=pattern)


async def test_namespaced_wildcard_validated_and_broadcast_handlers_enforce_their_limits():
    service = FulfillmentService()
    await service.container._setup_extensions()
    service._discover_handlers()

    deliveries = (
        ("store.orders.*", "store.orders.created", b'{"order_id":"order-1"}'),
        (
            "store.invoices.approved",
            "store.invoices.approved",
            b'{"invoice_id":"invoice-1"}',
        ),
        ("store.inventory.changed", "store.inventory.changed", b'{"sku":"sku-1"}'),
    )
    for pattern, subject, payload in deliveries:
        await dispatch(service, pattern, subject, payload)
        await dispatch(service, pattern, subject, payload)

    assert service.handled == [
        ("order", "order-1"),
        ("invoice", "invoice-1"),
        ("inventory", "sku-1"),
    ]
    assert set(service.resilience.limiter._windows) == {
        "store.fulfillment:on_order",
        "store.fulfillment:on_invoice",
        "store.fulfillment:on_inventory",
    }


async def test_a_dispatched_limit_does_not_disable_a_nested_operation_limit():
    extension = ResilienceExtension()
    extension._rate_limits["checkout"] = RateLimitConfig(calls=10, window=60)
    context = WorkerContext(
        kind="rpc",
        subject="store.rpc.checkout",
        headers={},
        correlation_id="checkout-1",
        payload={},
        data={"handler_name": "checkout"},
    )
    reserve_inventory = AsyncMock(return_value="reserved")

    @rate_limit(calls=1, window=60)
    async def reserve(sku: str) -> str:
        return await reserve_inventory(sku)

    await extension.worker_setup(context)
    try:
        assert await reserve("sku-1") == "reserved"
        with pytest.raises(RateLimitExceeded):
            await reserve("sku-1")
    finally:
        await extension.worker_teardown(context)

    reserve_inventory.assert_awaited_once_with("sku-1")


async def test_overlapping_event_routes_keep_independent_handler_budgets():
    service = FulfillmentService()
    await service.container._setup_extensions()
    service._discover_handlers()
    message = MockMessage(
        subject="store.orders.created",
        data=b'{"order_id":"order-2"}',
        headers={},
    )

    await service.container.dispatcher.events.handle_event(message)
    await service.container.dispatcher.events.handle_event(message)

    assert service.handled.count(("order", "order-2")) == 1
    assert service.handled.count(("created", "order-2")) == 2


async def test_a_reused_limit_declaration_keeps_each_operation_independent():
    common_limit = rate_limit(calls=1, window=60)

    @common_limit
    async def checkout(order_id: str) -> str:
        return order_id

    @common_limit
    async def reserve_inventory(sku: str) -> str:
        return sku

    extension = ResilienceExtension()
    checkout_config = checkout._cliffracer_rate_limit  # type: ignore[attr-defined]
    extension._rate_limits["checkout"] = checkout_config
    context = WorkerContext(
        kind="rpc",
        subject="store.rpc.checkout",
        headers={},
        correlation_id="checkout-2",
        payload={},
        data={"handler_name": "checkout"},
    )

    await extension.worker_setup(context)
    try:
        assert await reserve_inventory("sku-2") == "sku-2"
        with pytest.raises(RateLimitExceeded):
            await reserve_inventory("sku-2")
    finally:
        await extension.worker_teardown(context)

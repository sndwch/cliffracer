"""Validating event dispatch: valid -> handler(model); invalid -> deadletter/drop."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener


class OrderCreated(BaseModel):
    order_id: str
    amount: float


class _MockMsg:
    def __init__(self, subject, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers = None


def _make_service(on_invalid=None, default="deadletter"):
    class OrderSvc(CliffracerService):
        received: list = []

        @validated_listener("orders.created", OrderCreated, on_invalid=on_invalid, fanout=True)
        async def on_order(self, message: OrderCreated):
            type(self).received.append(message)

    svc = OrderSvc(ServiceConfig(name="order_svc", default_on_invalid=default))
    svc.received.clear()
    svc._discover_handlers()
    svc.publish_event = AsyncMock()  # capture dead-letters without NATS
    svc.container._publish_dlq = AsyncMock()
    return svc


@pytest.mark.unit
def test_discovery_registers_schema():
    svc = _make_service()
    assert "orders.created" in svc.container.registry.event_handlers
    assert any(s is OrderCreated for s, _ in svc.container.registry.event_schemas.values())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_message_reaches_handler_as_model():
    svc = _make_service()
    await svc.container._handle_event(_MockMsg("orders.created", {"order_id": "o1", "amount": 9.5}))
    assert len(svc.received) == 1
    assert isinstance(svc.received[0], OrderCreated)
    assert svc.received[0].order_id == "o1"
    svc.publish_event.assert_not_called()
    svc.container._publish_dlq.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_message_dead_letters():
    svc = _make_service(default="deadletter")
    await svc.container._handle_event(
        _MockMsg("orders.created", {"order_id": "o1"})
    )  # missing amount
    assert svc.received == []  # handler NOT called
    svc.container._publish_dlq.assert_called_once()
    dlq_subject = svc.container._publish_dlq.call_args.args[0]
    kwargs = svc.container._publish_dlq.call_args.kwargs
    assert dlq_subject == "dlq.order_svc"
    assert kwargs["original_subject"] == "orders.created"
    assert any(e.get("loc") == ["amount"] for e in kwargs["errors"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drop_does_not_publish():
    svc = _make_service(on_invalid="drop")
    await svc.container._handle_event(_MockMsg("orders.created", {"order_id": "o1"}))  # invalid
    assert svc.received == []
    svc.publish_event.assert_not_called()
    svc.container._publish_dlq.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_dict_message_routes_to_on_invalid_policy():
    """Verify non-dict payload routes to on_invalid policy without TypeError."""
    svc = _make_service(on_invalid="drop")
    await svc.container._handle_event(_MockMsg("orders.created", 123))
    assert svc.received == []
    svc.publish_event.assert_not_called()

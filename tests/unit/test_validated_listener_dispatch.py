"""Validating event dispatch: valid -> handler(model); invalid -> deadletter/drop."""

import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, validated_listener
from cliffracer.core.dispatch.events import DispatchOutcome

pytestmark = pytest.mark.unit


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


@pytest.fixture
def warnings_logged():
    """The warnings the framework logs, as text, for the length of one test."""
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="WARNING")
    yield lines
    logger.remove(sink)


async def _handle(svc, msg):
    """Dispatch with errors allowed to escape: a crash on the validated path must not be
    swallowed and logged, which is what the default does."""
    return await svc.container._handle_event(msg, raise_on_error=True)


@pytest.mark.parametrize("on_invalid", [None, "drop"])
def test_discovery_registers_schema(on_invalid):
    """The schema is keyed by the subject the handler is bound to, with its on_invalid."""
    svc = _make_service(on_invalid=on_invalid)
    registry = svc.container.registry
    assert "orders.created" in registry.event_handlers
    assert registry.event_schemas["orders.created"] == (OrderCreated, on_invalid)
    assert set(registry.event_schemas) == {"orders.created"}


@pytest.mark.asyncio
async def test_valid_message_reaches_handler_as_model():
    svc = _make_service()
    await svc.container._handle_event(_MockMsg("orders.created", {"order_id": "o1", "amount": 9.5}))
    assert len(svc.received) == 1
    assert isinstance(svc.received[0], OrderCreated)
    assert svc.received[0].order_id == "o1"
    svc.publish_event.assert_not_called()
    svc.container._publish_dlq.assert_not_called()


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


@pytest.mark.asyncio
async def test_an_invalid_message_is_dropped_under_the_drop_policy(warnings_logged):
    svc = _make_service(on_invalid="drop")

    outcome = await _handle(svc, _MockMsg("orders.created", {"order_id": "o1"}))  # missing amount

    assert outcome is DispatchOutcome.INVALID
    assert svc.received == []
    svc.container._publish_dlq.assert_not_called()
    # The decision is recorded: dropping is not the same as never having validated.
    assert any("Dropped invalid message on 'orders.created'" in line for line in warnings_logged)
    assert not any("Dead-lettered" in line for line in warnings_logged)


@pytest.mark.asyncio
async def test_an_invalid_message_is_judged_invalid_when_it_is_dead_lettered():
    svc = _make_service(on_invalid="deadletter")

    outcome = await _handle(svc, _MockMsg("orders.created", {"order_id": "o1"}))

    assert outcome is DispatchOutcome.INVALID
    svc.container._publish_dlq.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["drop", "deadletter"])
async def test_non_dict_message_routes_to_on_invalid_policy(policy, warnings_logged):
    """A payload that is not an object is judged invalid and handled by the declared policy.

    Both policies are run, so the one that was applied is what the assertions read.
    """
    svc = _make_service(on_invalid=policy)

    outcome = await _handle(svc, _MockMsg("orders.created", 123))

    assert outcome is DispatchOutcome.INVALID
    assert svc.received == []
    dropped = any("Dropped invalid message" in line for line in warnings_logged)
    if policy == "drop":
        svc.container._publish_dlq.assert_not_called()
        assert dropped
    else:
        svc.container._publish_dlq.assert_called_once()
        assert not dropped

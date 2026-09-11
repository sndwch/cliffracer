"""Unit tests for event wire envelope standardization.

Verifies canonical event wire envelope wrapping in publish_event, compatibility
for legacy flat payloads, and transparent unwrapping in _dispatch_event for both
@validated_listener and @listener.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, Field

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    listener,
    validated_listener,
)
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.correlation import CorrelationContext


class OrderCreated(BaseModel):
    order_id: str
    amount: float = Field(gt=0)


class MockMsg:
    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        reply: str | None = None,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.reply = reply


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_event_emits_canonical_envelope_by_default() -> None:
    """publish_event wraps domain kwargs into canonical event wire envelope by default."""
    cfg = ServiceConfig(name="orders_svc", version="1.0.0")
    svc = CliffracerService(cfg)
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    svc.nc = mock_nc

    CorrelationContext.set("corr-pub-001")
    try:
        await svc.publish_event("orders.created", order_id="ord-99", amount=49.95)

        mock_nc.publish.assert_called_once()
        call_subject = mock_nc.publish.call_args.args[0]
        call_bytes = mock_nc.publish.call_args.args[1]
        call_headers = mock_nc.publish.call_args.kwargs["headers"]

        assert call_subject == "orders.created"
        envelope = json.loads(call_bytes.decode())

        assert "data" in envelope
        assert envelope["data"]["order_id"] == "ord-99"
        assert envelope["data"]["amount"] == 49.95
        assert envelope["source_service"] == "orders_svc"
        assert envelope["correlation_id"] == "corr-pub-001"

        # Timestamp is valid ISO 8601
        ts = datetime.fromisoformat(envelope["timestamp"])
        assert ts.tzinfo is not None

        # Headers carry X-Correlation-ID and correlation_id
        assert call_headers["X-Correlation-ID"] == "corr-pub-001"
        assert call_headers["correlation_id"] == "corr-pub-001"
    finally:
        CorrelationContext.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_event_envelope_false_emits_flat_payload() -> None:
    """publish_event with envelope=False emits a flat payload for legacy compatibility."""
    cfg = ServiceConfig(name="orders_svc", version="1.0.0")
    svc = CliffracerService(cfg)
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    svc.nc = mock_nc

    await svc.publish_event(
        "orders.created", envelope=False, correlation_id="cid-flat", order_id="ord-1"
    )

    call_bytes = mock_nc.publish.call_args.args[1]
    payload = json.loads(call_bytes.decode())
    assert "data" not in payload
    assert payload["order_id"] == "ord-1"
    assert payload["correlation_id"] == "cid-flat"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_event_with_prepackaged_data() -> None:
    """publish_event with data={...} wraps without double-nesting."""
    cfg = ServiceConfig(name="orders_svc", version="1.0.0")
    svc = CliffracerService(cfg)
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    svc.nc = mock_nc

    await svc.publish_event(
        "orders.created",
        data={"order_id": "ord-2", "amount": 10.0},
        correlation_id="cid-prepack",
    )

    call_bytes = mock_nc.publish.call_args.args[1]
    envelope = json.loads(call_bytes.decode())
    assert envelope["data"] == {"order_id": "ord-2", "amount": 10.0}
    assert envelope["correlation_id"] == "cid-prepack"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validated_listener_unwraps_canonical_event_envelope() -> None:
    """@validated_listener unwraps data['data'] before schema validation."""
    received = []

    class OrderService(CliffracerService):
        @validated_listener("orders.created", schema=OrderCreated, fanout=True)
        async def on_order(self, message: OrderCreated, correlation_id: str | None = None) -> None:
            received.append((message, correlation_id))

    svc = OrderService(ServiceConfig(name="test_service", version="1.0.0"))
    svc._discover_handlers()

    envelope = {
        "data": {"order_id": "ord-100", "amount": 19.99},
        "timestamp": datetime.now().isoformat(),
        "source_service": "upstream_svc",
        "correlation_id": "corr-evt-001",
    }
    raw_msg = MockMsg(
        subject="orders.created",
        data=json.dumps(envelope).encode(),
        headers={"X-Correlation-ID": "corr-evt-001"},
    )

    outcome = await svc.container._dispatch_event(raw_msg, pattern="orders.created")
    assert outcome == DispatchOutcome.OK
    assert len(received) == 1
    model, cid = received[0]
    assert isinstance(model, OrderCreated)
    assert model.order_id == "ord-100"
    assert model.amount == 19.99
    assert cid == "corr-evt-001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validated_listener_supports_legacy_flat_payload() -> None:
    """@validated_listener maintains backward compatibility for legacy flat payloads."""
    received = []

    class OrderService(CliffracerService):
        @validated_listener("orders.created", schema=OrderCreated, fanout=True)
        async def on_order(self, message: OrderCreated) -> None:
            received.append(message)

    svc = OrderService(ServiceConfig(name="test_service", version="1.0.0"))
    svc._discover_handlers()

    flat_payload = {"order_id": "ord-flat-1", "amount": 25.0, "correlation_id": "c-1"}
    raw_msg = MockMsg(
        subject="orders.created",
        data=json.dumps(flat_payload).encode(),
    )

    outcome = await svc.container._dispatch_event(raw_msg, pattern="orders.created")
    assert outcome == DispatchOutcome.OK
    assert len(received) == 1
    assert received[0].order_id == "ord-flat-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unvalidated_listener_unwraps_canonical_envelope_kwargs() -> None:
    """Unvalidated @listener receives domain fields as kwargs from canonical envelope."""
    received = []

    class EventService(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(
            self, order_id: str, amount: float, correlation_id: str | None = None
        ) -> None:
            received.append((order_id, amount, correlation_id))

    svc = EventService(ServiceConfig(name="test_service", version="1.0.0"))
    svc._discover_handlers()

    envelope = {
        "data": {"order_id": "ord-200", "amount": 88.0},
        "timestamp": datetime.now().isoformat(),
        "source_service": "billing_svc",
        "correlation_id": "corr-raw-1",
    }
    raw_msg = MockMsg(
        subject="orders.created",
        data=json.dumps(envelope).encode(),
        headers={"X-Correlation-ID": "corr-raw-1"},
    )

    outcome = await svc.container._dispatch_event(raw_msg, pattern="orders.created")
    assert outcome == DispatchOutcome.OK
    assert len(received) == 1
    assert received[0] == ("ord-200", 88.0, "corr-raw-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unvalidated_listener_with_data_parameter() -> None:
    """If @listener handler signature specifies 'data', it receives domain_payload."""
    received = []

    class GenericData(BaseModel):
        item: str
        qty: int

    class EventService(CliffracerService):
        @listener("events.generic", fanout=True)
        async def on_generic(self, data: GenericData, subject: str) -> None:
            received.append((data.model_dump(), subject))

    svc = EventService(ServiceConfig(name="test_service", version="1.0.0"))
    svc._discover_handlers()

    envelope = {
        "data": {"item": "gadget", "qty": 5},
        "timestamp": datetime.now().isoformat(),
        "source_service": "inventory_svc",
        "correlation_id": "corr-gen-1",
    }
    raw_msg = MockMsg(
        subject="events.generic",
        data=json.dumps(envelope).encode(),
    )

    outcome = await svc.container._dispatch_event(raw_msg, pattern="events.generic")
    assert outcome == DispatchOutcome.OK
    assert len(received) == 1
    data_arg, subj_arg = received[0]
    assert data_arg == {"item": "gadget", "qty": 5}
    assert subj_arg == "events.generic"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broadcast_message_reaches_validated_listener_cleanly() -> None:
    """Messages emitted via broadcast_message unwrap cleanly at @validated_listener."""
    received = []

    class BroadcastConsumerService(CliffracerService):
        @validated_listener("system.alerts", schema=OrderCreated, fanout=True)
        async def on_alert(self, message: OrderCreated) -> None:
            received.append(message)

    svc = BroadcastConsumerService(ServiceConfig(name="test_consumer", version="1.0.0"))
    svc._discover_handlers()

    # Simulates what broadcast_message produces on the wire
    wire_msg = {
        "data": {"order_id": "alert-1", "amount": 10.0},
        "timestamp": datetime.now().isoformat(),
        "source_service": "alert_producer",
        "correlation_id": "corr-alert",
    }
    raw_msg = MockMsg(
        subject="system.alerts",
        data=json.dumps(wire_msg).encode(),
    )

    outcome = await svc.container._dispatch_event(raw_msg, pattern="system.alerts")
    assert outcome == DispatchOutcome.OK
    assert len(received) == 1
    assert received[0].order_id == "alert-1"

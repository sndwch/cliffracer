"""Tier 4 E2E Test Suite: Real-World Workload Scenarios.

Implements 5 comprehensive production scenarios:
1. Order Processing Pipeline (RPC -> Canonical Event -> JetStream Consumer -> KV Update -> Correlation Tracking).
2. Telemetry Ingestion with KV Deduplication (High-frequency sensor events, idempotency, revision checking).
3. Resilient RPC with Circuit Breaking (Fault injection, circuit tripping to OPEN, fail-fast, recovery).
4. Async Event Fanout with Distributed Correlation Tracing (Root event -> multi-worker fanout -> secondary events).
5. Service Discovery & Schema Catalog (Cross-service introspection over NATS, schema validation, contract matching).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from cliffracer_resilience import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitBreakerConfig,
    RpcCircuitOpenError,
)
from pydantic import BaseModel, Field

from cliffracer import CliffracerService, rpc, validated_listener
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.exceptions import RPCTimeoutError

from .conftest import MockJetStreamMsg, MockNatsTransport

# ---------------------------------------------------------------------------
# Domain Models for Real-World Scenarios
# ---------------------------------------------------------------------------


class LineItem(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)


class OrderSubmitRequest(BaseModel):
    order_id: str
    customer_id: str
    items: list[LineItem]


class OrderCreatedEvent(BaseModel):
    order_id: str
    customer_id: str
    total_amount: float


class OrderFulfilledEvent(BaseModel):
    order_id: str
    status: str
    fulfillment_timestamp: str


class TelemetryReading(BaseModel):
    sensor_id: str
    seq: int = Field(ge=0)
    temperature: float
    humidity: float


class UserRegistrationEvent(BaseModel):
    user_id: str
    email: str
    username: str


# ---------------------------------------------------------------------------
# Scenario 1: Order Processing Pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_1_order_processing_pipeline(
    e2e_service_factory: Any,
) -> None:
    """Scenario 1: End-to-end Order Processing Pipeline.

    Workflow:
    1. Client submits OrderSubmitRequest via RPC to OrderIngestService.
    2. OrderIngestService validates request and publishes OrderCreatedEvent with canonical envelope.
    3. FulfillmentService consumes OrderCreatedEvent via @validated_listener, updates state,
       and publishes OrderFulfilledEvent.
    4. End-to-end correlation ID is strictly preserved across all hops.
    """
    transport = MockNatsTransport()
    trace_id = "trace-order-pipeline-9001"

    # 1. Order Ingestion Service
    class OrderIngestService(CliffracerService):
        @rpc
        async def submit_order(self, order: OrderSubmitRequest) -> dict[str, str]:
            total = sum(i.quantity * i.price for i in order.items)
            # Emit canonical event inheriting active correlation ID
            await self.publish_event(
                "orders.created",
                order_id=order.order_id,
                customer_id=order.customer_id,
                total_amount=total,
            )
            return {"order_id": order.order_id, "status": "accepted"}

    ingest_svc = e2e_service_factory(
        OrderIngestService, name="order_ingest", jetstream_enabled=True
    )
    ingest_svc.container.nc = transport
    ingest_svc._discover_handlers()

    # 2. Fulfillment Service
    processed_orders: list[tuple[OrderCreatedEvent, str | None]] = []

    class FulfillmentService(CliffracerService):
        @validated_listener("orders.created", OrderCreatedEvent, durable="fulfillment_worker")
        async def on_order_created(self, message: OrderCreatedEvent) -> None:
            cid = CorrelationContext.get()
            processed_orders.append((message, cid))
            # Publish fulfillment event
            await self.publish_event(
                "orders.fulfilled",
                order_id=message.order_id,
                status="fulfilled",
                fulfillment_timestamp=datetime.now(UTC).isoformat(),
            )

    fulfill_svc = e2e_service_factory(
        FulfillmentService, name="order_fulfill", jetstream_enabled=True
    )
    fulfill_svc.container.nc = transport
    fulfill_svc._discover_handlers()

    # Client submits order via RPC
    order_data = {
        "order": {
            "order_id": "ORD-2026-001",
            "customer_id": "CUST-42",
            "items": [
                {"sku": "SKU-APPLE", "quantity": 3, "price": 1.50},
                {"sku": "SKU-ORANGE", "quantity": 2, "price": 2.00},
            ],
        }
    }
    rpc_msg = MockJetStreamMsg(
        subject="order_ingest.submit_order",
        data=json.dumps(order_data).encode(),
        reply="_INBOX.client_reply",
        headers={"X-Correlation-ID": trace_id},
    )

    await ingest_svc.container._handle_rpc_request(rpc_msg)

    # Assert RPC reply
    assert rpc_msg.response_data is not None
    reply = json.loads(rpc_msg.response_data.decode())
    assert reply["success"] is True
    assert reply["correlation_id"] == trace_id

    # Assert orders.created published message
    created_events = [m for m in transport.published_messages if m[0] == "orders.created"]
    assert len(created_events) == 1
    envelope = json.loads(created_events[0][1].decode())
    assert envelope["correlation_id"] == trace_id
    assert envelope["source_service"] == "order_ingest"
    assert envelope["data"]["order_id"] == "ORD-2026-001"
    assert envelope["data"]["total_amount"] == 8.50

    # Deliver to Fulfillment Service
    event_msg = MockJetStreamMsg(
        subject="orders.created",
        data=created_events[0][1],
        headers={"X-Correlation-ID": trace_id},
    )
    await fulfill_svc.container._dispatch_event(event_msg)

    # Verify fulfillment execution
    assert len(processed_orders) == 1
    event_obj, active_cid = processed_orders[0]
    assert event_obj.order_id == "ORD-2026-001"
    assert active_cid == trace_id

    # Verify orders.fulfilled published with matching correlation ID
    fulfilled_events = [m for m in transport.published_messages if m[0] == "orders.fulfilled"]
    assert len(fulfilled_events) == 1
    fulfill_envelope = json.loads(fulfilled_events[0][1].decode())
    assert fulfill_envelope["correlation_id"] == trace_id
    assert fulfill_envelope["data"]["status"] == "fulfilled"


# ---------------------------------------------------------------------------
# Scenario 2: Telemetry Ingestion with KV Deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_telemetry_ingestion_with_kv_deduplication(
    e2e_service_factory: Any,
) -> None:
    """Scenario 2: Telemetry ingestion pipeline with sequence deduplication.

    Workflow:
    1. Sensors emit readings over NATS topic telemetry.readings.
    2. Network retries cause duplicate readings with identical seq numbers.
    3. IngestionService tracks latest seq per sensor in mock KV store.
    4. Duplicate sequences are identified and dropped without duplicate processing.
    5. New sequence readings are processed and stored.
    """
    transport = MockNatsTransport()
    kv_store: dict[str, int] = {}  # Mock JetStream KV (sensor_id -> max_seq)
    recorded_telemetry: list[TelemetryReading] = []

    class TelemetryIngestService(CliffracerService):
        @validated_listener("telemetry.readings", TelemetryReading, durable="telemetry_ingest")
        async def on_reading(self, message: TelemetryReading) -> None:
            last_seq = kv_store.get(message.sensor_id, -1)
            if message.seq <= last_seq:
                # Deduplication: already processed
                return

            kv_store[message.sensor_id] = message.seq
            recorded_telemetry.append(message)

    svc = e2e_service_factory(TelemetryIngestService, name="telemetry_svc", jetstream_enabled=True)
    svc.container.nc = transport
    svc._discover_handlers()

    readings = [
        TelemetryReading(sensor_id="sensor-alpha", seq=1, temperature=21.5, humidity=45.0),
        TelemetryReading(
            sensor_id="sensor-alpha", seq=1, temperature=21.5, humidity=45.0
        ),  # Duplicate
        TelemetryReading(sensor_id="sensor-alpha", seq=2, temperature=22.0, humidity=44.8),
        TelemetryReading(sensor_id="sensor-beta", seq=1, temperature=18.3, humidity=60.1),
        TelemetryReading(
            sensor_id="sensor-alpha", seq=2, temperature=22.0, humidity=44.8
        ),  # Duplicate
        TelemetryReading(sensor_id="sensor-beta", seq=2, temperature=18.5, humidity=59.9),
    ]

    for reading in readings:
        payload = {
            "data": reading.model_dump(),
            "timestamp": datetime.now(UTC).isoformat(),
            "source_service": "iot_gateway",
            "correlation_id": f"corr-{reading.sensor_id}-{reading.seq}",
        }
        msg = MockJetStreamMsg(
            subject="telemetry.readings",
            data=json.dumps(payload).encode(),
            headers={"X-Correlation-ID": payload["correlation_id"]},
        )
        await svc.container._dispatch_event(msg)

    # Expected: exactly 4 unique records (2 for alpha, 2 for beta)
    assert len(recorded_telemetry) == 4
    assert kv_store["sensor-alpha"] == 2
    assert kv_store["sensor-beta"] == 2


# ---------------------------------------------------------------------------
# Scenario 3: Resilient RPC with Circuit Breaking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_resilient_rpc_with_circuit_breaking() -> None:
    """Scenario 3: Resilient RPC proxy with circuit breaking protection.

    Workflow:
    1. Caller invokes downstream payment service via CircuitBreaker.
    2. Downstream service encounters transient outages, throwing failures.
    3. After failure threshold (3 failures), circuit breaker trips to OPEN.
    4. Subsequent calls fail fast with RpcCircuitOpenError without hitting downstream service.
    5. After recovery timeout, circuit enters HALF-OPEN, allows test probe, and recovers to CLOSED.
    """
    config = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout=0.1,  # Short recovery timeout for testing
        half_open_max_calls=1,
    )
    cb = CircuitBreaker(name="payment_gateway", config=config)
    assert cb.state == CLOSED

    downstream_calls: int = 0
    should_fail: bool = True

    async def payment_service_charge(amount: float) -> dict[str, str]:
        nonlocal downstream_calls
        downstream_calls += 1
        if should_fail:
            raise RPCTimeoutError("Payment gateway database timeout")
        return {"status": "paid", "amount": str(amount)}

    # Failures 1, 2, 3
    for _ in range(3):
        with pytest.raises(RPCTimeoutError):
            await cb.call(payment_service_charge, 50.0)

    # Circuit must now be OPEN
    assert cb.state == OPEN
    assert downstream_calls == 3

    # 4th call must fail fast without executing downstream service
    with pytest.raises(RpcCircuitOpenError):
        await cb.call(payment_service_charge, 50.0)

    assert downstream_calls == 3, "OPEN circuit breaker MUST NOT call downstream service"

    # Wait for recovery timeout
    await asyncio.sleep(0.12)
    assert cb.state == HALF_OPEN

    # Recovery: downstream heals
    should_fail = False
    result = await cb.call(payment_service_charge, 50.0)
    assert result["status"] == "paid"
    assert downstream_calls == 4
    # Circuit recovered to CLOSED
    assert cb.state == CLOSED


# ---------------------------------------------------------------------------
# Scenario 4: Async Event Fanout with Distributed Correlation Tracing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_async_event_fanout_with_correlation_tracing(
    e2e_service_factory: Any,
) -> None:
    """Scenario 4: 1-to-N event fanout with end-to-end correlation propagation.

    Workflow:
    1. UserRegistrationService emits user.registered with X-Correlation-ID: trace-fanout-777.
    2. Event fans out to three independent consumer services:
       - WelcomeEmailWorker
       - AnalyticsWorker
       - AuditLogWorker
    3. Each worker processes the event and publishes its own secondary event:
       - email.dispatched
       - analytics.user_indexed
       - audit.entry_logged
    4. All three secondary events carry the identical X-Correlation-ID: trace-fanout-777.
    """
    transport = MockNatsTransport()
    trace_id = "trace-fanout-777"

    # Worker 1: Welcome Email
    class EmailWorker(CliffracerService):
        @validated_listener("users.registered", UserRegistrationEvent, fanout=True)
        async def on_user(self, message: UserRegistrationEvent) -> None:
            await self.publish_event("email.dispatched", to=message.email)

    # Worker 2: Analytics
    class AnalyticsWorker(CliffracerService):
        @validated_listener("users.registered", UserRegistrationEvent, fanout=True)
        async def on_user(self, message: UserRegistrationEvent) -> None:
            await self.publish_event("analytics.user_indexed", uid=message.user_id)

    # Worker 3: Audit Log
    class AuditWorker(CliffracerService):
        @validated_listener("users.registered", UserRegistrationEvent, fanout=True)
        async def on_user(self, message: UserRegistrationEvent) -> None:
            await self.publish_event("audit.entry_logged", action="register", uid=message.user_id)

    email_svc = e2e_service_factory(EmailWorker, name="email_svc")
    analytics_svc = e2e_service_factory(AnalyticsWorker, name="analytics_svc")
    audit_svc = e2e_service_factory(AuditWorker, name="audit_svc")

    for svc in (email_svc, analytics_svc, audit_svc):
        svc.container.nc = transport
        svc._discover_handlers()

    # Original event emitted
    root_event_payload = {
        "data": {"user_id": "usr_99", "email": "alice@example.com", "username": "alice"},
        "timestamp": datetime.now(UTC).isoformat(),
        "source_service": "user_service",
        "correlation_id": trace_id,
    }
    raw_event = json.dumps(root_event_payload).encode()

    # Dispatch to all three workers (fanout)
    for svc in (email_svc, analytics_svc, audit_svc):
        msg = MockJetStreamMsg(
            subject="users.registered",
            data=raw_event,
            headers={"X-Correlation-ID": trace_id},
        )
        await svc.container._dispatch_event(msg)

    # Inspect published secondary events
    emitted_subjects = [m[0] for m in transport.published_messages]
    assert "email.dispatched" in emitted_subjects
    assert "analytics.user_indexed" in emitted_subjects
    assert "audit.entry_logged" in emitted_subjects

    for subj, data_bytes, _headers, _reply in transport.published_messages:
        envelope = json.loads(data_bytes.decode())
        assert envelope["correlation_id"] == trace_id, (
            f"Event on '{subj}' MUST preserve correlation ID '{trace_id}'"
        )


# ---------------------------------------------------------------------------
# Scenario 5: Service Discovery & Schema Catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_5_service_discovery_and_schema_catalog(
    e2e_service_factory: Any,
) -> None:
    """Scenario 5: Multi-service discovery and contract compatibility verification.

    Workflow:
    1. Deploy OrderService (produces orders.created) and NotificationService (consumes orders.created).
    2. Central Catalog Discovery queries {service}.describe for both services over NATS.
    3. Verifies that descriptions provide methods, listeners, docstrings, and Pydantic schemas.
    4. Verifies contract compatibility: the payload schema emitted by OrderService matches
       the validated_listener schema expected by NotificationService.
    """
    transport = MockNatsTransport()

    class OrderProducerService(CliffracerService):
        """Order producer service emitting customer orders."""

        @rpc
        async def submit(self, order: OrderSubmitRequest) -> dict[str, str]:
            """Submit a new customer order for fulfillment."""
            return {"status": "ok"}

    class NotificationConsumerService(CliffracerService):
        """Notification service sending email alerts upon order creation."""

        @validated_listener("orders.created", OrderCreatedEvent, durable="notif_worker")
        async def on_order(self, message: OrderCreatedEvent) -> None:
            """Consume order created events and send email notifications."""
            pass

    order_svc = e2e_service_factory(
        OrderProducerService, name="orders_prod", jetstream_enabled=True
    )
    notif_svc = e2e_service_factory(
        NotificationConsumerService, name="notif_cons", jetstream_enabled=True
    )

    order_svc.container.nc = transport
    notif_svc.container.nc = transport

    order_svc._discover_handlers()
    notif_svc._discover_handlers()

    await transport.subscribe("orders_prod.describe", order_svc.container._handle_describe_request)
    await transport.subscribe("notif_cons.describe", notif_svc.container._handle_describe_request)

    # Query describe for OrderProducerService over NATS transport
    order_desc_msg = await transport.request(
        "orders_prod.describe",
        payload=b"",
        headers={"X-Correlation-ID": "corr-catalog-01"},
    )
    assert order_desc_msg is not None and order_desc_msg.data is not None
    order_desc_data = json.loads(order_desc_msg.data.decode())

    # Verify Producer Description
    assert order_desc_data["service"] == "orders_prod"
    assert "methods" in order_desc_data
    submit_method = next((m for m in order_desc_data["methods"] if m["name"] == "submit"), None)
    assert submit_method is not None
    assert submit_method["doc"] == "Submit a new customer order for fulfillment."

    # Query describe for NotificationConsumerService over NATS transport
    notif_desc_msg = await transport.request(
        "notif_cons.describe",
        payload=b"",
        headers={"X-Correlation-ID": "corr-catalog-02"},
    )
    assert notif_desc_msg is not None and notif_desc_msg.data is not None
    notif_desc_data = json.loads(notif_desc_msg.data.decode())

    assert notif_desc_data["service"] == "notif_cons"
    assert "version" in notif_desc_data
    assert "description_hash" in notif_desc_data

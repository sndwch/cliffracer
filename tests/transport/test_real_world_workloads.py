"""Real-world workload scenarios, over the in-memory transport.

Implements 5 comprehensive production scenarios:
1. Order Processing Pipeline (RPC -> Canonical Event -> JetStream Consumer -> KV Update -> Correlation Tracking).
2. Telemetry Ingestion with Sequence Deduplication (High-frequency sensor events, consumer-side idempotency).
3. Resilient RPC with Circuit Breaking (Fault injection, circuit tripping to OPEN, fail-fast, recovery).
4. Async Event Fanout with Distributed Correlation Tracing (Root event -> multi-worker fanout -> secondary events).
5. Service Discovery & Schema Catalog (Cross-service introspection over NATS, schema validation, contract matching).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
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
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.outputs import Output
from cliffracer.testing import MockJetStreamContext, ServiceTestHarness

from .conftest import Connection, MockJetStreamMsg, started

pytestmark = pytest.mark.unit

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


class DifferentOrderCreatedEvent(BaseModel):
    """What a consumer that drifted from `OrderCreatedEvent` expects: a string where it has a number."""

    order_id: str
    customer_id: str
    total_amount: str


class TelemetryReading(BaseModel):
    sensor_id: str
    seq: int = Field(ge=0)
    temperature: float
    humidity: float


class UserRegistrationEvent(BaseModel):
    user_id: str
    email: str
    username: str


async def until(condition: Callable[[], bool], what: str, timeout: float = 2.0) -> None:
    """Wait for `condition`, bounded, so a miss fails by name instead of hanging."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# Scenario 1: Order Processing Pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_1_order_processing_pipeline(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Scenario 1: End-to-end Order Processing Pipeline.

    Workflow:
    1. Client submits OrderSubmitRequest via RPC to OrderIngestService.
    2. OrderIngestService validates request and publishes OrderCreatedEvent with canonical envelope.
    3. FulfillmentService consumes OrderCreatedEvent via @validated_listener, updates state,
       and publishes OrderFulfilledEvent.
    4. End-to-end correlation ID is strictly preserved across all hops.
    """
    transport = mock_transport
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

    ingest_svc = transport_service_factory(
        OrderIngestService,
        name="order_ingest",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.>"])],
    )
    # The broker does not model JetStream, so both services publish into one recording context.
    js = MockJetStreamContext()
    ingest_svc.container.nc = transport
    ingest_svc.container.js = js  # type: ignore[assignment]
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

    fulfill_svc = transport_service_factory(
        FulfillmentService,
        name="order_fulfill",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.>"])],
    )
    fulfill_svc.container.nc = transport
    fulfill_svc.container.js = js  # type: ignore[assignment]
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
    assert reply["result"] == {"order_id": "ORD-2026-001", "status": "accepted"}

    # Assert orders.created published message
    created_events = [m for m in js.published if m[0] == "orders.created"]
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
    fulfilled_events = [m for m in js.published if m[0] == "orders.fulfilled"]
    assert len(fulfilled_events) == 1
    fulfill_envelope = json.loads(fulfilled_events[0][1].decode())
    assert fulfill_envelope["correlation_id"] == trace_id
    # The whole event, by its schema: a payload missing a field, or carrying one more, fails here.
    assert set(fulfill_envelope["data"]) == set(OrderFulfilledEvent.model_fields)
    fulfilled = OrderFulfilledEvent.model_validate(fulfill_envelope["data"])
    assert (fulfilled.order_id, fulfilled.status) == ("ORD-2026-001", "fulfilled")
    # `_dispatch_event` is the core-subscription path, which makes no acknowledgement decision;
    # one appearing here would be a JetStream disposition leaking into it. The JetStream path's
    # ack, nak and term are asserted in test_wire_semantics.py.
    assert (event_msg.ack_calls, event_msg.nak_calls, event_msg.term_calls) == (0, [], 0)


# ---------------------------------------------------------------------------
# Scenario 2: Telemetry Ingestion with Sequence Deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_telemetry_ingestion_with_sequence_deduplication(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Scenario 2: Telemetry ingestion pipeline with consumer-side sequence deduplication.

    Workflow:
    1. Sensors emit readings over NATS topic telemetry.readings.
    2. Network retries cause duplicate readings with identical seq numbers.
    3. IngestionService tracks the latest seq per sensor in a dict of its own.
    4. Duplicate sequences are identified and dropped without duplicate processing.
    5. New sequence readings are processed and stored.

    The dedup rule is the listener's own and runs on a plain dict: no cliffracer KV or
    idempotency mechanism is involved. It also cannot see the framework delivering a message
    twice, because a redelivery always has `seq <= last_seq` and is dropped like a retry. So
    every INVOCATION is counted before the rule runs: six readings are sent, so six deliveries.
    """
    transport = mock_transport
    last_seq_by_sensor: dict[str, int] = {}  # the listener's own bookkeeping
    deliveries: list[TelemetryReading] = []  # counted before the dedup rule
    recorded_telemetry: list[TelemetryReading] = []

    class TelemetryIngestService(CliffracerService):
        @validated_listener("telemetry.readings", TelemetryReading, durable="telemetry_ingest")
        async def on_reading(self, message: TelemetryReading) -> None:
            deliveries.append(message)
            last_seq = last_seq_by_sensor.get(message.sensor_id, -1)
            if message.seq <= last_seq:
                # Deduplication: already processed
                return

            last_seq_by_sensor[message.sensor_id] = message.seq
            recorded_telemetry.append(message)

    svc = transport_service_factory(
        TelemetryIngestService, name="telemetry_svc", jetstream_enabled=True
    )
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

    # The framework delivered each of the six messages once, retries included...
    assert len(deliveries) == len(readings) == 6, [(d.sensor_id, d.seq) for d in deliveries]
    # ...and the listener's rule kept the 4 unique records (2 for alpha, 2 for beta)
    assert len(recorded_telemetry) == 4
    assert last_seq_by_sensor["sensor-alpha"] == 2
    assert last_seq_by_sensor["sensor-beta"] == 2


# ---------------------------------------------------------------------------
# Scenario 3: Resilient RPC with Circuit Breaking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_a_circuit_breaker_trips_fails_fast_admits_one_probe_and_recovers() -> (
    None
):
    """Scenario 3: a circuit breaker around a flaky downstream callable.

    There is no RPC, transport or service here: the breaker wraps a local coroutine, and the
    timeout it raises is raised by hand. How the breaker guards a real proxy is covered in the
    resilience package's own tests.

    Workflow:
    1. Caller invokes a downstream payment function via CircuitBreaker.
    2. The downstream encounters transient outages, throwing failures.
    3. After failure threshold (3 failures), circuit breaker trips to OPEN.
    4. Subsequent calls fail fast with RpcCircuitOpenError without hitting the downstream.
    5. After recovery timeout, the circuit is HALF-OPEN and admits `half_open_max_calls` (one)
       probe at a time: a second call while the probe is in flight is refused, not forwarded.
    6. The probe succeeds and the circuit recovers to CLOSED.
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
    release_probe = asyncio.Event()
    hold_probe: bool = False

    async def payment_service_charge(amount: float) -> dict[str, str]:
        nonlocal downstream_calls
        downstream_calls += 1
        if should_fail:
            raise RPCTimeoutError("Payment gateway database timeout")
        if hold_probe:
            await release_probe.wait()
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

    # Recovery: downstream heals, and the first probe is held in flight
    should_fail = False
    hold_probe = True
    probe = asyncio.create_task(cb.call(payment_service_charge, 50.0))
    await until(lambda: downstream_calls == 4, "the probe to reach the downstream")

    # The limit binds: a second call while the probe is in flight is refused, not forwarded.
    # Bounded, so a breaker that forwarded it would fail here and not wait on the held probe.
    try:
        with pytest.raises(RpcCircuitOpenError):
            await asyncio.wait_for(cb.call(payment_service_charge, 50.0), timeout=1.0)
        assert downstream_calls == 4, "HALF_OPEN must not admit more than half_open_max_calls"
    finally:
        release_probe.set()
    result = await probe
    assert result["status"] == "paid"
    assert downstream_calls == 4
    # Circuit recovered to CLOSED
    assert cb.state == CLOSED


# ---------------------------------------------------------------------------
# Scenario 4: Async Event Fanout with Distributed Correlation Tracing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_async_event_fanout_with_correlation_tracing(
    transport_service_factory: Any,
    mock_transport: Connection,
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
    transport = mock_transport
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

    email_svc = transport_service_factory(EmailWorker, name="email_svc")
    analytics_svc = transport_service_factory(AnalyticsWorker, name="analytics_svc")
    audit_svc = transport_service_factory(AuditWorker, name="audit_svc")

    # Original event emitted
    root_event_payload = {
        "data": {"user_id": "usr_99", "email": "alice@example.com", "username": "alice"},
        "timestamp": datetime.now(UTC).isoformat(),
        "source_service": "user_service",
        "correlation_id": trace_id,
    }
    raw_event = json.dumps(root_event_payload).encode()

    secondary = {"email.dispatched", "analytics.user_indexed", "audit.entry_logged"}

    def emitted() -> set[str]:
        return {m.subject for m in transport.broker.published} & secondary

    # The services start as they would on a broker, and the event is published ONCE: the broker
    # delivers it to every subscription outside a queue group, so three secondary events means
    # the one message reached three listeners. Dispatching to each service by hand would show
    # only that each can handle the event.
    async with started(transport, email_svc, analytics_svc, audit_svc):
        # A fanout listener takes no queue group; with one, the broker, like a server, would hand
        # the message to a single member of it.
        asked = [s.queue for s in transport.broker.subscribed if s.subject == "users.registered"]
        assert asked == [None, None, None], transport.broker.subscribed

        await transport.publish(
            "users.registered", raw_event, headers={"X-Correlation-ID": trace_id}
        )
        await until(lambda: emitted() == secondary, "all three secondary events")

    for subj, data_bytes in ((m.subject, m.data) for m in transport.broker.published):
        envelope = json.loads(data_bytes.decode())
        assert envelope["correlation_id"] == trace_id, (
            f"Event on '{subj}' MUST preserve correlation ID '{trace_id}'"
        )


# ---------------------------------------------------------------------------
# Scenario 5: Service Discovery & Schema Catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_5_service_discovery_and_schema_catalog(
    transport_service_factory: Any,
) -> None:
    """Scenario 5: Multi-service discovery and contract compatibility verification.

    Workflow:
    1. Deploy OrderService (produces orders.created) and NotificationService (consumes orders.created).
    2. Central Catalog Discovery queries {service}.describe for both services over NATS.
    3. Verifies that descriptions provide methods, listeners, docstrings, and Pydantic schemas.
    4. Verifies contract compatibility: the payload schema emitted by OrderService matches
       the validated_listener schema expected by NotificationService.
    """

    class OrderProducerService(CliffracerService):
        """Order producer service emitting customer orders."""

        order_created = Output(OrderCreatedEvent, "orders.created")

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

    order_svc = transport_service_factory(
        OrderProducerService, name="orders_prod", jetstream_enabled=True
    )
    notif_svc = transport_service_factory(
        NotificationConsumerService, name="notif_cons", jetstream_enabled=True
    )

    # Read through the harness's own describe, not over the broker: the consumer's durable listener
    # needs JetStream to start, and the broker does not model streams, so neither service is started.
    async with ServiceTestHarness(order_svc) as order_h, ServiceTestHarness(notif_svc) as notif_h:
        order_desc_data = await order_h.describe()
        notif_desc_data = await notif_h.describe()

    # Producer: its methods, and the event it declares it emits
    assert order_desc_data["service"] == "orders_prod"
    submit_method = next((m for m in order_desc_data["methods"] if m["name"] == "submit"), None)
    assert submit_method is not None
    assert submit_method["doc"] == "Submit a new customer order for fulfillment."
    (emitted,) = order_desc_data["outputs"]
    assert emitted["subject"] == "orders.created", emitted

    # Consumer: the listener it declares, as the catalog sees it
    assert notif_desc_data["service"] == "notif_cons"
    (listener,) = notif_desc_data["listeners"]
    assert listener["pattern"] == "orders.created", listener
    assert listener["durable"] == "notif_worker", listener
    assert listener["schema"]["qualname"] == "OrderCreatedEvent", listener

    # Contract compatibility: the schema the consumer validates is the one the producer emits
    consumer_schema = notif_desc_data["components"][listener["schema"]["schema_hash"]]
    assert consumer_schema == emitted["schema"]["validation"], (consumer_schema, emitted)
    assert listener["pattern"] == emitted["subject"]


@pytest.mark.asyncio
async def test_CONTROL_a_consumer_expecting_another_schema_does_not_match_the_producer(
    transport_service_factory: Any,
) -> None:
    """The comparison in scenario 5 is not vacuously true: a consumer whose model differs fails it."""

    class OrderProducerService(CliffracerService):
        order_created = Output(OrderCreatedEvent, "orders.created")

    class DriftedConsumer(CliffracerService):
        @validated_listener("orders.created", DifferentOrderCreatedEvent, durable="drifted")
        async def on_order(self, message: DifferentOrderCreatedEvent) -> None:
            pass

    producer = transport_service_factory(OrderProducerService, name="orders_prod")
    consumer = transport_service_factory(
        DriftedConsumer, name="drifted_cons", jetstream_enabled=True
    )

    async with ServiceTestHarness(producer) as produced, ServiceTestHarness(consumer) as consumed:
        (emitted,) = (await produced.describe())["outputs"]
        consumer_desc = await consumed.describe()
    (listener,) = consumer_desc["listeners"]

    consumer_schema = consumer_desc["components"][listener["schema"]["schema_hash"]]
    assert consumer_schema != emitted["schema"]["validation"]

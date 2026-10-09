"""Wire semantics, over the in-memory transport.

Covers Tiers 1-3:
- Tier 1: Feature verification for X-Correlation-ID header, canonical event envelopes, RPC error envelopes (>=5 per feature).
- Tier 2: Boundary & corner cases (missing/malformed headers, empty/huge payloads, diverse exception types, >=5 per feature).
- Tier 3: Cross-feature interactions (correlation propagation across RPC call -> event publish -> validated listener).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, Field

from cliffracer import (
    CliffracerService,
    RpcServerError,
    RpcUnknownMethod,
    RpcValidationError,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.client import ServiceClient
from cliffracer.core.correlation import CorrelationContext, correlation_id_var
from cliffracer.core.correlation_extension import CorrelationExtension
from cliffracer.core.dispatch.events import DispatchOutcome
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockJetStreamContext, wait_until
from cliffracer.testing.broker import Published

from .conftest import Connection, MockJetStreamMsg, started

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test Domain Models
# ---------------------------------------------------------------------------
class OrderModel(BaseModel):
    order_id: str
    amount: float = Field(gt=0)
    customer_id: str = "guest"


class NestedInnerModel(BaseModel):
    count: int = Field(gt=0)


class NestedOuterModel(BaseModel):
    inner: NestedInnerModel


def published_on(transport: Connection, subject: str) -> list[Published]:
    """What was published on the broker on `subject`, in order."""
    return [m for m in transport.broker.published if m.subject == subject]


def replies_on(transport: Connection) -> list[Published]:
    """What was published on the broker to a reply inbox."""
    return [m for m in transport.broker.published if m.subject.startswith("_INBOX.")]


async def ask(
    transport: Connection,
    subject: str,
    payload: bytes,
    correlation_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Send a request over the broker, as a client does, and return the reply's body and headers."""
    reply = await transport.request(
        subject, payload, timeout=5.0, headers={"X-Correlation-ID": correlation_id}
    )
    return json.loads(reply.data.decode()), reply.headers


# ---------------------------------------------------------------------------
# Tier 1: Feature Coverage (>=5 tests per feature)
# ---------------------------------------------------------------------------

# === Feature: Standardize Wire Header to X-Correlation-ID ===


def test_tier1_311_01_canonical_x_correlation_id_outbound_injection() -> None:
    """Verify CorrelationContext.inject_into_headers injects canonical X-Correlation-ID."""
    headers: dict[str, Any] = {}
    cid = "test-corr-uuid-1234"
    result = CorrelationContext.inject_into_headers(headers, cid)
    assert result["X-Correlation-ID"] == cid
    assert "X-Correlation-ID" in headers


def test_tier1_311_02_service_client_outbound_header_emission() -> None:
    """Verify ServiceClient._headers_for_send emits canonical X-Correlation-ID."""
    client = ServiceClient("dummy_service", headers={"auth": "token123"})
    outbound = client._headers_for_send()
    assert "X-Correlation-ID" in outbound
    assert outbound["auth"] == "token123"
    assert len(outbound["X-Correlation-ID"]) > 0


@pytest.fixture
def no_ambient_correlation_id():
    """Run with no correlation id in the ambient context, restoring it afterwards."""
    token = correlation_id_var.set(None)
    try:
        yield
    finally:
        correlation_id_var.reset(token)


@pytest.mark.parametrize("spelling", ["X-Correlation-ID", "x-correlation-id", "correlation_id"])
def test_tier1_311_02_a_callers_own_correlation_id_is_the_one_sent(
    spelling: str, no_ambient_correlation_id: None
) -> None:
    """A caller that already holds a trace id continues it, however it spelled the header."""
    client = ServiceClient("svc", headers={spelling: "caller-trace-1"})

    outbound = client._headers_for_send()

    assert outbound["X-Correlation-ID"] == "caller-trace-1"
    assert outbound["correlation_id"] == "caller-trace-1"


def test_tier1_311_02_the_callers_id_outranks_the_ambient_one() -> None:
    token = correlation_id_var.set("ambient-trace")
    try:
        client = ServiceClient("svc", headers={"X-Correlation-ID": "caller-trace-2"})

        assert client._headers_for_send()["X-Correlation-ID"] == "caller-trace-2"
    finally:
        correlation_id_var.reset(token)


def test_tier1_311_02_with_no_caller_id_the_ambient_trace_is_continued() -> None:
    token = correlation_id_var.set("ambient-trace")
    try:
        assert ServiceClient("svc")._headers_for_send()["X-Correlation-ID"] == "ambient-trace"
    finally:
        correlation_id_var.reset(token)


def test_tier1_311_02_with_no_id_anywhere_each_request_gets_a_new_one(
    no_ambient_correlation_id: None,
) -> None:
    client = ServiceClient("svc")

    first = client._headers_for_send()["X-Correlation-ID"]
    second = client._headers_for_send()["X-Correlation-ID"]

    assert first and second
    assert first != second


def test_tier1_311_03_case_insensitive_header_extraction() -> None:
    """Verify CorrelationContext.extract_from_headers extracts case-insensitively."""
    variants = [
        {"x-correlation-id": "cid-lower"},
        {"X-Correlation-ID": "cid-canonical"},
        {"X-CORRELATION-ID": "cid-upper"},
        {"x-CoRrElAtIoN-Id": "cid-mixed"},
    ]
    for variant in variants:
        (value,) = variant.values()
        assert CorrelationContext.extract_from_headers(variant) == value


def test_tier1_311_04_candidate_header_precedence() -> None:
    """Verify extraction precedence: x-correlation-id > x-request-id > correlation_id."""
    headers = {
        "x-correlation-id": "cid-primary",
        "x-request-id": "cid-secondary",
        "correlation_id": "cid-legacy",
    }
    extracted = CorrelationContext.extract_from_headers(headers)
    assert extracted == "cid-primary"

    # Secondary when primary omitted
    headers_no_primary = {
        "x-request-id": "cid-secondary",
        "correlation_id": "cid-legacy",
    }
    assert CorrelationContext.extract_from_headers(headers_no_primary) == "cid-secondary"

    # Legacy underscore when standard omitted
    headers_legacy = {"correlation_id": "cid-legacy"}
    assert CorrelationContext.extract_from_headers(headers_legacy) == "cid-legacy"


@pytest.mark.asyncio
async def test_tier1_311_05_correlation_extension_extracts_and_binds() -> None:
    """Verify CorrelationExtension extracts X-Correlation-ID and sets contextvar."""
    ext = CorrelationExtension()
    ctx = WorkerContext(
        kind="rpc",
        subject="test.subject",
        headers={"X-Correlation-ID": "corr-worker-777"},
        correlation_id=None,
        payload={"data": "hello"},
    )
    await ext.worker_setup(ctx)
    assert ctx.correlation_id == "corr-worker-777"
    assert correlation_id_var.get() == "corr-worker-777"
    await ext.worker_teardown(ctx)
    assert correlation_id_var.get() is None


# === Feature: Standardize Event Wire Envelope ===


@pytest.mark.asyncio
async def test_tier1_312_01_publish_event_emits_canonical_envelope(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify publish_event wraps payload in canonical envelope {data, timestamp, source_service, correlation_id}."""
    svc = transport_service_factory(name="order_service")
    transport = mock_transport

    async with started(transport, svc):
        CorrelationContext.set("corr-pub-123")
        await svc.publish_event("orders.created", order_id="ord_99", amount=49.99)

    (sent,) = published_on(transport, "orders.created")
    assert sent.subject == "orders.created"
    # The wire artefact of "Standardize Wire Header to X-Correlation-ID": the canonical header
    # by name, the legacy one beside it, and the content type, with no reply subject.
    assert sent.headers == {
        "X-Correlation-ID": "corr-pub-123",
        "correlation_id": "corr-pub-123",
        "Content-Type": "application/json",
    }
    assert sent.reply is None
    envelope = json.loads(sent.data.decode())

    # Invariant checks
    assert "data" in envelope, "Envelope must contain top-level 'data' key"
    assert "timestamp" in envelope, "Envelope must contain top-level 'timestamp' key"
    assert "source_service" in envelope, "Envelope must contain top-level 'source_service' key"
    assert "correlation_id" in envelope, "Envelope must contain top-level 'correlation_id' key"
    assert envelope["source_service"] == "order_service"
    assert envelope["correlation_id"] == "corr-pub-123"
    assert envelope["data"] == {"order_id": "ord_99", "amount": 49.99}


@pytest.mark.asyncio
async def test_tier1_312_02_publish_event_envelope_false_flat_payload(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify publish_event with envelope=False emits flat payload for backwards compatibility."""
    svc = transport_service_factory(name="legacy_service")
    transport = mock_transport

    async with started(transport, svc):
        CorrelationContext.set("corr-flat-456")
        await svc.publish_event("legacy.event", envelope=False, item="gadget", qty=5)

    (sent,) = published_on(transport, "legacy.event")
    assert sent.headers is not None and sent.headers["X-Correlation-ID"] == "corr-flat-456"
    payload = json.loads(sent.data.decode())
    assert "data" not in payload
    assert payload["item"] == "gadget"
    assert payload["qty"] == 5
    assert payload["correlation_id"] == "corr-flat-456"


@pytest.mark.asyncio
async def test_tier1_312_03_broadcast_message_emits_canonical_envelope(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify broadcast_message emits canonical envelope."""
    svc = transport_service_factory(name="broadcast_service")
    transport = mock_transport

    async with started(transport, svc):
        await svc.broadcast_message("system.ping", status="alive", node="node-1")
    (sent,) = published_on(transport, "system.ping")
    envelope = json.loads(sent.data.decode())
    assert sent.headers is not None
    assert sent.headers["X-Correlation-ID"] == envelope["correlation_id"]
    assert envelope["data"] == {"status": "alive", "node": "node-1"}
    assert envelope["source_service"] == "broadcast_service"
    assert "timestamp" in envelope


@pytest.mark.asyncio
async def test_tier1_312_04_validated_listener_unwraps_canonical_envelope(
    transport_service_factory: Any,
) -> None:
    """Verify @validated_listener un-wraps canonical envelope data['data'] into Pydantic model."""
    received_orders: list[OrderModel] = []

    class ListenerService(CliffracerService):
        @validated_listener("orders.created", OrderModel, durable="order_worker")
        async def on_order(self, message: OrderModel) -> None:
            received_orders.append(message)

    svc = transport_service_factory(
        ListenerService, name="consumer_service", jetstream_enabled=True
    )
    svc._discover_handlers()

    envelope = {
        "data": {"order_id": "ord_unwrap_1", "amount": 100.50, "customer_id": "cust_42"},
        "timestamp": datetime.now(UTC).isoformat(),
        "source_service": "upstream_service",
        "correlation_id": "corr-unwrap-test",
    }
    msg = MockJetStreamMsg(
        subject="orders.created",
        data=json.dumps(envelope).encode(),
        headers={"X-Correlation-ID": "corr-unwrap-test"},
    )

    await svc.container._dispatch_event(msg)
    assert len(received_orders) == 1
    assert received_orders[0].order_id == "ord_unwrap_1"
    assert received_orders[0].amount == 100.50
    assert received_orders[0].customer_id == "cust_42"


@pytest.mark.asyncio
async def test_tier1_312_05_unvalidated_listener_unpacks_canonical_envelope(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify unvalidated @listener un-packs domain payload kwargs from canonical envelope."""
    received_data: list[dict[str, Any]] = []

    class EventService(CliffracerService):
        @listener("events.audit", fanout=True)
        async def on_audit(self, action: str, actor: str, ip: str = "") -> None:
            received_data.append({"action": action, "actor": actor, "extra": {"ip": ip}})

    svc = transport_service_factory(EventService, name="audit_service")

    envelope = {
        "data": {"action": "login", "actor": "alice", "ip": "10.0.0.1"},
        "timestamp": datetime.now(UTC).isoformat(),
        "source_service": "auth_service",
        "correlation_id": "corr-audit-1",
    }
    async with started(mock_transport, svc):
        await mock_transport.publish("events.audit", json.dumps(envelope).encode())
        await wait_until(lambda: received_data, within=5.0, reason="the listener to run")

    assert len(received_data) == 1
    assert received_data[0]["action"] == "login"
    assert received_data[0]["actor"] == "alice"
    assert received_data[0]["extra"] == {"ip": "10.0.0.1"}


# === Feature: Align RPC Error Envelope to Guarantee success: false ===


@pytest.mark.asyncio
async def test_tier1_313_01_unknown_method_returns_success_false(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify unknown RPC method replies with success: false and correlation_id."""
    svc = transport_service_factory(name="rpc_service")
    async with started(mock_transport, svc):
        reply, reply_headers = await ask(
            mock_transport, "rpc_service.rpc.non_existent_method", b"{}", "corr-rpc-unk"
        )

    # The reply carries the headers the service set on it, as it does over a broker.
    assert reply_headers == {
        "Content-Type": "application/json",
        "X-Correlation-ID": "corr-rpc-unk",
    }
    assert reply.get("success") is False, "RPC error reply MUST have success: false"
    assert "Unknown method" in reply.get("error", "")
    assert "timestamp" in reply
    assert reply.get("correlation_id") == "corr-rpc-unk"


@pytest.mark.asyncio
async def test_tier1_313_02_policy_refusal_reject_message_returns_success_false(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify RejectMessage (extension refusal) returns success: false."""

    class GuardedService(CliffracerService):
        @rpc
        async def secure_op(self) -> str:
            raise RejectMessage("unauthorized access")

    svc = transport_service_factory(GuardedService, name="secure_svc")
    async with started(mock_transport, svc):
        reply, _ = await ask(mock_transport, "secure_svc.rpc.secure_op", b"{}", "corr-refuse-1")

    assert reply.get("success") is False, "Policy refusal MUST return success: false"
    assert "refused: unauthorized access" in reply.get("error", "")
    assert reply.get("correlation_id") == "corr-refuse-1"


@pytest.mark.asyncio
async def test_tier1_313_03_validation_error_returns_success_false(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify RPC validation failure returns success: false and details."""

    class ValidatedRpcService(CliffracerService):
        @rpc
        async def create_user(self, age: int) -> dict[str, str]:
            return {"status": "ok"}

    svc = transport_service_factory(ValidatedRpcService, name="val_svc")
    async with started(mock_transport, svc):
        # Pass string that fails int parsing
        reply, _ = await ask(
            mock_transport,
            "val_svc.rpc.create_user",
            json.dumps({"age": "not-a-number"}).encode(),
            "corr-val-fail",
        )

    assert reply.get("success") is False, "Validation failure MUST return success: false"
    assert reply.get("error") == "validation failed"
    assert "details" in reply
    assert isinstance(reply["details"], list)


@pytest.mark.asyncio
async def test_tier1_313_04_unhandled_application_exception_returns_success_false(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify unhandled internal exception returns success: false and traceback."""

    class CrashingService(CliffracerService):
        @rpc
        async def buggy_calc(self, divisor: int) -> int:
            return 100 // divisor

    svc = transport_service_factory(CrashingService, name="crash_svc", expose_internal_errors=True)
    async with started(mock_transport, svc):
        reply, _ = await ask(
            mock_transport,
            "crash_svc.rpc.buggy_calc",
            json.dumps({"divisor": 0}).encode(),
            "corr-crash-1",
        )

    assert reply.get("success") is False, "Unhandled exception MUST return success: false"
    assert "zero" in reply.get("error", "")
    assert "traceback" in reply
    assert reply.get("correlation_id") == "corr-crash-1"


@pytest.mark.asyncio
async def test_tier1_313_05_describe_error_returns_success_false(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify describe request error returns success: false."""
    svc = transport_service_factory(name="desc_svc")

    class RefuseDescribe(Extension):
        async def worker_setup(self, ctx: WorkerContext) -> None:
            if ctx.kind == "describe":
                raise RejectMessage("describe blocked")

    # An extension on the real pipeline, so the correlation extension still binds the id.
    svc.add_extension(RefuseDescribe(), "refuse_describe")

    async with started(mock_transport, svc):
        reply, _ = await ask(mock_transport, "desc_svc.describe", b"", "corr-desc-err")

    assert reply.get("success") is False, "Describe error MUST return success: false"
    assert "refused: describe blocked" in reply.get("error", "")
    assert reply.get("correlation_id") == "corr-desc-err"


# ---------------------------------------------------------------------------
# Tier 2: Boundary & Corner Cases (>=5 tests per feature)
# ---------------------------------------------------------------------------

# === Feature: X-Correlation-ID Boundaries ===


def test_tier2_311_01_empty_string_correlation_header_falls_back() -> None:
    """Verify empty string header is treated as absent, falling back to secondary."""
    headers = {"X-Correlation-ID": "   ", "x-request-id": "secondary-id"}
    extracted = CorrelationContext.extract_from_headers(headers)
    assert extracted == "secondary-id"


def test_tier2_311_02_special_characters_in_correlation_id() -> None:
    """Verify correlation ID containing colons, slashes, and unicode is safely preserved."""
    special_id = "urn:uuid:1234-5678/trace:001#🚀"
    headers = {"X-Correlation-ID": special_id}
    extracted = CorrelationContext.extract_from_headers(headers)
    assert extracted == special_id


def test_tier2_311_03_extreme_length_correlation_id() -> None:
    """Verify an 8KB correlation ID is refused whole, and one at the limit is kept whole.

    An ID is logged on every line of its request and copied onto every message the handler
    sends, so one over 256 characters is treated as absent; it is neither truncated nor an error.
    """
    huge_id = "corr_" + "A" * 8192
    assert CorrelationContext.extract_from_headers({"X-Correlation-ID": huge_id}) is None

    at_the_limit = "corr_" + "A" * 251
    assert len(at_the_limit) == 256
    assert (
        CorrelationContext.extract_from_headers({"X-Correlation-ID": at_the_limit}) == at_the_limit
    )


def test_tier2_311_04_missing_or_none_headers_dict() -> None:
    """Verify None or empty headers dictionary returns None gracefully."""
    assert CorrelationContext.extract_from_headers(None) is None  # type: ignore[arg-type]
    assert CorrelationContext.extract_from_headers({}) is None


def test_tier2_311_05_conflicting_headers_with_different_values() -> None:
    """Verify conflicting header names strictly follow candidate order."""
    conflicting = {
        "correlation_id": "val_lowest",
        "request-id": "val_mid_low",
        "x-trace-id": "val_mid",
        "x-request-id": "val_high",
        "x-correlation-id": "val_highest",
    }
    assert CorrelationContext.extract_from_headers(conflicting) == "val_highest"
    del conflicting["x-correlation-id"]
    assert CorrelationContext.extract_from_headers(conflicting) == "val_high"
    del conflicting["x-request-id"]
    assert CorrelationContext.extract_from_headers(conflicting) == "val_mid"


# === Feature: Event Wire Envelope Boundaries ===


@pytest.mark.asyncio
async def test_tier2_312_01_empty_event_data_payload(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify publish_event with empty kwargs produces valid envelope with data: {}."""
    svc = transport_service_factory(name="empty_payload_svc")
    transport = mock_transport

    async with started(transport, svc):
        await svc.publish_event("events.heartbeat")
    (sent,) = published_on(transport, "events.heartbeat")
    envelope = json.loads(sent.data.decode())
    assert envelope["data"] == {}
    assert "timestamp" in envelope


@pytest.mark.asyncio
async def test_tier2_312_02_huge_nested_event_payload(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify 100KB nested tree payload envelopes and serializes cleanly."""
    svc = transport_service_factory(name="huge_payload_svc")
    transport = mock_transport

    deep_dict = {"leaf": "value", "list": list(range(5000))}
    async with started(transport, svc):
        await svc.publish_event("events.telemetry", payload=deep_dict)

    (sent,) = published_on(transport, "events.telemetry")
    envelope = json.loads(sent.data.decode())
    assert envelope["data"]["payload"]["leaf"] == "value"
    assert len(envelope["data"]["payload"]["list"]) == 5000


@pytest.mark.asyncio
async def test_tier2_312_03_envelope_metadata_key_collision(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify user payload having keys named 'data', 'timestamp', or 'source_service' nest inside data."""
    svc = transport_service_factory(name="collision_svc")
    transport = mock_transport

    user_kwargs = {
        "data": "inner_data_content",
        "timestamp": "user_timestamp",
        "source_service": "custom_upstream",
    }
    async with started(transport, svc):
        before = datetime.now(UTC)
        await svc.publish_event("events.user_collision", **user_kwargs)
        after = datetime.now(UTC)

    (sent,) = published_on(transport, "events.user_collision")
    envelope = json.loads(sent.data.decode())
    assert envelope["source_service"] == "collision_svc"
    assert envelope["data"]["data"] == "inner_data_content"
    assert envelope["data"]["source_service"] == "custom_upstream"
    # The caller's `timestamp` is payload, not envelope metadata: it stays in
    # `data`, and the envelope's own is the service's clock at publish time.
    assert envelope["data"]["timestamp"] == "user_timestamp"
    assert envelope["timestamp"] != "user_timestamp"
    assert before <= datetime.fromisoformat(envelope["timestamp"]) <= after


@pytest.mark.asyncio
async def test_tier2_312_04_legacy_flat_payload_backward_compatibility(
    transport_service_factory: Any,
) -> None:
    """Verify @validated_listener accepts legacy flat payloads seamlessly without DLQ."""
    received: list[OrderModel] = []

    class LegacyConsumer(CliffracerService):
        @validated_listener("orders.legacy", OrderModel, durable="legacy_worker")
        async def on_order(self, message: OrderModel) -> None:
            received.append(message)

    svc = transport_service_factory(LegacyConsumer, name="legacy_consumer", jetstream_enabled=True)
    svc._discover_handlers()

    flat_payload = {"order_id": "flat_123", "amount": 75.0, "customer_id": "flat_user"}
    msg = MockJetStreamMsg(subject="orders.legacy", data=json.dumps(flat_payload).encode())

    await svc.container._dispatch_event(msg)
    assert len(received) == 1
    assert received[0].order_id == "flat_123"


class _StrictConsumer(CliffracerService):
    handled: list[OrderModel]

    @validated_listener("orders.strict", OrderModel, durable="strict_worker")
    async def on_order(self, message: OrderModel) -> None:
        self.handled.append(message)


def _strict_service(transport_service_factory: Any) -> tuple[Any, list[Any]]:
    svc = transport_service_factory(_StrictConsumer, name="strict_svc", jetstream_enabled=True)
    svc.handled = []
    svc._discover_handlers()
    dlq_calls: list[Any] = []
    svc.container._publish_dlq = AsyncMock(side_effect=lambda *a, **kw: dlq_calls.append((a, kw)))  # type: ignore[method-assign]
    return svc, dlq_calls


@pytest.mark.asyncio
async def test_tier2_312_05_malformed_json_is_dead_lettered_once_and_judged_invalid(
    transport_service_factory: Any,
) -> None:
    """Unparseable JSON is dead-lettered exactly once, to the DLQ subject, with its raw text."""
    svc, dlq_calls = _strict_service(transport_service_factory)
    corrupted_msg = MockJetStreamMsg(subject="orders.strict", data=b"NOT_VALID_JSON{abc")

    outcome = await svc.container._dispatch_event(corrupted_msg)

    assert outcome is DispatchOutcome.INVALID
    assert len(dlq_calls) == 1
    (args, kwargs) = dlq_calls[0]
    assert args[0] == "dlq.strict_svc"
    assert kwargs["payload"] == {"raw": "NOT_VALID_JSON{abc"}
    assert svc.handled == []


@pytest.mark.asyncio
async def test_tier2_312_05_malformed_json_is_terminated_not_acked_or_naked(
    transport_service_factory: Any,
) -> None:
    """The termination lives one layer up from the dead-lettering: through the JetStream handler
    a malformed message is dead-lettered once and terminated once, never acked or redelivered."""
    svc, dlq_calls = _strict_service(transport_service_factory)
    corrupted_msg = MockJetStreamMsg(
        subject="orders.strict", data=b"NOT_VALID_JSON{abc", from_jetstream=True
    )

    await svc.container._handle_jetstream_event(corrupted_msg, pattern="orders.strict")

    assert len(dlq_calls) == 1
    assert corrupted_msg.term_calls == 1
    assert corrupted_msg.ack_calls == 0
    assert corrupted_msg.nak_calls == []


@pytest.mark.asyncio
async def test_tier2_312_05_CONTROL_a_valid_message_is_handled_and_acked_not_dead_lettered(
    transport_service_factory: Any,
) -> None:
    """The service can handle a valid message, so the dead-lettering above is a decision about
    the malformed one and not a service that rejects everything."""
    svc, dlq_calls = _strict_service(transport_service_factory)
    valid_msg = MockJetStreamMsg(
        subject="orders.strict",
        data=json.dumps({"order_id": "ok_1", "amount": 5.0}).encode(),
        from_jetstream=True,
    )

    await svc.container._handle_jetstream_event(valid_msg, pattern="orders.strict")

    assert [m.order_id for m in svc.handled] == ["ok_1"]
    assert dlq_calls == []
    assert valid_msg.ack_calls == 1
    assert valid_msg.term_calls == 0
    assert valid_msg.nak_calls == []


# === Feature: RPC Error Envelope Boundaries ===


@pytest.mark.asyncio
async def test_tier2_313_01_diverse_exception_types_enveloped(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify diverse exception types (KeyError, TypeError) return success: false."""

    class MultiErrorService(CliffracerService):
        @rpc
        async def raise_key(self) -> None:
            _ = {}["missing_key"]

        @rpc
        async def raise_type(self) -> None:
            _ = "string" + 123  # type: ignore[operator]

    svc = transport_service_factory(MultiErrorService, name="multi_err_svc")

    async with started(mock_transport, svc):
        for method in ("raise_key", "raise_type"):
            reply, _ = await ask(mock_transport, f"multi_err_svc.rpc.{method}", b"{}", method)
            assert reply["success"] is False
            assert "error" in reply
    # The reading the no-reply case below asserts is empty: a request carrying a reply subject
    # puts a reply on the broker, so an empty reading there is a reply not sent, not one not seen.
    assert len(replies_on(mock_transport)) == 2


@pytest.mark.asyncio
async def test_tier2_313_02_rpc_without_reply_subject_dropped_cleanly(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify RPC request without reply subject drops error response cleanly without crash."""
    svc = transport_service_factory(name="no_reply_svc")
    async with started(mock_transport, svc):
        # Fire-and-forget: published with no reply subject, so there is nowhere to answer.
        await mock_transport.publish("no_reply_svc.rpc.unknown_method", b"{}")
        await mock_transport.broker.settle()
    assert replies_on(mock_transport) == []
    assert mock_transport.broker.handler_errors == ()


@pytest.mark.asyncio
async def test_tier2_313_03_exception_message_escaping_special_chars(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify exception messages containing quotes, newlines, and unicode serialize valid JSON."""

    class WeirdErrorService(CliffracerService):
        @rpc
        async def trigger(self) -> None:
            raise ValueError('Error with "quotes", \n newlines, \t tabs and 💥 emojis')

    svc = transport_service_factory(
        WeirdErrorService, name="weird_svc", expose_internal_errors=True
    )
    async with started(mock_transport, svc):
        # Must parse without JSONDecodeError
        reply, _ = await ask(mock_transport, "weird_svc.rpc.trigger", b"{}", "corr-weird")
    assert reply["success"] is False
    assert "quotes" in reply["error"]
    assert "emojis" in reply["error"]


@pytest.mark.asyncio
async def test_tier2_313_04_deeply_nested_validation_errors(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Verify nested Pydantic model validation failure returns detailed error structure."""

    class NestedRpcService(CliffracerService):
        @rpc
        async def submit(self, payload: NestedOuterModel) -> str:
            return "ok"

    svc = transport_service_factory(NestedRpcService, name="nested_val_svc")

    bad_payload = {"payload": {"inner": {"count": -5}}}
    async with started(mock_transport, svc):
        reply, _ = await ask(
            mock_transport, "nested_val_svc.rpc.submit", json.dumps(bad_payload).encode(), "n"
        )
    assert reply["success"] is False
    assert reply["error"] == "validation failed"
    assert "details" in reply


def _error_reply(error: str, **extra: Any) -> dict[str, Any]:
    return {
        "error": error,
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": "corr-client-err",
        **extra,
    }


def test_tier2_313_05_client_maps_an_unknown_method_code_to_the_exception() -> None:
    """A reply carrying the typed code is classified by the code, not by its prose."""
    client = ServiceClient("order_service")
    reply = _error_reply("no such thing", success=False, code="unknown_method")

    with pytest.raises(RpcUnknownMethod):
        client._raise_for_error(reply, "order_service.missing_rpc")


def test_tier2_313_05_client_maps_the_unknown_method_prose_of_an_uncoded_reply() -> None:
    """A reply from a service that sends no code is classified by its error text.

    `success` plays no part in this branch, so it is not what this test is about; the
    next two tests are about the one place `success` decides.
    """
    client = ServiceClient("order_service")
    reply = _error_reply("Unknown method: missing_rpc", success=False)

    with pytest.raises(RpcUnknownMethod) as excinfo:
        client._raise_for_error(reply, "order_service.missing_rpc")
    assert "missing_rpc" in str(excinfo.value)


def test_tier2_313_05_an_uncoded_validation_failure_is_trusted_only_with_success_false() -> None:
    """For an uncoded reply, "validation failed" is a validation error only when the envelope also
    says `success: false`. A reply that says `success: true` and still carries that text is not
    one, and falls to the generic server error."""
    client = ServiceClient("order_service")
    details = [{"loc": ["amount"], "msg": "must be positive"}]

    with pytest.raises(RpcValidationError):
        client._raise_for_error(
            _error_reply("validation failed", success=False, details=details), "svc.m"
        )
    with pytest.raises(RpcServerError):
        client._raise_for_error(
            _error_reply("validation failed", success=True, details=details), "svc.m"
        )


# ---------------------------------------------------------------------------
# Tier 3: Cross-Feature Interactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_correlation_preservation_rpc_to_publish_to_listener(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Cross-feature: Trace correlation ID through RPC call -> event publish -> validated listener."""
    received_in_listener: list[tuple[OrderModel, str | None]] = []

    class OrderOrchestrator(CliffracerService):
        @rpc
        async def place_order(self, order_id: str, amount: float) -> dict[str, str]:
            # Step 2: Publish event inheriting inbound correlation ID
            await self.publish_event("orders.placed", order_id=order_id, amount=amount)
            return {"status": "accepted"}

        @validated_listener("orders.placed", OrderModel, durable="fulfillment_consumer")
        async def fulfill_order(self, message: OrderModel) -> None:
            # Step 3: Listener inherits correlation ID
            received_in_listener.append((message, CorrelationContext.get()))

    svc = transport_service_factory(
        OrderOrchestrator,
        name="orchestrator",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.>"])],
    )
    transport = mock_transport
    js = MockJetStreamContext()
    svc.container.nc = transport
    # The broker does not model JetStream, so the publish goes to the harness's recording context.
    svc.container.js = js  # type: ignore[assignment]
    svc._discover_handlers()

    # Step 1: Inbound RPC with canonical X-Correlation-ID
    trace_id = "trace-e2e-order-hop-9999"
    rpc_msg = MockJetStreamMsg(
        subject="orchestrator.place_order",
        data=json.dumps({"order_id": "ord_trace_1", "amount": 199.95}).encode(),
        reply="_INBOX.orchestrator_reply",
        headers={"X-Correlation-ID": trace_id},
    )

    await svc.container._handle_rpc_request(rpc_msg)

    # Verify RPC response
    assert rpc_msg.response_data is not None
    rpc_reply = json.loads(rpc_msg.response_data.decode())
    assert rpc_reply["success"] is True
    assert rpc_reply["correlation_id"] == trace_id

    # Verify published event envelope
    ((subj, payload_bytes, headers),) = js.published
    envelope = json.loads(payload_bytes.decode())
    assert envelope["correlation_id"] == trace_id
    assert headers is not None and headers["X-Correlation-ID"] == trace_id
    assert envelope["data"]["order_id"] == "ord_trace_1"

    # Step 4: Deliver published event to listener
    event_msg = MockJetStreamMsg(
        subject="orders.placed",
        data=payload_bytes,
        headers={"X-Correlation-ID": trace_id},
    )
    await svc.container._dispatch_event(event_msg)

    # Verify listener execution
    assert len(received_in_listener) == 1
    order, active_cid = received_in_listener[0]
    assert order.order_id == "ord_trace_1"
    assert active_cid == trace_id, "Listener MUST observe the original distributed correlation ID"


@pytest.mark.asyncio
async def test_tier3_rpc_validation_failure_preserves_inbound_correlation(
    transport_service_factory: Any,
    mock_transport: Connection,
) -> None:
    """Cross-feature: Inbound correlation preserved on RPC validation failure response."""

    class StrictService(CliffracerService):
        @rpc
        async def execute(self, code: int) -> str:
            return f"code_{code}"

    svc = transport_service_factory(StrictService, name="strict_svc")

    trace_id = "trace-fail-corr-4444"
    async with started(mock_transport, svc):
        reply, reply_headers = await ask(
            mock_transport,
            "strict_svc.rpc.execute",
            json.dumps({"code": "invalid_string_code"}).encode(),
            trace_id,
        )
    assert reply["success"] is False
    assert reply["correlation_id"] == trace_id
    assert reply_headers["X-Correlation-ID"] == trace_id


@pytest.mark.asyncio
async def test_tier3_dlq_envelope_carries_inbound_correlation(
    transport_service_factory: Any,
) -> None:
    """Cross-feature: Poison message routed to DLQ includes original X-Correlation-ID."""

    class FailingConsumer(CliffracerService):
        @validated_listener("items.process", OrderModel, durable="failing_worker")
        async def on_item(self, message: OrderModel) -> None:
            pass

    svc = transport_service_factory(FailingConsumer, name="failing_svc", jetstream_enabled=True)
    svc._discover_handlers()

    dlq_captures: list[dict[str, Any]] = []

    async def mock_dlq_publish(subject: str, *args: Any, **kwargs: Any) -> None:
        headers = kwargs["headers"]  # always passed by keyword; a KeyError here says it changed
        dlq_captures.append(
            {"subject": subject, "payload": kwargs.get("payload"), "headers": headers}
        )

    svc.container._publish_dlq = AsyncMock(side_effect=mock_dlq_publish)  # type: ignore[method-assign]

    trace_id = "trace-dlq-poison-8888"
    msg = MockJetStreamMsg(
        subject="items.process",
        data=json.dumps({"order_id": "ord_1", "amount": -999.0}).encode(),  # Fails amount > 0
        headers={"X-Correlation-ID": trace_id},
    )

    await svc.container._dispatch_event(msg)
    assert len(dlq_captures) == 1
    dlq_headers = dlq_captures[0]["headers"]
    # The canonical wire header, by name. Reading it back through
    # `CorrelationContext.extract_from_headers` would pass for any of the seven
    # header names that function accepts, so a DLQ message with no
    # `X-Correlation-ID` at all would still pass.
    assert dlq_headers["X-Correlation-ID"] == trace_id
    assert dlq_captures[0]["subject"] == "dlq.failing_svc"
    assert dlq_captures[0]["payload"] == {"order_id": "ord_1", "amount": -999.0}


@pytest.mark.asyncio
async def test_a_stopped_service_leaves_its_transport_closed_and_unsubscribed(
    transport_service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    mock_transport: Connection,
) -> None:
    """Teardown is read off the transport the service used, not off the flags
    that stop() sets on the way past.

    `lifecycle.is_stopped` and `_running` are written by the method under
    test, so a stop() that flipped them and skipped timers, subscriptions,
    task drain and disconnect would satisfy them exactly as well as a real
    shutdown does. The transport is the thing that decides whether the service
    let go of the broker.
    """
    transport = mock_transport

    async def fake_connect(*args: Any, **kwargs: Any) -> Connection:
        return transport

    monkeypatch.setattr("cliffracer.core.dial.connect", fake_connect)

    class Svc(CliffracerService):
        @rpc
        async def ping(self) -> str:
            return "pong"

    svc = transport_service_factory(Svc, name="lifecycle_factory_svc")
    await svc.start()

    assert svc._running
    assert transport.is_connected, "the service is holding the transport"
    assert transport.subscriptions, "the service subscribed something to let go of"

    await svc.stop()

    assert transport.subscriptions == (), "subscriptions released on the transport"
    assert not transport.is_connected
    assert transport.is_closed, "the connection was closed, not merely abandoned"


@pytest.mark.asyncio
async def test_correlation_context_clear_is_load_bearing():
    """Verify CorrelationContext.clear actively resets correlation_id_var."""
    CorrelationContext.set("active_transport_token")
    assert CorrelationContext.get() == "active_transport_token"
    CorrelationContext.clear()
    assert CorrelationContext.get() is None

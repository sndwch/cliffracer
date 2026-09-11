"""End-to-End E2E Test Suite for Wire Semantics.

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
    RpcUnknownMethod,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.client import ServiceClient
from cliffracer.core.correlation import CorrelationContext, correlation_id_var
from cliffracer.core.correlation_extension import CorrelationExtension
from cliffracer.core.extension import RejectMessage, WorkerContext

from .conftest import MockJetStreamMsg, MockNatsTransport


# ---------------------------------------------------------------------------
# Test Domain Models
# ---------------------------------------------------------------------------
class OrderModel(BaseModel):
    order_id: str
    amount: float = Field(gt=0)
    customer_id: str = "guest"


class NestedPayloadModel(BaseModel):
    root_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class NestedInnerModel(BaseModel):
    count: int = Field(gt=0)


class NestedOuterModel(BaseModel):
    inner: NestedInnerModel


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


def test_tier1_311_03_case_insensitive_header_extraction() -> None:
    """Verify CorrelationContext.extract_from_headers extracts case-insensitively."""
    variants = [
        {"x-correlation-id": "cid-lower"},
        {"X-Correlation-ID": "cid-canonical"},
        {"X-CORRELATION-ID": "cid-upper"},
        {"x-CoRrElAtIoN-Id": "cid-mixed"},
    ]
    for variant in variants:
        extracted = CorrelationContext.extract_from_headers(variant)
        assert extracted is not None
        assert extracted.startswith("cid-")


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
    e2e_service_factory: Any,
) -> None:
    """Verify publish_event wraps payload in canonical envelope {data, timestamp, source_service, correlation_id}."""
    svc = e2e_service_factory(name="order_service")
    transport = MockNatsTransport()
    svc.container.nc = transport

    CorrelationContext.set("corr-pub-123")
    await svc.publish_event("orders.created", order_id="ord_99", amount=49.99)

    assert len(transport.published_messages) == 1
    subj, payload_bytes, headers, reply = transport.published_messages[0]
    assert subj == "orders.created"
    envelope = json.loads(payload_bytes.decode())

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
    e2e_service_factory: Any,
) -> None:
    """Verify publish_event with envelope=False emits flat payload for backwards compatibility."""
    svc = e2e_service_factory(name="legacy_service")
    transport = MockNatsTransport()
    svc.container.nc = transport

    CorrelationContext.set("corr-flat-456")
    await svc.publish_event("legacy.event", envelope=False, item="gadget", qty=5)

    assert len(transport.published_messages) == 1
    subj, payload_bytes, headers, reply = transport.published_messages[0]
    payload = json.loads(payload_bytes.decode())
    assert "data" not in payload
    assert payload["item"] == "gadget"
    assert payload["qty"] == 5
    assert payload["correlation_id"] == "corr-flat-456"


@pytest.mark.asyncio
async def test_tier1_312_03_broadcast_message_emits_canonical_envelope(
    e2e_service_factory: Any,
) -> None:
    """Verify broadcast_message emits canonical envelope."""
    svc = e2e_service_factory(name="broadcast_service")
    transport = MockNatsTransport()
    svc.container.nc = transport

    await svc.broadcast_message("system.ping", status="alive", node="node-1")
    assert len(transport.published_messages) == 1
    subj, payload_bytes, headers, reply = transport.published_messages[0]
    envelope = json.loads(payload_bytes.decode())
    assert envelope["data"] == {"status": "alive", "node": "node-1"}
    assert envelope["source_service"] == "broadcast_service"
    assert "timestamp" in envelope


@pytest.mark.asyncio
async def test_tier1_312_04_validated_listener_unwraps_canonical_envelope(
    e2e_service_factory: Any,
) -> None:
    """Verify @validated_listener un-wraps canonical envelope data['data'] into Pydantic model."""
    received_orders: list[OrderModel] = []

    class ListenerService(CliffracerService):
        @validated_listener("orders.created", OrderModel, durable="order_worker")
        async def on_order(self, message: OrderModel) -> None:
            received_orders.append(message)

    svc = e2e_service_factory(ListenerService, name="consumer_service", jetstream_enabled=True)
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
    e2e_service_factory: Any,
) -> None:
    """Verify unvalidated @listener un-packs domain payload kwargs from canonical envelope."""
    received_data: list[dict[str, Any]] = []

    class EventService(CliffracerService):
        @listener("events.audit", fanout=True)
        async def on_audit(self, action: str, actor: str, ip: str = "") -> None:
            received_data.append({"action": action, "actor": actor, "extra": {"ip": ip}})

    svc = e2e_service_factory(EventService, name="audit_service")
    svc._discover_handlers()

    envelope = {
        "data": {"action": "login", "actor": "alice", "ip": "10.0.0.1"},
        "timestamp": datetime.now(UTC).isoformat(),
        "source_service": "auth_service",
        "correlation_id": "corr-audit-1",
    }
    msg = MockJetStreamMsg(subject="events.audit", data=json.dumps(envelope).encode())
    await svc.container._dispatch_event(msg)

    assert len(received_data) == 1
    assert received_data[0]["action"] == "login"
    assert received_data[0]["actor"] == "alice"


# === Feature: Align RPC Error Envelope to Guarantee success: false ===


@pytest.mark.asyncio
async def test_tier1_313_01_unknown_method_returns_success_false(
    e2e_service_factory: Any,
) -> None:
    """Verify unknown RPC method replies with success: false and correlation_id."""
    svc = e2e_service_factory(name="rpc_service")
    msg = MockJetStreamMsg(
        subject="rpc_service.non_existent_method",
        data=b"{}",
        reply="_INBOX.reply_123",
        headers={"X-Correlation-ID": "corr-rpc-unk"},
    )
    await svc.container._handle_rpc_request(msg)

    assert msg._response_sent
    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply.get("success") is False, "RPC error reply MUST have success: false"
    assert "Unknown method" in reply.get("error", "")
    assert "timestamp" in reply
    assert reply.get("correlation_id") == "corr-rpc-unk"


@pytest.mark.asyncio
async def test_tier1_313_02_policy_refusal_reject_message_returns_success_false(
    e2e_service_factory: Any,
) -> None:
    """Verify RejectMessage (extension refusal) returns success: false."""

    class GuardedService(CliffracerService):
        @rpc
        async def secure_op(self) -> str:
            raise RejectMessage("unauthorized access")

    svc = e2e_service_factory(GuardedService, name="secure_svc")
    svc._discover_handlers()

    msg = MockJetStreamMsg(
        subject="secure_svc.secure_op",
        data=b"{}",
        reply="_INBOX.reply_refusal",
        headers={"X-Correlation-ID": "corr-refuse-1"},
    )
    await svc.container._handle_rpc_request(msg)

    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply.get("success") is False, "Policy refusal MUST return success: false"
    assert "refused: unauthorized access" in reply.get("error", "")
    assert reply.get("correlation_id") == "corr-refuse-1"


@pytest.mark.asyncio
async def test_tier1_313_03_validation_error_returns_success_false(
    e2e_service_factory: Any,
) -> None:
    """Verify RPC validation failure returns success: false and details."""

    class ValidatedRpcService(CliffracerService):
        @rpc
        async def create_user(self, age: int) -> dict[str, str]:
            return {"status": "ok"}

    svc = e2e_service_factory(ValidatedRpcService, name="val_svc")
    svc._discover_handlers()

    # Pass string that fails int parsing
    msg = MockJetStreamMsg(
        subject="val_svc.create_user",
        data=json.dumps({"age": "not-a-number"}).encode(),
        reply="_INBOX.val_reply",
        headers={"X-Correlation-ID": "corr-val-fail"},
    )
    await svc.container._handle_rpc_request(msg)

    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply.get("success") is False, "Validation failure MUST return success: false"
    assert reply.get("error") == "validation failed"
    assert "details" in reply
    assert isinstance(reply["details"], list)


@pytest.mark.asyncio
async def test_tier1_313_04_unhandled_application_exception_returns_success_false(
    e2e_service_factory: Any,
) -> None:
    """Verify unhandled internal exception returns success: false and traceback."""

    class CrashingService(CliffracerService):
        @rpc
        async def buggy_calc(self, divisor: int) -> int:
            return 100 // divisor

    svc = e2e_service_factory(CrashingService, name="crash_svc", expose_internal_errors=True)
    svc._discover_handlers()

    msg = MockJetStreamMsg(
        subject="crash_svc.buggy_calc",
        data=json.dumps({"divisor": 0}).encode(),
        reply="_INBOX.crash_reply",
        headers={"X-Correlation-ID": "corr-crash-1"},
    )
    await svc.container._handle_rpc_request(msg)

    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply.get("success") is False, "Unhandled exception MUST return success: false"
    assert "zero" in reply.get("error", "")
    assert "traceback" in reply
    assert reply.get("correlation_id") == "corr-crash-1"


@pytest.mark.asyncio
async def test_tier1_313_05_describe_error_returns_success_false(
    e2e_service_factory: Any,
) -> None:
    """Verify describe request error returns success: false."""
    svc = e2e_service_factory(name="desc_svc")

    # Mock container describe worker to raise RejectMessage
    svc.container._run_worker = AsyncMock(side_effect=RejectMessage("describe blocked"))

    msg = MockJetStreamMsg(
        subject="desc_svc.describe",
        data=b"",
        reply="_INBOX.desc_reply",
        headers={"X-Correlation-ID": "corr-desc-err"},
    )
    await svc.container._handle_describe_request(msg)

    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply.get("success") is False, "Describe error MUST return success: false"
    assert "refused" in reply.get("error", "")


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
    """Verify 8KB correlation ID string is handled without memory error or truncation."""
    huge_id = "corr_" + "A" * 8192
    headers = {"X-Correlation-ID": huge_id}
    extracted = CorrelationContext.extract_from_headers(headers)
    assert extracted == huge_id


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
    e2e_service_factory: Any,
) -> None:
    """Verify publish_event with empty kwargs produces valid envelope with data: {}."""
    svc = e2e_service_factory(name="empty_payload_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    await svc.publish_event("events.heartbeat")
    assert len(transport.published_messages) == 1
    envelope = json.loads(transport.published_messages[0][1].decode())
    assert envelope["data"] == {}
    assert "timestamp" in envelope


@pytest.mark.asyncio
async def test_tier2_312_02_huge_nested_event_payload(
    e2e_service_factory: Any,
) -> None:
    """Verify 100KB nested tree payload envelopes and serializes cleanly."""
    svc = e2e_service_factory(name="huge_payload_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    deep_dict = {"leaf": "value", "list": list(range(5000))}
    await svc.publish_event("events.telemetry", payload=deep_dict)

    assert len(transport.published_messages) == 1
    envelope = json.loads(transport.published_messages[0][1].decode())
    assert envelope["data"]["payload"]["leaf"] == "value"
    assert len(envelope["data"]["payload"]["list"]) == 5000


@pytest.mark.asyncio
async def test_tier2_312_03_envelope_metadata_key_collision(
    e2e_service_factory: Any,
) -> None:
    """Verify user payload having keys named 'data', 'timestamp', or 'source_service' nest inside data."""
    svc = e2e_service_factory(name="collision_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    user_kwargs = {
        "data": "inner_data_content",
        "timestamp": "user_timestamp",
        "source_service": "custom_upstream",
    }
    await svc.publish_event("events.user_collision", **user_kwargs)

    envelope = json.loads(transport.published_messages[0][1].decode())
    assert envelope["source_service"] == "collision_svc"
    assert envelope["data"]["data"] == "inner_data_content"
    assert envelope["data"]["source_service"] == "custom_upstream"


@pytest.mark.asyncio
async def test_tier2_312_04_legacy_flat_payload_backward_compatibility(
    e2e_service_factory: Any,
) -> None:
    """Verify @validated_listener accepts legacy flat payloads seamlessly without DLQ."""
    received: list[OrderModel] = []

    class LegacyConsumer(CliffracerService):
        @validated_listener("orders.legacy", OrderModel, durable="legacy_worker")
        async def on_order(self, message: OrderModel) -> None:
            received.append(message)

    svc = e2e_service_factory(LegacyConsumer, name="legacy_consumer", jetstream_enabled=True)
    svc._discover_handlers()

    flat_payload = {"order_id": "flat_123", "amount": 75.0, "customer_id": "flat_user"}
    msg = MockJetStreamMsg(subject="orders.legacy", data=json.dumps(flat_payload).encode())

    await svc.container._dispatch_event(msg)
    assert len(received) == 1
    assert received[0].order_id == "flat_123"


@pytest.mark.asyncio
async def test_tier2_312_05_malformed_json_routes_to_dlq(
    e2e_service_factory: Any,
) -> None:
    """Verify unparseable JSON routes to DLQ and terminates."""

    class ValidatedConsumer(CliffracerService):
        @validated_listener("orders.strict", OrderModel, durable="strict_worker")
        async def on_order(self, order: OrderModel) -> None:
            pass

    svc = e2e_service_factory(ValidatedConsumer, name="strict_svc", jetstream_enabled=True)
    svc._discover_handlers()

    dlq_calls: list[Any] = []
    svc.container._publish_dlq = AsyncMock(side_effect=lambda *a, **kw: dlq_calls.append((a, kw)))  # type: ignore[method-assign]

    corrupted_msg = MockJetStreamMsg(
        subject="orders.strict",
        data=b"NOT_VALID_JSON{abc",
    )
    outcome = await svc.container._dispatch_event(corrupted_msg)
    assert outcome.name in ("INVALID", "ERROR")
    assert corrupted_msg.term_calls >= 1 or len(dlq_calls) >= 1


# === Feature: RPC Error Envelope Boundaries ===


@pytest.mark.asyncio
async def test_tier2_313_01_diverse_exception_types_enveloped(
    e2e_service_factory: Any,
) -> None:
    """Verify diverse exception types (KeyError, TypeError) return success: false."""

    class MultiErrorService(CliffracerService):
        @rpc
        async def raise_key(self) -> None:
            _ = {}["missing_key"]

        @rpc
        async def raise_type(self) -> None:
            _ = "string" + 123  # type: ignore[operator]

    svc = e2e_service_factory(MultiErrorService, name="multi_err_svc")
    svc._discover_handlers()

    for method in ("raise_key", "raise_type"):
        msg = MockJetStreamMsg(
            subject=f"multi_err_svc.{method}",
            data=b"{}",
            reply=f"_INBOX.{method}",
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_data is not None
        reply = json.loads(msg.response_data.decode())
        assert reply["success"] is False
        assert "error" in reply


@pytest.mark.asyncio
async def test_tier2_313_02_rpc_without_reply_subject_dropped_cleanly(
    e2e_service_factory: Any,
) -> None:
    """Verify RPC request without reply subject drops error response cleanly without crash."""
    svc = e2e_service_factory(name="no_reply_svc")
    msg = MockJetStreamMsg(
        subject="no_reply_svc.unknown_method",
        data=b"{}",
        reply=None,  # Fire-and-forget RPC error
    )
    # Should not raise exception
    await svc.container._handle_rpc_request(msg)
    assert not msg._response_sent


@pytest.mark.asyncio
async def test_tier2_313_03_exception_message_escaping_special_chars(
    e2e_service_factory: Any,
) -> None:
    """Verify exception messages containing quotes, newlines, and unicode serialize valid JSON."""

    class WeirdErrorService(CliffracerService):
        @rpc
        async def trigger(self) -> None:
            raise ValueError('Error with "quotes", \n newlines, \t tabs and 💥 emojis')

    svc = e2e_service_factory(WeirdErrorService, name="weird_svc", expose_internal_errors=True)
    svc._discover_handlers()

    msg = MockJetStreamMsg(
        subject="weird_svc.trigger",
        data=b"{}",
        reply="_INBOX.weird",
    )
    await svc.container._handle_rpc_request(msg)
    assert msg.response_data is not None
    # Must parse without JSONDecodeError
    reply = json.loads(msg.response_data.decode())
    assert reply["success"] is False
    assert "quotes" in reply["error"]
    assert "emojis" in reply["error"]


@pytest.mark.asyncio
async def test_tier2_313_04_deeply_nested_validation_errors(
    e2e_service_factory: Any,
) -> None:
    """Verify nested Pydantic model validation failure returns detailed error structure."""

    class NestedRpcService(CliffracerService):
        @rpc
        async def submit(self, payload: NestedOuterModel) -> str:
            return "ok"

    svc = e2e_service_factory(NestedRpcService, name="nested_val_svc")
    svc._discover_handlers()

    bad_payload = {"payload": {"inner": {"count": -5}}}
    msg = MockJetStreamMsg(
        subject="nested_val_svc.submit",
        data=json.dumps(bad_payload).encode(),
        reply="_INBOX.nested_reply",
    )
    await svc.container._handle_rpc_request(msg)
    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply["success"] is False
    assert reply["error"] == "validation failed"
    assert "details" in reply


def test_tier2_313_05_client_error_handling_unknown_method() -> None:
    """Verify ServiceClient._raise_for_error properly interprets success: false unknown method."""
    client = ServiceClient("order_service")
    error_reply = {
        "success": False,
        "error": "Unknown method: missing_rpc",
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": "corr-client-err",
    }
    with pytest.raises(RpcUnknownMethod) as excinfo:
        client._raise_for_error(error_reply, "order_service.missing_rpc")
    assert "missing_rpc" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tier 3: Cross-Feature Interactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_correlation_preservation_rpc_to_publish_to_listener(
    e2e_service_factory: Any,
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

    svc = e2e_service_factory(OrderOrchestrator, name="orchestrator", jetstream_enabled=True)
    transport = MockNatsTransport()
    svc.container.nc = transport
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
    assert len(transport.published_messages) == 1
    subj, payload_bytes, headers, reply = transport.published_messages[0]
    envelope = json.loads(payload_bytes.decode())
    assert envelope["correlation_id"] == trace_id
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
    e2e_service_factory: Any,
) -> None:
    """Cross-feature: Inbound correlation preserved on RPC validation failure response."""

    class StrictService(CliffracerService):
        @rpc
        async def execute(self, code: int) -> str:
            return f"code_{code}"

    svc = e2e_service_factory(StrictService, name="strict_svc")
    svc._discover_handlers()

    trace_id = "trace-fail-corr-4444"
    msg = MockJetStreamMsg(
        subject="strict_svc.execute",
        data=json.dumps({"code": "invalid_string_code"}).encode(),
        reply="_INBOX.strict_reply",
        headers={"X-Correlation-ID": trace_id},
    )

    await svc.container._handle_rpc_request(msg)
    assert msg.response_data is not None
    reply = json.loads(msg.response_data.decode())
    assert reply["success"] is False
    assert reply["correlation_id"] == trace_id


@pytest.mark.asyncio
async def test_tier3_dlq_envelope_carries_inbound_correlation(
    e2e_service_factory: Any,
) -> None:
    """Cross-feature: Poison message routed to DLQ includes original X-Correlation-ID."""

    class FailingConsumer(CliffracerService):
        @validated_listener("items.process", OrderModel, durable="failing_worker")
        async def on_item(self, message: OrderModel) -> None:
            pass

    svc = e2e_service_factory(FailingConsumer, name="failing_svc", jetstream_enabled=True)
    svc._discover_handlers()

    dlq_captures: list[dict[str, Any]] = []

    async def mock_dlq_publish(subject: str, *args: Any, **kwargs: Any) -> None:
        headers = kwargs.get("headers") or (args[1] if len(args) > 1 else {})
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
    # Check DLQ headers carry X-Correlation-ID
    cid = CorrelationContext.extract_from_headers(dlq_headers)
    assert cid == trace_id

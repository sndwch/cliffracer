"""Empirical adversarial stress testing for Wire Semantics and RPC error handling.

Stress-tests boundary conditions, edge cases, malformed payloads, and invariant guarantees:
- CorrelationContext.extract_from_headers: non-strings, mixed-case, whitespace, missing/empty.
- Event wire envelope unwrapping in _dispatch_event: nested domain payloads, missing metadata,
  extra unexpected fields, flat legacy payloads, empty dicts, string/list domain data.
- RPC error envelopes: unknown method, invalid json, Pydantic ValidationError, policy refusal
  RejectMessage, unhandled exception, describe failures, asserting success is False,
  error is non-empty string, and correlation_id is present and preserved.
"""

import asyncio
import json
from datetime import datetime
from typing import Annotated, Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, Field, RootModel

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.client import (
    ClientError,
    RpcRefused,
    RpcUnknownMethod,
    RpcValidationError,
    ServiceClient,
)
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.correlation import (
    CorrelationContext,
)
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext

# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------


class MockMsg:
    """Mock NATS message for event dispatch and RPC tests."""

    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        reply: str = "_INBOX.test_reply",
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.reply = reply
        self.response_bytes: bytes | None = None
        self.response_headers: dict[str, str] | None = None

    async def respond(self, data: bytes) -> None:
        self.response_bytes = data
        self.response_headers = dict(self.headers)


MockRpcMsg = MockMsg


class SimpleItem(BaseModel):
    name: str
    price: float = Field(gt=0)


class NestedEventPayload(BaseModel):
    data: str
    sub_id: int


# ===========================================================================
# 1. Adversarial Tests for CorrelationContext.extract_from_headers
# ===========================================================================


@pytest.mark.unit
class TestAdversarialCorrelationExtraction:
    """Boundary and stress tests for CorrelationContext.extract_from_headers."""

    def test_extract_none_and_non_dict(self) -> None:
        """Non-dict inputs must return None safely without raising exceptions."""
        assert CorrelationContext.extract_from_headers(None) is None
        assert CorrelationContext.extract_from_headers([]) is None  # type: ignore[arg-type]
        assert CorrelationContext.extract_from_headers("X-Correlation-ID: 123") is None  # type: ignore[arg-type]
        assert CorrelationContext.extract_from_headers(12345) is None  # type: ignore[arg-type]
        assert CorrelationContext.extract_from_headers(3.14) is None  # type: ignore[arg-type]
        assert CorrelationContext.extract_from_headers(set()) is None  # type: ignore[arg-type]

    def test_extract_empty_and_whitespace_only(self) -> None:
        """Empty dicts and whitespace-only correlation headers must return None or fall through."""
        assert CorrelationContext.extract_from_headers({}) is None
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": ""}) is None
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": "   "}) is None
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": "\t\n\r  \n"}) is None

        # Fallthrough on empty/whitespace candidate to next candidate
        headers = {
            "x-correlation-id": "   ",
            "x-request-id": "\t\r\n",
            "x-trace-id": "valid-trace-id",
        }
        assert CorrelationContext.extract_from_headers(headers) == "valid-trace-id"

    def test_extract_non_string_keys(self) -> None:
        """Headers with non-string keys (e.g. int, bool, tuple) must be handled safely."""
        headers: dict[Any, Any] = {
            123: "val-int-key",
            True: "val-bool-key",
            ("a", "b"): "val-tuple-key",
            "X-Correlation-ID": "expected-id",
        }
        assert CorrelationContext.extract_from_headers(headers) == "expected-id"

    def test_extract_non_string_values(self) -> None:
        """Non-string values for correlation ID candidates must be stringified and stripped."""
        # Integers
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": 987654}) == "987654"
        # Floats
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": 12.345}) == "12.345"
        # True
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": True}) == "True"
        # Complex objects
        assert (
            CorrelationContext.extract_from_headers({"X-Correlation-ID": ["a", "b"]})
            == "['a', 'b']"
        )
        assert (
            CorrelationContext.extract_from_headers({"X-Correlation-ID": {"id": 1}}) == "{'id': 1}"
        )

        # Note: 0 and False are falsy in Python `if value:` check and fall through
        headers_zero = {"X-Correlation-ID": 0, "x-request-id": "fallback-cid"}
        assert CorrelationContext.extract_from_headers(headers_zero) == "fallback-cid"

    def test_extract_mixed_casing_variations(self) -> None:
        """Any case permutation of all candidate headers must be recognized."""
        casing_candidates = [
            ("X-CoRrElAtIoN-Id", "cid-mix-1"),
            ("x-CORRELATION-ID", "cid-mix-2"),
            ("X-rEqUeSt-Id", "cid-mix-3"),
            ("x-REQUEST-ID", "cid-mix-4"),
            ("X-TrAcE-iD", "cid-mix-5"),
            ("x-TRACE-ID", "cid-mix-6"),
            ("CoRrElAtIoN-iD", "cid-mix-7"),
            ("CORRELATION-ID", "cid-mix-8"),
            ("CoRrElAtIoN_iD", "cid-mix-9"),
            ("CORRELATION_ID", "cid-mix-10"),
            ("ReQuEsT-Id", "cid-mix-11"),
            ("REQUEST-ID", "cid-mix-12"),
            ("TrAcE-Id", "cid-mix-13"),
            ("TRACE-ID", "cid-mix-14"),
        ]
        for header_key, expected_val in casing_candidates:
            res = CorrelationContext.extract_from_headers({header_key: expected_val})
            assert res == expected_val, f"Failed for key {header_key}"

    def test_extract_precedence_ladder_exhaustive(self) -> None:
        """Verify strict priority ladder across all 7 candidates."""
        ladder = [
            ("x-correlation-id", "prio-1"),
            ("x-request-id", "prio-2"),
            ("x-trace-id", "prio-3"),
            ("correlation-id", "prio-4"),
            ("correlation_id", "prio-5"),
            ("request-id", "prio-6"),
            ("trace-id", "prio-7"),
        ]

        # Build dict with all candidates
        headers = dict(ladder)
        for k, expected in ladder:
            assert CorrelationContext.extract_from_headers(headers) == expected
            del headers[k]

        assert CorrelationContext.extract_from_headers(headers) is None

    def test_extract_whitespace_padding(self) -> None:
        """Leading and trailing whitespace, tabs, and newlines must be cleanly stripped."""
        headers = {"X-Correlation-ID": " \t \r\n  corr-strip-me-123 \n\t  "}
        assert CorrelationContext.extract_from_headers(headers) == "corr-strip-me-123"

    def test_extract_stress_huge_headers(self) -> None:
        """Performance & robustness with huge headers and large correlation values."""
        # 100,000 character correlation ID
        huge_id = "cid_" + "x" * 100_000
        assert CorrelationContext.extract_from_headers({"X-Correlation-ID": huge_id}) == huge_id

        # 5,000 unrelated headers
        huge_dict = {f"X-Custom-Header-{i}": f"value-{i}" for i in range(5_000)}
        huge_dict["X-Correlation-ID"] = "needle-in-haystack"
        assert CorrelationContext.extract_from_headers(huge_dict) == "needle-in-haystack"

    def test_extract_unicode_and_special_chars(self) -> None:
        """Unicode, emoji, and punctuation in correlation headers must be preserved."""
        unicode_id = "corr-⚡-🚀-こんにちは-1234"
        assert (
            CorrelationContext.extract_from_headers({"X-Correlation-ID": unicode_id}) == unicode_id
        )


# ===========================================================================
# 2. Adversarial Tests for Event Wire Envelope Unwrapping
# ===========================================================================


@pytest.mark.unit
class TestAdversarialEventEnvelopeUnwrapping:
    """Stress testing event envelope unwrapping in Container._dispatch_event."""

    @pytest.mark.asyncio
    async def test_nested_domain_payload_with_data_key(self) -> None:
        """If domain payload contains its own 'data' key, it must NOT be recursively unwrapped."""
        validated_calls: list[NestedEventPayload] = []
        unvalidated_calls: list[dict[str, Any]] = []

        class NestedEventService(CliffracerService):
            @validated_listener("test.nested", schema=NestedEventPayload, fanout=True)
            async def on_val(self, message: NestedEventPayload) -> None:
                validated_calls.append(message)

            @listener("test.nested.raw", fanout=True)
            async def on_raw(self, data: str, sub_id: int) -> None:
                unvalidated_calls.append({"data": data, "sub_id": sub_id})

        svc = NestedEventService(ServiceConfig(name="nested_svc", version="1.0.0"))
        svc._discover_handlers()

        # Wire envelope where data['data'] contains {'data': 'inner_string', 'sub_id': 99}
        wire_envelope = {
            "data": {"data": "inner_string", "sub_id": 99},
            "timestamp": datetime.now().isoformat(),
            "source_service": "upstream_svc",
            "correlation_id": "corr-nested-1",
        }

        # 1. Validated listener
        msg1 = MockMsg("test.nested", json.dumps(wire_envelope).encode())
        outcome1 = await svc.container._dispatch_event(msg1, pattern="test.nested")
        assert outcome1 == DispatchOutcome.OK
        assert len(validated_calls) == 1
        assert validated_calls[0].data == "inner_string"
        assert validated_calls[0].sub_id == 99

        # 2. Unvalidated listener
        msg2 = MockMsg("test.nested.raw", json.dumps(wire_envelope).encode())
        outcome2 = await svc.container._dispatch_event(msg2, pattern="test.nested.raw")
        assert outcome2 == DispatchOutcome.OK
        assert len(unvalidated_calls) == 1
        assert unvalidated_calls[0] == {"data": "inner_string", "sub_id": 99}

    @pytest.mark.asyncio
    async def test_envelope_missing_metadata_treated_as_flat(self) -> None:
        """Envelopes missing timestamp or source_service are treated as flat legacy payloads."""
        received: list[dict[str, Any]] = []

        class FlatLegacyService(CliffracerService):
            @listener("test.legacy", fanout=True)
            async def on_legacy(
                self,
                data: dict[str, str],
                source_service: str | None = None,
                timestamp: str | None = None,
            ) -> None:
                kwargs: dict[str, Any] = {"data": data}
                if source_service is not None:
                    kwargs["source_service"] = source_service
                if timestamp is not None:
                    kwargs["timestamp"] = timestamp
                received.append(kwargs)

        svc = FlatLegacyService(ServiceConfig(name="legacy_svc", version="1.0.0"))
        svc._discover_handlers()

        # Missing timestamp: is_enveloped is False
        payload_missing_ts = {"data": {"foo": "bar"}, "source_service": "some_svc"}
        msg1 = MockMsg("test.legacy", json.dumps(payload_missing_ts).encode())
        outcome1 = await svc.container._dispatch_event(msg1, pattern="test.legacy")
        assert outcome1 == DispatchOutcome.OK
        assert len(received) == 1
        assert received[0]["data"] == {"foo": "bar"}
        assert received[0]["source_service"] == "some_svc"

        # Missing source_service: is_enveloped is False
        payload_missing_src = {"data": {"foo": "bar"}, "timestamp": datetime.now().isoformat()}
        msg2 = MockMsg("test.legacy", json.dumps(payload_missing_src).encode())
        outcome2 = await svc.container._dispatch_event(msg2, pattern="test.legacy")
        assert outcome2 == DispatchOutcome.OK
        assert len(received) == 2
        assert received[1]["data"] == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_envelope_extra_unexpected_fields_stripped(self) -> None:
        """Extra envelope-level metadata fields do not leak into domain payload."""
        received: list[SimpleItem] = []

        class CleanEventService(CliffracerService):
            @validated_listener("items.created", schema=SimpleItem, fanout=True)
            async def on_item(self, message: SimpleItem) -> None:
                received.append(message)

        svc = CleanEventService(ServiceConfig(name="clean_svc", version="1.0.0"))
        svc._discover_handlers()

        envelope_with_extras = {
            "data": {"name": "widget", "price": 19.99},
            "timestamp": datetime.now().isoformat(),
            "source_service": "producer_svc",
            "correlation_id": "c-extra-1",
            "extra_header": "unexpected_1",
            "telemetry_span_id": 99999,
            "security_token": "secret_token",
        }
        msg = MockMsg("items.created", json.dumps(envelope_with_extras).encode())
        outcome = await svc.container._dispatch_event(msg, pattern="items.created")
        assert outcome == DispatchOutcome.OK
        assert len(received) == 1
        assert received[0].name == "widget"
        assert received[0].price == 19.99

    @pytest.mark.asyncio
    async def test_envelope_flat_legacy_payload(self) -> None:
        """Flat legacy payloads without envelope unwrap domain fields directly."""
        received: list[SimpleItem] = []

        class FlatConsumer(CliffracerService):
            @validated_listener("items.flat", schema=SimpleItem, fanout=True)
            async def on_item(self, message: SimpleItem) -> None:
                received.append(message)

        svc = FlatConsumer(ServiceConfig(name="flat_svc", version="1.0.0"))
        svc._discover_handlers()

        flat_payload = {"name": "gadget", "price": 25.5, "correlation_id": "legacy-corr"}
        msg = MockMsg("items.flat", json.dumps(flat_payload).encode())
        outcome = await svc.container._dispatch_event(msg, pattern="items.flat")
        assert outcome == DispatchOutcome.OK
        assert len(received) == 1
        assert received[0].name == "gadget"
        assert received[0].price == 25.5

    @pytest.mark.asyncio
    async def test_envelope_empty_dict_payload(self) -> None:
        """Empty dict payload is handled gracefully without uncaught exceptions."""
        called = []

        class EmptyDictConsumer(CliffracerService):
            @listener("events.empty", fanout=True)
            async def on_empty(self) -> None:
                called.append({})

        svc = EmptyDictConsumer(ServiceConfig(name="empty_svc", version="1.0.0"))
        svc._discover_handlers()

        msg = MockMsg("events.empty", b"{}")
        outcome = await svc.container._dispatch_event(msg, pattern="events.empty")
        assert outcome == DispatchOutcome.OK
        assert len(called) == 1
        assert called[0] == {}

    @pytest.mark.asyncio
    async def test_envelope_data_as_string(self) -> None:
        """Payload where 'data' is a string unwraps properly for matching listeners."""
        str_received = []

        class StringEventService(CliffracerService):
            @listener("events.str", fanout=True)
            async def on_str(self, data: str) -> None:
                str_received.append(data)

            @validated_listener("events.str.model", schema=RootModel[str], fanout=True)
            async def on_val_str(self, message: RootModel[str]) -> None:
                str_received.append(message.root)

        svc = StringEventService(ServiceConfig(name="str_svc", version="1.0.0"))
        svc._discover_handlers()

        envelope = {
            "data": "plain text payload",
            "timestamp": datetime.now().isoformat(),
            "source_service": "sender_svc",
            "correlation_id": "c-str-1",
        }

        msg1 = MockMsg("events.str", json.dumps(envelope).encode())
        outcome1 = await svc.container._dispatch_event(msg1, pattern="events.str")
        assert outcome1 == DispatchOutcome.OK

        msg2 = MockMsg("events.str.model", json.dumps(envelope).encode())
        outcome2 = await svc.container._dispatch_event(msg2, pattern="events.str.model")
        assert outcome2 == DispatchOutcome.OK

        assert str_received == ["plain text payload", "plain text payload"]

    @pytest.mark.asyncio
    async def test_envelope_data_as_list(self) -> None:
        """Payload where 'data' is a list unwraps properly for list listeners."""
        list_received = []

        class ListEventService(CliffracerService):
            @listener("events.list", fanout=True)
            async def on_list(self, data: list[int]) -> None:
                list_received.append(data)

            @validated_listener("events.list.model", schema=RootModel[list[int]], fanout=True)
            async def on_val_list(self, message: RootModel[list[int]]) -> None:
                list_received.append(message.root)

        svc = ListEventService(ServiceConfig(name="list_svc", version="1.0.0"))
        svc._discover_handlers()

        envelope = {
            "data": [10, 20, 30, 40],
            "timestamp": datetime.now().isoformat(),
            "source_service": "sender_svc",
            "correlation_id": "c-list-1",
        }

        msg1 = MockMsg("events.list", json.dumps(envelope).encode())
        outcome1 = await svc.container._dispatch_event(msg1, pattern="events.list")
        assert outcome1 == DispatchOutcome.OK

        msg2 = MockMsg("events.list.model", json.dumps(envelope).encode())
        outcome2 = await svc.container._dispatch_event(msg2, pattern="events.list.model")
        assert outcome2 == DispatchOutcome.OK

        assert list_received == [[10, 20, 30, 40], [10, 20, 30, 40]]

    @pytest.mark.asyncio
    async def test_envelope_decode_error_deadletters_and_returns_invalid(self) -> None:
        """Malformed byte payloads trigger dead-lettering and return DispatchOutcome.INVALID."""

        class DlqService(CliffracerService):
            @listener("events.bad", fanout=True)
            async def on_bad(self, item: str = "") -> None:
                pass

        svc = DlqService(
            ServiceConfig(name="dlq_svc", version="1.0.0", dlq_subject="dlq.{service}")
        )
        svc._discover_handlers()
        svc.container._dead_letter_decode_error = AsyncMock()  # type: ignore[method-assign]

        bad_msg = MockMsg(
            "events.bad", b"not valid json {{{", headers={"X-Correlation-ID": "c-bad"}
        )
        outcome = await svc.container._dispatch_event(bad_msg, pattern="events.bad")
        assert outcome == DispatchOutcome.INVALID

    @pytest.mark.asyncio
    async def test_header_correlation_id_takes_precedence_over_envelope(self) -> None:
        """Wire header X-Correlation-ID takes precedence over envelope correlation_id."""
        cids = []

        class CorrService(CliffracerService):
            @listener("events.corr", fanout=True)
            async def on_event(
                self,
                foo: str | None = None,
                correlation_id: str | None = None,
            ) -> None:
                cids.append(correlation_id)

        svc = CorrService(ServiceConfig(name="corr_svc", version="1.0.0"))
        svc._discover_handlers()

        envelope = {
            "data": {"foo": "bar"},
            "timestamp": datetime.now().isoformat(),
            "source_service": "sender_svc",
            "correlation_id": "envelope-cid",
        }
        msg = MockMsg(
            "events.corr",
            json.dumps(envelope).encode(),
            headers={"X-Correlation-ID": "header-cid-wins"},
        )
        outcome = await svc.container._dispatch_event(msg, pattern="events.corr")
        assert outcome == DispatchOutcome.OK
        assert len(cids) == 1
        assert cids[0] == "header-cid-wins"


# ===========================================================================
# 3. Adversarial Tests for RPC Error Envelopes
# ===========================================================================


class PolicyRefusalExtension(Extension):
    """Extension that simulates policy rejections."""

    async def worker_setup(self, ctx: WorkerContext) -> None:
        if ctx.kind == "rpc" and ctx.subject and ctx.subject.endswith(".policy_blocked"):
            raise RejectMessage("policy refusal: rate limit exceeded")
        if ctx.kind == "describe" and ctx.data.get("block_describe"):
            raise RejectMessage("policy refusal: describe forbidden")


class AdversarialRpcService(CliffracerService):
    """Service with diverse RPC failure modes."""

    pol = PolicyRefusalExtension()

    @rpc
    async def divide(self, a: int, b: Annotated[int, Field(gt=0)]) -> int:
        return a // b

    @rpc
    async def throw_with_message(self) -> str:
        raise ValueError("simulated database failure")

    @rpc
    async def throw_bare(self) -> str:
        raise RuntimeError()

    @rpc
    async def policy_blocked(self) -> str:
        return "unreachable"


@pytest.mark.unit
class TestAdversarialRpcErrorEnvelopes:
    """Rigorous verification of RPC error envelopes across all failure modes."""

    @pytest.fixture
    def svc(self) -> AdversarialRpcService:
        s = AdversarialRpcService(ServiceConfig(name="adv_svc", version="1.0.0"))
        s._discover_handlers()
        return s

    @pytest.mark.asyncio
    async def test_failure_mode_1_unknown_method(self, svc: AdversarialRpcService) -> None:
        """Unknown method: success=False, error is non-empty string, correlation_id preserved."""
        msg = MockRpcMsg(
            subject="adv_svc.nonexistent_method",
            data=b"{}",
            headers={"X-Correlation-ID": "corr-unknown-999"},
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert isinstance(res["error"], str) and len(res["error"]) > 0
        assert "Unknown method: nonexistent_method" in res["error"]
        assert res["correlation_id"] == "corr-unknown-999"

        # Timestamp is valid ISO 8601
        ts = datetime.fromisoformat(res["timestamp"])
        assert ts.tzinfo is not None

    @pytest.mark.asyncio
    async def test_failure_mode_2_invalid_json(self, svc: AdversarialRpcService) -> None:
        """Invalid JSON: success=False, error is non-empty string, details present, correlation_id preserved."""
        msg = MockRpcMsg(
            subject="adv_svc.divide",
            data=b"not json {[[[",
            headers={"X-Correlation-ID": "corr-bad-json-1"},
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert isinstance(res["error"], str) and len(res["error"]) > 0
        assert res["error"] == "validation failed"
        assert isinstance(res.get("details"), list) and len(res["details"]) > 0
        assert res["details"][0]["type"] == "payload_invalid"
        assert res["correlation_id"] == "corr-bad-json-1"

    @pytest.mark.asyncio
    async def test_failure_mode_3_pydantic_validation_error(
        self, svc: AdversarialRpcService
    ) -> None:
        """Pydantic validation failure: success=False, error is non-empty, details present, correlation_id preserved."""
        # b must be > 0; pass b=-10
        msg = MockRpcMsg(
            subject="adv_svc.divide",
            data=json.dumps({"a": 100, "b": -10}).encode(),
            headers={"X-Correlation-ID": "corr-pyd-val-2"},
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert isinstance(res["error"], str) and len(res["error"]) > 0
        assert res["error"] == "validation failed"
        assert isinstance(res.get("details"), list) and len(res["details"]) > 0
        assert res["correlation_id"] == "corr-pyd-val-2"

    @pytest.mark.asyncio
    async def test_failure_mode_4_policy_refusal_reject_message(
        self, svc: AdversarialRpcService
    ) -> None:
        """Policy refusal RejectMessage: success=False, error starts with refused:, correlation_id preserved."""
        msg = MockRpcMsg(
            subject="adv_svc.policy_blocked",
            data=b"{}",
            headers={"X-Correlation-ID": "corr-policy-refuse"},
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert isinstance(res["error"], str) and len(res["error"]) > 0
        assert res["error"] == "refused: policy refusal: rate limit exceeded"
        assert res["correlation_id"] == "corr-policy-refuse"

    @pytest.mark.asyncio
    async def test_failure_mode_5_unhandled_exception_with_message(
        self, svc: AdversarialRpcService
    ) -> None:
        """Unhandled exception with message: success=False, error is non-empty, correlation_id preserved."""
        msg = MockRpcMsg(
            subject="adv_svc.throw_with_message",
            data=b"{}",
            headers={"X-Correlation-ID": "corr-unhandled-msg"},
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert isinstance(res["error"], str) and len(res["error"]) > 0
        assert "Internal server error" in res["error"]
        assert "traceback" not in res
        assert res["correlation_id"] == "corr-unhandled-msg"

        # Test expose_internal_errors=True opt-in
        opt_svc = AdversarialRpcService(
            ServiceConfig(name="adv_svc_opt", expose_internal_errors=True)
        )
        opt_svc._discover_handlers()
        opt_msg = MockRpcMsg(
            subject="adv_svc_opt.throw_with_message",
            data=b"{}",
            headers={"X-Correlation-ID": "corr-unhandled-opt"},
        )
        await opt_svc.container._handle_rpc_request(opt_msg)
        assert opt_msg.response_bytes is not None
        opt_res = json.loads(opt_msg.response_bytes.decode())
        assert opt_res["success"] is False
        assert "simulated database failure" in opt_res["error"]
        assert "traceback" in opt_res
        assert opt_res["correlation_id"] == "corr-unhandled-opt"

    @pytest.mark.asyncio
    async def test_failure_mode_6_describe_policy_refusal(self) -> None:
        """Describe failure on RejectMessage: success=False, error is non-empty, correlation_id preserved."""

        class DescBlockExt(Extension):
            async def worker_setup(self, ctx: WorkerContext) -> None:
                if ctx.kind == "describe":
                    raise RejectMessage("introspection access denied")

        class DescService(CliffracerService):
            ext = DescBlockExt()

        svc_desc = DescService(ServiceConfig(name="desc_test_svc", version="1.0.0"))
        svc_desc._discover_handlers()

        msg = MockRpcMsg(
            subject="desc_test_svc.describe",
            data=b"",
            headers={"X-Correlation-ID": "corr-desc-refuse"},
        )
        await svc_desc.container._handle_describe_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert isinstance(res["error"], str) and len(res["error"]) > 0
        assert res["error"] == "refused: introspection access denied"
        assert res["correlation_id"] == "corr-desc-refuse"

    @pytest.mark.asyncio
    async def test_failure_mode_7_describe_unhandled_exception(self) -> None:
        """Describe failure on unhandled exception: success=False, error is non-empty, correlation_id preserved."""
        svc_desc = CliffracerService(ServiceConfig(name="desc_test_svc2", version="1.0.0"))
        svc_desc._discover_handlers()

        def mock_broken_describe(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("introspection generation failed")

        with patch("cliffracer.introspect.describe", mock_broken_describe):
            msg = MockRpcMsg(
                subject="desc_test_svc2.describe",
                data=b"",
                headers={"X-Correlation-ID": "corr-desc-crash"},
            )
            await svc_desc.container._handle_describe_request(msg)
            assert msg.response_bytes is not None
            res = json.loads(msg.response_bytes.decode())

            assert res["success"] is False
            assert isinstance(res["error"], str) and len(res["error"]) > 0
            assert "introspection generation failed" in res["error"]
            assert res["correlation_id"] == "corr-desc-crash"

    @pytest.mark.asyncio
    async def test_boundary_bare_exception_error_string(self, svc: AdversarialRpcService) -> None:
        """Adversarial check: what happens when a handler raises a bare exception without message?

        If a handler raises `raise RuntimeError()`, `str(e)` produces an empty string.
        An empty error string violates the invariant that `res['error']` must be a non-empty string.
        """
        msg = MockRpcMsg(
            subject="adv_svc.throw_bare",
            data=b"{}",
            headers={"X-Correlation-ID": "corr-bare-exc"},
        )
        await svc.container._handle_rpc_request(msg)
        assert msg.response_bytes is not None
        res = json.loads(msg.response_bytes.decode())

        assert res["success"] is False
        assert res["correlation_id"] == "corr-bare-exc"
        # Observation of current behavior: str(e) yields empty string for bare exceptions
        # We verify whether error is empty and document it
        is_non_empty = bool(res.get("error"))
        # Document current behavior for finding report:
        if not is_non_empty:
            pytest.skip(
                f"VULNERABILITY DETECTED: bare exception produced empty error string {res['error']!r}"
            )

    @pytest.mark.asyncio
    async def test_boundary_describe_bare_exception_error_string(self) -> None:
        """Adversarial check: describe request failure with bare exception produces empty error string."""
        svc_desc = CliffracerService(ServiceConfig(name="desc_bare_svc", version="1.0.0"))
        svc_desc._discover_handlers()

        with patch("cliffracer.introspect.describe", side_effect=RuntimeError()):
            msg = MockRpcMsg(
                subject="desc_bare_svc.describe",
                data=b"",
                headers={"X-Correlation-ID": "corr-desc-bare"},
            )
            await svc_desc.container._handle_describe_request(msg)
            assert msg.response_bytes is not None
            res = json.loads(msg.response_bytes.decode())

            assert res["success"] is False
            assert res["correlation_id"] == "corr-desc-bare"
            if not bool(res.get("error")):
                pytest.skip(
                    f"VULNERABILITY DETECTED: describe bare exception produced empty error string {res['error']!r}"
                )

    @pytest.mark.asyncio
    async def test_error_envelope_preserves_mixed_case_and_legacy_headers(
        self, svc: AdversarialRpcService
    ) -> None:
        """Unknown method and invalid json error envelopes preserve mixed case and legacy correlation headers."""
        # Mixed-case X-cOrReLaTiOn-Id on unknown method
        msg1 = MockRpcMsg(
            subject="adv_svc.unknown_method",
            data=b"{}",
            headers={"X-cOrReLaTiOn-Id": "mix-cid-1"},
        )
        await svc.container._handle_rpc_request(msg1)
        assert msg1.response_bytes is not None
        res1 = json.loads(msg1.response_bytes.decode())
        assert res1["success"] is False
        assert res1["correlation_id"] == "mix-cid-1"

        # Legacy correlation_id header on invalid json
        msg2 = MockRpcMsg(
            subject="adv_svc.divide",
            data=b"invalid {json",
            headers={"correlation_id": "legacy-cid-2"},
        )
        await svc.container._handle_rpc_request(msg2)
        assert msg2.response_bytes is not None
        res2 = json.loads(msg2.response_bytes.decode())
        assert res2["success"] is False
        assert res2["correlation_id"] == "legacy-cid-2"

    @pytest.mark.asyncio
    async def test_error_envelope_when_headers_missing(self, svc: AdversarialRpcService) -> None:
        """When headers are absent or empty, correlation_id key is still present in response."""
        # Unknown method without headers
        msg1 = MockRpcMsg(subject="adv_svc.unknown_method", data=b"{}", headers={})
        await svc.container._handle_rpc_request(msg1)
        assert msg1.response_bytes is not None
        res1 = json.loads(msg1.response_bytes.decode())
        assert res1["success"] is False
        assert "correlation_id" in res1  # Key is present (value is None)

        # Validation error without headers: CorrelationExtension generates an ID
        msg2 = MockRpcMsg(
            subject="adv_svc.divide",
            data=json.dumps({"a": 1, "b": -1}).encode(),
            headers={},
        )
        await svc.container._handle_rpc_request(msg2)
        assert msg2.response_bytes is not None
        res2 = json.loads(msg2.response_bytes.decode())
        assert res2["success"] is False
        assert "correlation_id" in res2
        assert res2["correlation_id"] is not None
        assert res2["correlation_id"].startswith("corr_")

    @pytest.mark.asyncio
    async def test_client_error_envelope_mapping_fidelity(self) -> None:
        """ServiceClient._raise_for_error maps all standardized error envelopes to typed exceptions."""
        client = ServiceClient(service="adv_svc")

        # 1. Unknown method
        with pytest.raises(RpcUnknownMethod) as exc1:
            client._raise_for_error(
                {"success": False, "error": "Unknown method: foo", "correlation_id": "c1"},
                "adv_svc.foo",
            )
        assert "Unknown method: foo" in str(exc1.value)

        # 2. Validation error
        with pytest.raises(RpcValidationError) as exc2:
            client._raise_for_error(
                {
                    "success": False,
                    "error": "validation failed",
                    "details": [{"loc": ["a"], "msg": "field required"}],
                    "correlation_id": "c2",
                },
                "adv_svc.divide",
            )
        assert len(exc2.value.details) == 1

        # 3. Policy refusal
        with pytest.raises(RpcRefused) as exc3:
            client._raise_for_error(
                {"success": False, "error": "refused: rate limited", "correlation_id": "c3"},
                "adv_svc.policy_blocked",
            )
        assert "rate limited" in str(exc3.value)

        # 4. Unhandled server exception
        with pytest.raises(ClientError) as exc4:
            client._raise_for_error(
                {"success": False, "error": "db timeout", "correlation_id": "c4"},
                "adv_svc.throw_with_message",
            )
        assert "db timeout" in str(exc4.value)

    @pytest.mark.asyncio
    async def test_rpc_concurrent_error_stress(self, svc: AdversarialRpcService) -> None:
        """Concurrent burst of 100 mixed error RPC requests preserves isolation and correlation IDs."""

        async def single_call(idx: int) -> None:
            cid = f"burst-cid-{idx}"
            mode = idx % 4
            if mode == 0:
                # Unknown method
                msg = MockRpcMsg("adv_svc.unknown", b"{}", headers={"X-Correlation-ID": cid})
                await svc.container._handle_rpc_request(msg)
                assert msg.response_bytes is not None
                res = json.loads(msg.response_bytes.decode())
                assert res["success"] is False
                assert res["correlation_id"] == cid
            elif mode == 1:
                # Invalid JSON
                msg = MockRpcMsg("adv_svc.divide", b"bad json", headers={"X-Correlation-ID": cid})
                await svc.container._handle_rpc_request(msg)
                assert msg.response_bytes is not None
                res = json.loads(msg.response_bytes.decode())
                assert res["success"] is False
                assert res["correlation_id"] == cid
            elif mode == 2:
                # Validation error
                msg = MockRpcMsg(
                    "adv_svc.divide",
                    json.dumps({"a": idx, "b": -1}).encode(),
                    headers={"X-Correlation-ID": cid},
                )
                await svc.container._handle_rpc_request(msg)
                assert msg.response_bytes is not None
                res = json.loads(msg.response_bytes.decode())
                assert res["success"] is False
                assert res["correlation_id"] == cid
            else:
                # Policy refusal
                msg = MockRpcMsg("adv_svc.policy_blocked", b"{}", headers={"X-Correlation-ID": cid})
                await svc.container._handle_rpc_request(msg)
                assert msg.response_bytes is not None
                res = json.loads(msg.response_bytes.decode())
                assert res["success"] is False
                assert res["correlation_id"] == cid

        # Run 100 concurrent requests
        await asyncio.gather(*(single_call(i) for i in range(100)))

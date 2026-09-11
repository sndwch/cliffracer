"""Adversarial stress-tests for ServiceTestHarness in-memory dispatch.

Empirically verifies:
1. Method existence contracts (harness.publish vs harness.emit_event).
2. RPC in-memory dispatch, response envelopes, error structures, and validation rejections.
3. Event in-memory dispatch, unrouted messages, and schema validation failures.
4. Extension hook lifecycle (worker_setup, worker_result, worker_teardown, RejectMessage, and isolation).
5. In-flight task draining and clean teardown without broker transport dependencies.
"""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.decorators import validated_listener
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer.testing import ServiceTestHarness

# ==============================================================================
# TEST FIXTURES & EXTENSIONS
# ==============================================================================


class AuditExtension(Extension):
    """Audits dispatch lifecycle hooks and allows simulating rejections."""

    def __init__(
        self, reject_rpc: bool = False, reject_event: bool = False, fail_loud: bool = False
    ) -> None:
        super().__init__()
        self.reject_rpc = reject_rpc
        self.reject_event = reject_event
        self.fail_loud = fail_loud
        self.hook_log: list[str] = []

    async def worker_setup(self, ctx: WorkerContext) -> None:
        self.hook_log.append(f"setup:{ctx.kind}")
        if self.reject_rpc and ctx.kind == "rpc":
            raise RejectMessage("Access denied: Invalid credentials")
        if self.reject_event and ctx.kind == "event":
            raise RejectMessage("Event rejected: Malformed tenant")
        if self.fail_loud:
            raise RuntimeError("Telemetry failure in worker_setup")

    async def worker_result(
        self, ctx: WorkerContext, result: Any, exc: BaseException | None
    ) -> None:
        status = "error" if exc is not None else "ok"
        self.hook_log.append(f"result:{ctx.kind}:{status}")

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        self.hook_log.append(f"teardown:{ctx.kind}")


class TransactionModel(BaseModel):
    account_id: str
    amount: float = Field(gt=0)


class TransferResult(BaseModel):
    source: str
    target: str
    transferred: float


class MockBankingService(CliffracerService):
    audit: AuditExtension = AuditExtension()

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.received_events: list[dict[str, Any]] = []
        self.validated_events: list[TransactionModel] = []
        self.background_runs: int = 0

    @rpc
    def transfer(self, source: str, target: str, amount: float) -> TransferResult:
        if amount <= 0:
            raise ValueError("Transfer amount must be positive")
        return TransferResult(source=source, target=target, transferred=amount)

    @rpc
    def fail_unhandled(self) -> str:
        raise RuntimeError("Fatal database connection crash")

    @rpc
    async def spawn_task(self) -> str:
        async def bg_worker() -> None:
            await asyncio.sleep(0.01)
            self.background_runs += 1

        self.container.lifecycle.spawn_supervised_task(bg_worker())
        return "spawned"

    @listener("banking.audit", fanout=True)
    async def on_audit_event(
        self,
        action: str | None = None,
        event: str | None = None,
        user: str | None = None,
    ) -> None:
        data = {
            k: v
            for k, v in {"action": action, "event": event, "user": user}.items()
            if v is not None
        }
        self.received_events.append(data)

    @validated_listener("banking.transaction", schema=TransactionModel, fanout=True)
    async def on_transaction(self, message: TransactionModel) -> None:
        self.validated_events.append(message)


# ==============================================================================
# 1. API METHOD EXISTENCE & PUBLISH CONTRACT
# ==============================================================================


@pytest.mark.unit
async def test_harness_publish_alias_for_emit_event() -> None:
    """ServiceTestHarness implements publish as an alias for emit_event."""
    async with ServiceTestHarness(MockBankingService) as harness:
        assert hasattr(harness, "emit_event")
        assert hasattr(harness, "publish")
        outcome = await harness.publish("banking.audit", {"action": "ping"})
        assert outcome == DispatchOutcome.OK
        svc: MockBankingService = harness.service  # type: ignore[assignment]
        assert len(svc.received_events) == 1
        assert svc.received_events[0] == {"action": "ping"}


# ==============================================================================
# 2. RPC IN-MEMORY DISPATCH & ENVELOPES
# ==============================================================================


@pytest.mark.unit
async def test_harness_rpc_successful_execution_and_envelope() -> None:
    """RPC invocation through harness returns decoded TestResponse with result and headers."""
    async with ServiceTestHarness(MockBankingService) as harness:
        resp = await harness.rpc(
            "transfer",
            source="acc_1",
            target="acc_2",
            amount=150.75,
            headers={"X-Correlation-ID": "test-corr-100"},
        )
        assert resp.success is True
        assert resp.error is None
        assert resp.result == {"source": "acc_1", "target": "acc_2", "transferred": 150.75}
        assert resp.headers.get("Content-Type") == "application/json"
        assert isinstance(resp.data, dict)
        assert "timestamp" in resp.data
        assert resp.data["correlation_id"] == "test-corr-100"


@pytest.mark.unit
async def test_harness_rpc_application_exception_envelope() -> None:
    """Application exceptions in RPC handlers are caught and packaged in error response envelope."""
    async with ServiceTestHarness(MockBankingService) as harness:
        # 1. Handled domain error (ValueError) -> Sanitized in default mode
        resp = await harness.rpc("transfer", source="a", target="b", amount=-50.0)
        assert resp.success is False
        assert "Internal server error" in (resp.error or "")
        assert isinstance(resp.data, dict)
        assert resp.data["success"] is False
        assert "traceback" not in resp.data

        # 2. Unhandled crash (RuntimeError) -> Sanitized in default mode
        resp_fatal = await harness.rpc("fail_unhandled")
        assert resp_fatal.success is False
        assert "Internal server error" in (resp_fatal.error or "")
        assert resp_fatal.data["success"] is False
        assert "traceback" not in resp_fatal.data

    # Opt-in with expose_internal_errors=True
    opt_config = ServiceConfig(name="banking_opt", health_port=0, expose_internal_errors=True)
    async with ServiceTestHarness(MockBankingService, config=opt_config) as harness_opt:
        resp_opt = await harness_opt.rpc("transfer", source="a", target="b", amount=-50.0)
        assert resp_opt.success is False
        assert resp_opt.error == "Transfer amount must be positive"
        assert isinstance(resp_opt.data, dict)
        assert "traceback" in resp_opt.data

        resp_fatal_opt = await harness_opt.rpc("fail_unhandled")
        assert resp_fatal_opt.success is False
        assert "Fatal database connection crash" in (resp_fatal_opt.error or "")
        assert isinstance(resp_fatal_opt.data, dict)
        assert "traceback" in resp_fatal_opt.data


@pytest.mark.unit
async def test_harness_rpc_schema_validation_error_envelope() -> None:
    """Type mismatches in RPC parameters return schema validation failure envelope."""
    async with ServiceTestHarness(MockBankingService) as harness:
        resp = await harness.rpc("transfer", source="a", target="b", amount="not_a_float")
        assert resp.success is False
        assert resp.error is not None
        assert "validation" in resp.error.lower()
        assert isinstance(resp.data, dict)
        assert "details" in resp.data


@pytest.mark.unit
async def test_harness_rpc_unknown_method_envelope() -> None:
    """Calling an undeclared RPC method returns Unknown method error response."""
    async with ServiceTestHarness(MockBankingService) as harness:
        resp = await harness.rpc("nonexistent_rpc_method")
        assert resp.success is False
        assert resp.error == "Unknown method: nonexistent_rpc_method"


# ==============================================================================
# 3. EVENT IN-MEMORY DISPATCH & OUTCOMES
# ==============================================================================


@pytest.mark.unit
async def test_harness_emit_event_delivers_to_listener() -> None:
    """Event emitted through harness routes to @listener and returns DispatchOutcome.OK."""
    async with ServiceTestHarness(MockBankingService) as harness:
        outcome = await harness.emit_event(
            "banking.audit",
            {"event": "login", "user": "alice"},
            headers={"X-Correlation-ID": "event-corr-200"},
        )
        assert outcome == DispatchOutcome.OK
        svc: MockBankingService = harness.service  # type: ignore
        assert len(svc.received_events) == 1
        assert svc.received_events[0]["event"] == "login"
        assert svc.received_events[0]["user"] == "alice"


@pytest.mark.unit
async def test_harness_emit_event_unrouted_subject_returns_ok() -> None:
    """Emitting to an unregistered subject completes with DispatchOutcome.OK and 0 invocations."""
    async with ServiceTestHarness(MockBankingService) as harness:
        outcome = await harness.emit_event("unregistered.subject", {"key": "val"})
        assert outcome == DispatchOutcome.OK
        svc: MockBankingService = harness.service  # type: ignore
        assert len(svc.received_events) == 0


@pytest.mark.unit
async def test_harness_emit_event_validated_listener_success_and_invalid() -> None:
    """Validated listener validates schema; invalid payload returns DispatchOutcome.INVALID."""
    async with ServiceTestHarness(MockBankingService) as harness:
        # Valid payload
        ok_outcome = await harness.emit_event(
            "banking.transaction",
            {"account_id": "acc_99", "amount": 250.0},
        )
        assert ok_outcome == DispatchOutcome.OK
        svc: MockBankingService = harness.service  # type: ignore
        assert len(svc.validated_events) == 1
        assert svc.validated_events[0].account_id == "acc_99"
        assert svc.validated_events[0].amount == 250.0

        # Invalid payload (amount <= 0 violates gt=0 constraint)
        bad_outcome = await harness.emit_event(
            "banking.transaction",
            {"account_id": "acc_99", "amount": -10.0},
        )
        assert bad_outcome == DispatchOutcome.INVALID
        # Handler must NOT have executed for the invalid message
        assert len(svc.validated_events) == 1


# ==============================================================================
# 4. EXTENSION HOOKS IN HARNESS DISPATCH
# ==============================================================================


@pytest.mark.unit
async def test_harness_rpc_executes_extension_lifecycle_hooks() -> None:
    """Harness executes worker_setup, handler, worker_result, and worker_teardown in sequence."""
    async with ServiceTestHarness(MockBankingService) as harness:
        ext: AuditExtension = next(
            e for e in harness.container.extensions if isinstance(e, AuditExtension)
        )

        resp = await harness.rpc("transfer", source="x", target="y", amount=10.0)
        assert resp.success is True
        assert ext.hook_log == ["setup:rpc", "result:rpc:ok", "teardown:rpc"]


@pytest.mark.unit
async def test_harness_rpc_extension_reject_message_returns_refused_envelope() -> None:
    """Extension raising RejectMessage in worker_setup skips handler and answers with error."""

    class RejectRpcService(CliffracerService):
        audit: AuditExtension = AuditExtension(reject_rpc=True)

        @rpc
        def do_work(self) -> str:
            return "should_not_run"

    async with ServiceTestHarness(RejectRpcService) as harness:
        resp = await harness.rpc("do_work")
        assert resp.success is False
        assert "refused: Access denied: Invalid credentials" in (resp.error or "")


@pytest.mark.unit
async def test_harness_rpc_extension_non_rejection_exception_is_isolated() -> None:
    """Generic exception in extension worker_setup is isolated (logged) and handler runs."""

    class BuggyExtService(CliffracerService):
        audit: AuditExtension = AuditExtension(fail_loud=True)

        @rpc
        def do_work(self) -> str:
            return "executed_safely"

    async with ServiceTestHarness(BuggyExtService) as harness:
        resp = await harness.rpc("do_work")
        assert resp.success is True
        assert resp.result == "executed_safely"


# ==============================================================================
# 5. INTROSPECTION & BACKGROUND TASK DRAINING
# ==============================================================================


@pytest.mark.unit
async def test_harness_describe_endpoint() -> None:
    """ServiceTestHarness queries describe metadata without network dialing."""
    async with ServiceTestHarness(MockBankingService) as harness:
        desc = await harness.describe()
        assert desc["service"] == "test_harness_svc"
        method_names = [m["name"] for m in desc.get("methods", [])]
        assert "transfer" in method_names
        assert "fail_unhandled" in method_names


@pytest.mark.unit
async def test_harness_teardown_drains_active_tasks() -> None:
    """ServiceTestHarness teardown awaits and drains in-flight supervised tasks."""
    async with ServiceTestHarness(MockBankingService) as harness:
        resp = await harness.rpc("spawn_task")
        assert resp.success is True
        assert len(harness.container.lifecycle.active_tasks) >= 1

    # After exiting context manager, teardown drained active tasks
    svc: MockBankingService = harness.service  # type: ignore
    assert svc.background_runs == 1
    assert len(harness.container.lifecycle.active_tasks) == 0


@pytest.mark.unit
async def test_harness_mock_nats_client_attributes() -> None:
    """Harness binds mock NATS client with is_connected=True, no real sockets."""
    async with ServiceTestHarness(MockBankingService) as harness:
        assert harness.container.nc is not None
        assert harness.container.nc.is_connected is True
        assert harness.container.nc.is_closed is False

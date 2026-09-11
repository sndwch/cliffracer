"""Unit test suite verifying decomposed MessageDispatcher collaborator classes."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from cliffracer.core.dispatch import (
    DeadLetterPublisher,
    DispatchOutcome,
    EventDispatcher,
    ExtensionPipeline,
    JetStreamDispatcher,
    OutboundDispatcher,
    RpcDispatcher,
)
from cliffracer.core.dispatcher import MessageDispatcher
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.typed_rpc import build_handler_spec


class DummyMsg:
    def __init__(
        self,
        subject: str,
        data: bytes = b"{}",
        headers: dict[str, str] | None = None,
        reply: str | None = "reply.inbox",
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.reply = reply
        self.responded: bytes | None = None
        self._acked = False
        self._naked = False
        self._termed = False
        self._in_progress = False

    async def respond(self, data: bytes) -> None:
        self.responded = data

    async def ack(self) -> None:
        self._acked = True

    async def nak(self, delay: float = 0.0) -> None:
        self._naked = True

    async def term(self) -> None:
        self._termed = True

    async def in_progress(self) -> None:
        self._in_progress = True


# ==============================================================================
# 1. ExtensionPipeline Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_pipeline_execution_order_and_hooks() -> None:
    events: list[str] = []

    class TestExt(Extension):
        def __init__(self, tag: str) -> None:
            super().__init__()
            self.tag = tag

        async def worker_setup(self, ctx: WorkerContext) -> None:
            events.append(f"{self.tag}:setup")

        async def worker_result(
            self, ctx: WorkerContext, result: Any, exc: BaseException | None
        ) -> None:
            events.append(f"{self.tag}:result")

        async def worker_teardown(self, ctx: WorkerContext) -> None:
            events.append(f"{self.tag}:teardown")

        async def before_call(self, ctx: WorkerContext) -> None:
            events.append(f"{self.tag}:before")

        async def after_call(
            self, ctx: WorkerContext, result: Any, exc: BaseException | None
        ) -> None:
            events.append(f"{self.tag}:after")

    ext1 = TestExt("ext1")
    ext2 = TestExt("ext2")
    pipeline = ExtensionPipeline([ext1, ext2])

    ctx = pipeline.create_send_context("rpc", "svc.rpc.test", {"a": 1}, "cid-123")
    assert ctx.correlation_id == "cid-123"

    async def call() -> str:
        events.append("called")
        return "ok"

    res = await pipeline.run_worker(ctx, call)
    assert res == "ok"
    assert events == [
        "ext1:setup",
        "ext2:setup",
        "called",
        "ext2:result",
        "ext1:result",
        "ext2:teardown",
        "ext1:teardown",
    ]

    events.clear()
    res = await pipeline.run_send_hooks(ctx, call)
    assert res == "ok"
    assert events == ["ext1:before", "ext2:before", "called", "ext2:after", "ext1:after"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_pipeline_fails_closed() -> None:
    class FailingExt(Extension):
        fails_closed = True

        async def worker_setup(self, ctx: WorkerContext) -> None:
            raise RuntimeError("boom")

    pipeline = ExtensionPipeline([FailingExt()])
    ctx = pipeline.create_send_context("rpc", "svc.rpc.test", {}, "cid")

    with pytest.raises(RejectMessage):
        await pipeline.run_worker(ctx, AsyncMock())


# ==============================================================================
# 2. DeadLetterPublisher Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dead_letter_publisher_subject_formatting_and_publishing() -> None:
    cfg = ServiceConfig(name="test_svc", dlq_subject="dlq.{service}")
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock(nc=mock_nc, js=None, jetstream_active=False))

    assert dlq.format_dlq_subject() == "dlq.test_svc"

    msg = DummyMsg("test_svc.events.fail", b"invalid-bytes")
    await dlq.dead_letter_decode_error(msg, ValueError("bad decode"))

    mock_nc.publish.assert_awaited_once()
    call_args = mock_nc.publish.await_args
    assert call_args.args[0] == "dlq.test_svc"


# ==============================================================================
# 3. RpcDispatcher Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_dispatcher_execution_and_error_envelopes() -> None:
    reg = ServiceRegistry()

    def multiply(x: int, y: int) -> int:
        return x * y

    reg.rpc_handlers["multiply"] = multiply
    reg.rpc_specs["multiply"] = build_handler_spec("multiply", multiply, owner=object)

    cfg = ServiceConfig(name="math_svc", max_rpc_concurrency=2)
    pipeline = ExtensionPipeline([])
    rpc = RpcDispatcher(reg, cfg, pipeline)

    msg = DummyMsg("math_svc.rpc.multiply", b'{"x": 6, "y": 7}')
    await rpc.handle_rpc_request(msg)

    assert msg.responded is not None
    import json

    resp = json.loads(msg.responded)
    assert resp["success"] is True
    assert resp["result"] == 42

    unknown_msg = DummyMsg("math_svc.rpc.divide", b'{"x": 6, "y": 7}')
    await rpc.handle_rpc_request(unknown_msg)
    resp_unknown = json.loads(unknown_msg.responded)
    assert resp_unknown["success"] is False
    assert "Unknown method" in resp_unknown["error"]


# ==============================================================================
# 4. EventDispatcher Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_dispatcher_routing_and_schema_validation() -> None:
    reg = ServiceRegistry()
    received: list[Any] = []

    class EventModel(BaseModel):
        count: int

    def on_event(message: EventModel) -> None:
        received.append(message.count)

    reg.event_handlers["order.created"] = on_event
    reg.event_schemas[on_event] = (EventModel, "deadletter")

    cfg = ServiceConfig(name="event_svc")
    pipeline = ExtensionPipeline([])
    mock_nc = MagicMock(publish=AsyncMock())
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock(nc=mock_nc, js=None, jetstream_active=False))
    events = EventDispatcher(reg, cfg, pipeline, dlq)

    # 1. Valid event
    msg_valid = DummyMsg("order.created", b'{"count": 99}')
    outcome = await events.handle_event(msg_valid, pattern="order.created")
    assert outcome == DispatchOutcome.OK
    assert received == [99]

    # 2. Invalid event schema -> routes to DLQ
    msg_invalid = DummyMsg("order.created", b'{"count": "not-an-int"}')
    outcome_invalid = await events.handle_event(msg_invalid, pattern="order.created")
    assert outcome_invalid == DispatchOutcome.INVALID
    mock_nc.publish.assert_awaited_once()


# ==============================================================================
# 5. JetStreamDispatcher Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jetstream_dispatcher_transport_protections_and_acks() -> None:
    cfg = ServiceConfig(name="js_svc", jetstream_max_deliver=3)
    pipeline = ExtensionPipeline([])
    reg = ServiceRegistry()
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock())
    events = EventDispatcher(reg, cfg, pipeline, dlq)
    js_disp = JetStreamDispatcher(cfg, lambda: MagicMock(), events, dlq)

    msg = DummyMsg("test.topic")
    assert await js_disp.safe_ack(msg) is True
    assert msg._acked is True

    assert await js_disp.safe_nak(msg, delay=1.0) is True
    assert msg._naked is True

    assert await js_disp.safe_term(msg) is True
    assert msg._termed is True

    assert await js_disp.safe_in_progress(msg) is True
    assert msg._in_progress is True


# ==============================================================================
# 6. OutboundDispatcher Tests
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbound_dispatcher_context_and_send_hooks() -> None:
    cfg = ServiceConfig(name="out_svc")
    pipeline = ExtensionPipeline([])
    outbound = OutboundDispatcher(cfg, lambda: MagicMock(), pipeline)

    ctx = outbound.send_context("rpc", "target.rpc", {"x": 1}, "cid-456")
    assert ctx.correlation_id == "cid-456"
    assert ctx.headers["X-Correlation-ID"] == "cid-456"

    executed = False

    async def send_fn() -> str:
        nonlocal executed
        executed = True
        return "sent"

    res = await outbound.run_send_hooks(ctx, send_fn)
    assert res == "sent"
    assert executed is True


# ==============================================================================
# 7. Facade and AST Statement Ceiling Invariant Test
# ==============================================================================


@pytest.mark.unit
def test_all_dispatcher_classes_under_statement_ceiling() -> None:
    """Verify that every class in cliffracer.core.dispatcher and submodules has <= 500 statements."""
    from cliffracer.core import dispatcher as dispatcher_module

    dispatcher_file = Path(inspect.getfile(dispatcher_module))
    files = [dispatcher_file]
    dispatch_dir = dispatcher_file.parent / "dispatch"
    if dispatch_dir.is_dir():
        files.extend(sorted(dispatch_dir.glob("*.py")))

    for file_path in files:
        content = file_path.read_text()
        tree = ast.parse(content)
        line_count = len(content.splitlines())
        assert line_count < 800, f"{file_path} exceeds 800 lines ({line_count} lines)"

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                stmts = sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt))
                assert stmts <= 500, (
                    f"Class {node.name} in {file_path.name} has {stmts} statements (>500)"
                )


@pytest.mark.unit
def test_zero_circular_container_dict_checks_in_core() -> None:
    """Verify that circular duck-typing checks into container.__dict__ are eliminated."""
    core_dir = Path("src/cliffracer/core")
    for py_file in core_dir.rglob("*.py"):
        content = py_file.read_text()
        assert 'getattr(container, "__dict__"' not in content, (
            f"Found circular container.__dict__ check in {py_file}"
        )
        assert 'getattr(self.service, "container", None)' not in content or (
            '__dict__["_' not in content
        ), f"Found circular service.container.__dict__ check in {py_file}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_message_dispatcher_facade_composition_and_delegation() -> None:
    """Verify MessageDispatcher facade composes all 6 collaborators and delegates cleanly."""
    cfg = ServiceConfig(name="facade_svc")
    reg = ServiceRegistry()
    conn_mock = MagicMock()
    dispatcher = MessageDispatcher(
        registry=reg,
        config=cfg,
        connection_provider=lambda: conn_mock,
        extensions=[],
    )

    # Invariants: all 6 collaborators instantiated
    assert isinstance(dispatcher.pipeline, ExtensionPipeline)
    assert isinstance(dispatcher.dlq, DeadLetterPublisher)
    assert isinstance(dispatcher.rpc, RpcDispatcher)
    assert isinstance(dispatcher.events, EventDispatcher)
    assert isinstance(dispatcher.jetstream, JetStreamDispatcher)
    assert isinstance(dispatcher.outbound, OutboundDispatcher)

    # Delegations
    assert dispatcher.format_dlq_subject() == "dlq.facade_svc"
    ctx = dispatcher._send_context("rpc", "sub", {}, "cid")
    assert ctx.correlation_id == "cid"

    msg = DummyMsg("sub")
    assert await dispatcher._safe_ack(msg) is True
    assert msg._acked is True

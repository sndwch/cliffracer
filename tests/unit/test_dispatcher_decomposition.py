"""Unit test suite verifying decomposed MessageDispatcher collaborator classes."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from cliffracer.core.correlation import CorrelationContext
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
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


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
        refuse_a_reply_with_no_subject(self)
        self.responded = data

    async def ack(self) -> None:
        self._acked = True

    async def nak(self, delay: float = 0.0) -> None:
        self._naked = True
        self.nak_delay = delay

    async def term(self) -> None:
        self._termed = True

    async def in_progress(self) -> None:
        self._in_progress = True


# ==============================================================================
# 1. ExtensionPipeline Tests
# ==============================================================================


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


@pytest.mark.asyncio
async def test_dead_letter_publisher_subject_formatting_and_publishing() -> None:
    cfg = ServiceConfig(name="test_svc", dlq_subject="dlq.{service}")
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock(nc=mock_nc, js=None, jetstream_active=False))

    assert dlq.format_dlq_subject() == "dlq.test_svc"

    headers: dict[str, str] = {}
    CorrelationContext.inject_into_headers(headers, "cid-dlq-1")
    msg = DummyMsg("test_svc.events.fail", b"invalid-bytes", headers=headers)
    msg.metadata = SimpleNamespace(num_delivered=3)
    await dlq.dead_letter_decode_error(msg, ValueError("bad decode"))

    mock_nc.publish.assert_awaited_once()
    call_args = mock_nc.publish.await_args
    assert call_args.args[0] == "dlq.test_svc"

    # What reaches the DLQ: the envelope, not just where it goes.
    envelope = json.loads(call_args.args[1])
    assert envelope["original_subject"] == "test_svc.events.fail"
    assert envelope["payload"] == {"raw": "invalid-bytes"}
    assert envelope["error"] == "Decode error: bad decode"
    assert envelope["service"] == "test_svc"
    assert envelope["deliveries"] == 3
    assert envelope["correlation_id"] == "cid-dlq-1"
    sent_headers = call_args.kwargs["headers"]
    assert sent_headers["Content-Type"] == "application/json"
    assert sent_headers["correlation_id"] == "cid-dlq-1"


# ==============================================================================
# 3. RpcDispatcher Tests
# ==============================================================================


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


@pytest.mark.asyncio
async def test_event_dispatcher_routing_and_schema_validation() -> None:
    reg = ServiceRegistry()
    received: list[Any] = []

    class EventModel(BaseModel):
        count: int

    def on_event(message: EventModel) -> None:
        received.append(message.count)

    reg.event_handlers["order.created"] = on_event
    reg.event_schemas["order.created"] = (EventModel, "deadletter")

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
    published = mock_nc.publish.await_args
    assert published.args[0] == "dlq.event_svc"
    envelope = json.loads(published.args[1])
    assert envelope["original_subject"] == "order.created"
    assert [e["loc"] for e in envelope["errors"]] == [["count"]]
    assert envelope["payload"] == {"count": "not-an-int"}


@pytest.mark.asyncio
async def test_with_no_pattern_the_subject_is_matched_against_every_registered_pattern() -> None:
    """The wildcard-routing branch: `handle_event(msg)` with no pattern iterates the registered
    patterns through `subject_matches`, so `*` and `>` patterns route and a stranger does not."""
    reg = ServiceRegistry()
    received: list[tuple[str, int]] = []

    class EventModel(BaseModel):
        count: int

    def on_order(message: EventModel) -> None:
        received.append(("order", message.count))

    def on_billing(message: EventModel) -> None:
        received.append(("billing", message.count))

    reg.event_handlers["order.*"] = on_order
    reg.event_handlers["billing.>"] = on_billing
    reg.event_schemas["order.*"] = (EventModel, "deadletter")
    reg.event_schemas["billing.>"] = (EventModel, "deadletter")
    cfg = ServiceConfig(name="event_svc")
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock(nc=MagicMock(publish=AsyncMock())))
    events = EventDispatcher(reg, cfg, ExtensionPipeline([]), dlq)

    one_token = await events.handle_event(DummyMsg("order.created", b'{"count": 1}'))
    tail = await events.handle_event(DummyMsg("billing.invoice.paid", b'{"count": 2}'))
    too_deep = await events.handle_event(DummyMsg("order.created.late", b'{"count": 3}'))
    stranger = await events.handle_event(DummyMsg("shipping.created", b'{"count": 4}'))

    assert (one_token, tail) == (DispatchOutcome.OK, DispatchOutcome.OK)
    assert (too_deep, stranger) == (DispatchOutcome.NO_HANDLER, DispatchOutcome.NO_HANDLER)
    assert received == [("order", 1), ("billing", 2)]


# ==============================================================================
# 5. JetStreamDispatcher Tests
# ==============================================================================


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
    assert msg.nak_delay == 1.0

    assert await js_disp.safe_term(msg) is True
    assert msg._termed is True

    assert await js_disp.safe_in_progress(msg) is True
    assert msg._in_progress is True


class _RaisingMsg(DummyMsg):
    """A message whose every broker call fails the way a dropped connection does."""

    async def ack(self) -> None:
        raise OSError("network unreachable")

    async def nak(self, delay: float = 0.0) -> None:
        raise OSError("network unreachable")

    async def term(self) -> None:
        raise OSError("network unreachable")

    async def in_progress(self) -> None:
        raise OSError("network unreachable")


@pytest.mark.asyncio
async def test_the_safe_methods_report_a_failed_broker_call_instead_of_raising() -> None:
    """The "protections": each of the four returns False, and does not raise, when the
    transport call fails. `safe_nak` is attempted with its delay, so the failure it reports
    is the nak's and not a skipped call."""
    cfg = ServiceConfig(name="js_svc")
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock())
    js_disp = JetStreamDispatcher(
        cfg,
        lambda: MagicMock(),
        EventDispatcher(ServiceRegistry(), cfg, ExtensionPipeline([]), dlq),
        dlq,
    )
    msg = _RaisingMsg("test.topic")

    assert await js_disp.safe_ack(msg) is False
    assert await js_disp.safe_nak(msg, delay=1.0) is False
    assert await js_disp.safe_term(msg) is False
    assert await js_disp.safe_in_progress(msg) is False


# ==============================================================================
# 6. OutboundDispatcher Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_outbound_dispatcher_context_and_send_hooks() -> None:
    cfg = ServiceConfig(name="out_svc")
    pipeline = ExtensionPipeline([])
    outbound = OutboundDispatcher(cfg, pipeline)

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


MAX_CLASS_STATEMENTS = 500
MAX_FILE_LINES = 800


def _dispatch_sources(dispatch_dir: Path) -> list[Path]:
    """Every module under the dispatch package, at any depth."""
    return sorted(dispatch_dir.rglob("*.py"))


def _ceiling_violations(file_path: Path) -> list[str]:
    """What `file_path` breaks of the dispatcher's size invariant: lines per file, statements per class."""
    content = file_path.read_text()
    violations = []
    line_count = len(content.splitlines())
    if line_count >= MAX_FILE_LINES:
        violations.append(f"{file_path} exceeds {MAX_FILE_LINES} lines ({line_count} lines)")
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, ast.ClassDef):
            stmts = sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt))
            if stmts > MAX_CLASS_STATEMENTS:
                violations.append(
                    f"Class {node.name} in {file_path} has {stmts} statements "
                    f"(>{MAX_CLASS_STATEMENTS})"
                )
    return violations


def test_all_dispatcher_classes_under_statement_ceiling() -> None:
    """Verify that every class in cliffracer.core.dispatcher and submodules has <= 500 statements."""
    from cliffracer.core import dispatcher as dispatcher_module

    dispatcher_file = Path(inspect.getfile(dispatcher_module))
    dispatch_dir = dispatcher_file.parent / "dispatch"
    # A missing or renamed package must fail here rather than narrow what is scanned.
    assert dispatch_dir.is_dir(), f"{dispatch_dir} is not the dispatch package"
    dispatch_files = _dispatch_sources(dispatch_dir)
    # An empty scan passes for any reason; today the package has seven modules.
    assert len(dispatch_files) >= 7, dispatch_files

    violations = [
        v for path in (dispatcher_file, *dispatch_files) for v in _ceiling_violations(path)
    ]

    assert not violations, violations


def test_the_ceiling_scan_reaches_a_module_in_a_nested_subpackage(tmp_path: Path) -> None:
    """The control for the test above: a module however deep is in scope, and an oversized
    class in it is reported. A flat `glob("*.py")` finds neither."""
    nested = tmp_path / "dispatch" / "sub"
    nested.mkdir(parents=True)
    monster = nested / "monster.py"
    body = "\n".join(f"        self.x{i} = {i}" for i in range(MAX_CLASS_STATEMENTS + 1))
    monster.write_text(f"class Monster:\n    def __init__(self):\n{body}\n")
    (tmp_path / "dispatch" / "small.py").write_text("class Small:\n    pass\n")

    assert monster in _dispatch_sources(tmp_path / "dispatch")
    violations = _ceiling_violations(monster)
    assert len(violations) == 1
    assert "Class Monster" in violations[0]
    assert _ceiling_violations(tmp_path / "dispatch" / "small.py") == []


CORE_DIR = Path(__file__).resolve().parents[2] / "src" / "cliffracer" / "core"


def _is_a_container(node: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    """An expression that names the container: `container`, `<x>.container`,
    `getattr(<x>, "container", ...)`, or a name in `aliases` (a name bound to one of those)."""
    if isinstance(node, ast.Name):
        return node.id == "container" or node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr == "container"
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "container"
    )


def _binds_a_container(value: ast.expr, aliases: frozenset[str]) -> bool:
    """A value that is, or may be, the container: the expression itself, either branch of a
    conditional expression, or any operand of `or` / `and`."""
    if isinstance(value, ast.IfExp):
        return _binds_a_container(value.body, aliases) or _binds_a_container(value.orelse, aliases)
    if isinstance(value, ast.BoolOp):
        return any(_binds_a_container(operand, aliases) for operand in value.values)
    return _is_a_container(value, aliases)


def _container_aliases(tree: ast.AST) -> frozenset[str]:
    """Names bound to the container anywhere in the module, however long the chain.

    `cont = getattr(self.service, "container", None)`, `cont = x.container`,
    `if (cont := x.container)`, `cont: Any = container`, and a name bound to such a name.
    A name is an alias wherever it appears in the file: a module-wide set is cruder than scoping
    and can only over-report, which a scan for a forbidden read can afford.
    """
    aliases: frozenset[str] = frozenset()
    while True:
        found = set(aliases)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _binds_a_container(node.value, aliases):
                found.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif (
                isinstance(node, ast.AnnAssign | ast.NamedExpr)
                and node.value is not None
                and isinstance(node.target, ast.Name)
                and _binds_a_container(node.value, aliases)
            ):
                found.add(node.target.id)
        if found == aliases:
            return aliases
        aliases = frozenset(found)


def _container_dict_reads(source: str) -> list[int]:
    """Line numbers where `source` reads the `__dict__` of a container, however it is spelled.

    The three ways to reach it: `<container>.__dict__`, `getattr(<container>, "__dict__", ...)`
    and `vars(<container>)`, where `<container>` may be a name the container was bound to. A
    search for one spelling of one of them passes for the rest.
    """
    tree = ast.parse(source)
    aliases = _container_aliases(tree)
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            if _is_a_container(node.value, aliases):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
            if node.func.id == "getattr" and len(node.args) >= 2:
                name = node.args[1]
                if (
                    isinstance(name, ast.Constant)
                    and name.value == "__dict__"
                    and _is_a_container(node.args[0], aliases)
                ):
                    lines.append(node.lineno)
            elif node.func.id == "vars" and _is_a_container(node.args[0], aliases):
                lines.append(node.lineno)
    return sorted(set(lines))


def test_zero_circular_container_dict_checks_in_core() -> None:
    """No module in core reads a container's `__dict__`: collaborators reach the connection
    through `connection_provider`, never back into the container that owns them."""
    core_files = sorted(CORE_DIR.rglob("*.py"))
    # An empty scan passes for any reason, so it has to find the files that exist.
    assert len(core_files) >= 30, f"found {len(core_files)} core files under {CORE_DIR}"

    offenders = {
        str(path.relative_to(CORE_DIR)): lines
        for path in core_files
        if (lines := _container_dict_reads(path.read_text()))
    }

    assert not offenders, f"container.__dict__ is read in core: {offenders}"


def test_the_core_scan_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert len(sorted(CORE_DIR.rglob("*.py"))) >= 30


@pytest.mark.parametrize(
    "spelling",
    [
        'getattr(container, "__dict__", {})',
        "getattr(container, '__dict__', {})",
        'getattr(self.container, "__dict__", {})',
        'self.container.__dict__["_rpc_specs"]',
        "vars(container)",
        "vars(self.service.container)",
        'getattr(getattr(self.service, "container", None), "__dict__", {})',
        "container.__dict__",
    ],
)
def test_CONTROL_every_spelling_of_a_container_dict_read_is_found(spelling: str) -> None:
    assert _container_dict_reads(f"def probe(self, container):\n    return {spelling}\n") == [2]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            '    cont = getattr(self.service, "container", None)\n'
            '    return cont.__dict__.get("_extensions")\n',
            id="getattr-alias-then-dict-get",
        ),
        pytest.param(
            "    cont = self.service.container\n    return cont.__dict__['x']\n",
            id="attribute-alias-then-subscript",
        ),
        pytest.param("    cont = container\n    return vars(cont)\n", id="name-alias-then-vars"),
        pytest.param(
            "    first = container\n    second = first\n    return second.__dict__\n",
            id="a-chain-of-aliases",
        ),
        pytest.param(
            '    if (cont := self.service.container) is not None:\n        return getattr(cont, "__dict__", None)\n',
            id="walrus-alias",
        ),
        pytest.param(
            "    cont: object = container\n    return cont.__dict__\n", id="annotated-alias"
        ),
        pytest.param(
            "    cont = self.service.container if self.service else None\n    return vars(cont)\n",
            id="conditional-alias",
        ),
        pytest.param(
            '    cont = getattr(self.service, "container", None) or container\n    return cont.__dict__\n',
            id="or-alias",
        ),
    ],
)
def test_CONTROL_a_dict_read_through_a_name_the_container_was_bound_to_is_found(
    body: str,
) -> None:
    reads = _container_dict_reads(f"def probe(self, container):\n{body}")

    assert len(reads) == 1 and reads[0] >= 3


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    cont = self.registry\n    return cont.__dict__\n", id="another-object"),
        pytest.param(
            "    cont = self.config if container else None\n    return vars(cont)\n",
            id="a-conditional-whose-value-is-not-the-container",
        ),
        pytest.param(
            "    cont = obj\n    return getattr(cont, '__dict__', {})\n", id="a-parameter"
        ),
    ],
)
def test_CONTROL_a_dict_read_through_a_name_that_is_not_the_container_is_not_reported(
    body: str,
) -> None:
    assert _container_dict_reads(f"def probe(self, container, obj):\n{body}") == []


@pytest.mark.parametrize(
    "innocent",
    [
        "self.config.__dict__",
        'getattr(obj, "__dict__", {})',
        "vars(self.registry)",
        "container.extensions",
        'getattr(self.service, "container", None)',
    ],
)
def test_CONTROL_a_read_that_is_not_of_a_container_is_not_reported(innocent: str) -> None:
    assert _container_dict_reads(f"def probe(self, container, obj):\n    return {innocent}\n") == []


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


_SENTINEL = object()

# The members that give back what the collaborator returned; the rest return None.
_RETURNING = {
    "handle_event",
    "make_event_callback",
    "_get_handler_meta",
    "_safe_ack",
    "_safe_nak",
    "_safe_term",
    "_safe_in_progress",
    "_dead_letter_decode_error",
    "_dead_letter_terminated",
    "_handle_invalid_message",
    "make_jetstream_event_callback",
    "pull_once",
    "format_dlq_subject",
    "_run_worker",
    "_run_send_hooks",
    "_send_context",
}
_MSG, _ERR, _SUB, _CTX = object(), object(), object(), object()


def _call():  # a stand-in for the awaitable factories the pipeline methods take
    return None


# (facade member, collaborator, its method, call args, call kwargs, args it must receive,
#  kwargs it must receive). Every row is a pure forward: what goes in comes out the other side.
_FORWARDS = [
    ("on_rpc_request", "rpc", "on_rpc_request", (_MSG,), {}, (_MSG,), {}),
    ("_bounded_handle_rpc", "rpc", "_bounded_handle_rpc", (_MSG,), {}, (_MSG,), {}),
    ("on_describe_request", "rpc", "on_describe_request", (_MSG,), {}, (_MSG,), {}),
    ("on_async_request", "rpc", "on_async_request", (_MSG,), {}, (_MSG,), {}),
    ("_bounded_handle_async_rpc", "rpc", "_bounded_handle_async_rpc", (_MSG,), {}, (_MSG,), {}),
    ("handle_rpc_request", "rpc", "handle_rpc_request", (_MSG,), {}, (_MSG,), {}),
    ("handle_describe_request", "rpc", "handle_describe_request", (_MSG,), {}, (_MSG,), {}),
    ("handle_async_request", "rpc", "handle_async_request", (_MSG,), {}, (_MSG,), {}),
    ("_bounded_handle_event", "events", "_bounded_handle_event", (_MSG, "p"), {}, (_MSG, "p"), {}),
    (
        "handle_event",
        "events",
        "handle_event",
        (_MSG,),
        {"pattern": "p", "raise_on_error": True},
        (_MSG,),
        {"pattern": "p", "raise_on_error": True},
    ),
    ("make_event_callback", "events", "make_event_callback", ("p",), {}, ("p",), {}),
    ("_get_handler_meta", "events", "_get_handler_meta", (_MSG,), {}, (_MSG,), {}),
    ("_safe_ack", "jetstream", "safe_ack", (_MSG,), {}, (_MSG,), {}),
    ("_safe_nak", "jetstream", "safe_nak", (_MSG,), {"delay": 2.5}, (_MSG,), {"delay": 2.5}),
    ("_safe_term", "jetstream", "safe_term", (_MSG,), {}, (_MSG,), {}),
    ("_safe_in_progress", "jetstream", "safe_in_progress", (_MSG,), {}, (_MSG,), {}),
    (
        "make_jetstream_event_callback",
        "jetstream",
        "make_event_callback",
        ("p",),
        {},
        ("p",),
        {},
    ),
    (
        "_bounded_handle_jetstream_event",
        "jetstream",
        "_bounded_handle_jetstream_event",
        (_MSG, "p"),
        {},
        (_MSG, "p"),
        {},
    ),
    (
        "handle_jetstream_event",
        "jetstream",
        "handle_jetstream_event",
        (_MSG,),
        {"pattern": "p"},
        (_MSG,),
        {"pattern": "p"},
    ),
    ("pull_once", "jetstream", "pull_once", (_SUB,), {"pattern": "p"}, (_SUB,), {"pattern": "p"}),
    (
        "pull_loop",
        "jetstream",
        "pull_loop",
        (_SUB, "d"),
        {"pattern": "p", "is_running_fn": _call, "unsubscribe": _call},
        (_SUB, "d"),
        {"pattern": "p", "is_running_fn": _call, "unsubscribe": _call},
    ),
    (
        "report_consumer_drift",
        "jetstream",
        "report_consumer_drift",
        (_SUB, "d"),
        {"pattern": "p"},
        (_SUB, "d"),
        {"pattern": "p"},
    ),
    ("format_dlq_subject", "dlq", "format_dlq_subject", (), {}, (), {}),
    (
        "publish_dlq",
        "dlq",
        "publish_dlq",
        ("dlq.x",),
        {"payload": 1, "headers": {"h": "v"}, "extra": 2},
        ("dlq.x",),
        {"payload": 1, "headers": {"h": "v"}, "extra": 2},
    ),
    (
        "_dead_letter_decode_error",
        "dlq",
        "dead_letter_decode_error",
        (_MSG, _ERR),
        {},
        (_MSG, _ERR),
        {},
    ),
    (
        "_dead_letter_terminated",
        "dlq",
        "dead_letter_terminated",
        (_MSG, _ERR, 3),
        {"delivery_limit": "server max_deliver 3", "correlation_id": "c-1"},
        (_MSG, _ERR, 3),
        {"delivery_limit": "server max_deliver 3", "correlation_id": "c-1"},
    ),
    # No correlation id given: the facade passes `None` on, which is what the collaborator reads
    # as "take it from the delivery", so a facade that dropped the argument would not show here.
    (
        "_dead_letter_terminated",
        "dlq",
        "dead_letter_terminated",
        (_MSG, _ERR, 3),
        {"delivery_limit": "server max_deliver 3"},
        (_MSG, _ERR, 3),
        {"delivery_limit": "server max_deliver 3", "correlation_id": None},
    ),
    (
        "_handle_invalid_message",
        "dlq",
        "handle_invalid_message",
        ("s", {"a": 1}, _ERR, _SUB, "drop"),
        {"correlation_id": "c-1"},
        ("s", {"a": 1}, _ERR, _SUB, "drop"),
        {"correlation_id": "c-1"},
    ),
    ("_run_worker", "pipeline", "run_worker", (_CTX, _call), {}, (_CTX, _call), {}),
    ("_run_send_hooks", "pipeline", "run_send_hooks", (_CTX, _call), {}, (_CTX, _call), {}),
    (
        "_guarded_hook",
        "pipeline",
        "_guarded_hook",
        (_MSG, "hook", _ERR),
        {},
        (_MSG, "hook", _ERR),
        {},
    ),
    (
        "_send_context",
        "outbound",
        "send_context",
        ("rpc", "s", {"x": 1}, "cid"),
        {},
        ("rpc", "s", {"x": 1}, "cid"),
        {},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member", "collaborator", "method", "args", "kwargs", "expect_args", "expect_kwargs"),
    _FORWARDS,
    ids=[row[0] for row in _FORWARDS],
)
async def test_every_facade_member_forwards_to_its_collaborator_unchanged(
    member, collaborator, method, args, kwargs, expect_args, expect_kwargs
) -> None:
    """The facade is a forwarding layer, so each member must hand its collaborator exactly what
    it was given and give back what the collaborator returned. Includes `raise_on_error`, which
    nothing in production passes through the facade as True, so only this row notices it dropped."""
    dispatcher = MessageDispatcher(
        registry=ServiceRegistry(),
        config=ServiceConfig(name="facade_svc"),
        connection_provider=lambda: MagicMock(),
        extensions=[],
    )
    target = getattr(dispatcher, collaborator)
    is_async = inspect.iscoroutinefunction(getattr(MessageDispatcher, member))
    probe = (AsyncMock if is_async else MagicMock)(return_value=_SENTINEL)
    setattr(target, method, probe)

    result = getattr(dispatcher, member)(*args, **kwargs)
    if is_async:
        result = await result

    probe.assert_called_once_with(*expect_args, **expect_kwargs)
    assert (result is _SENTINEL) == (member in _RETURNING), (member, result)


def test_the_facade_aliases_are_the_members_they_name() -> None:
    aliases = {
        "safe_ack": "_safe_ack",
        "safe_nak": "_safe_nak",
        "safe_term": "_safe_term",
        "safe_in_progress": "_safe_in_progress",
        "dead_letter_decode_error": "_dead_letter_decode_error",
        "dead_letter_terminated": "_dead_letter_terminated",
        "handle_invalid_message": "_handle_invalid_message",
        "run_worker": "_run_worker",
        "run_send_hooks": "_run_send_hooks",
        "send_context": "_send_context",
    }

    for alias, member in aliases.items():
        assert getattr(MessageDispatcher, alias) is getattr(MessageDispatcher, member), alias

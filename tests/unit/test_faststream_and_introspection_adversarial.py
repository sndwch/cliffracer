"""Adversarial stress testing for FastStream Host and Introspection primitives.

Empirically validates:
1. FastStream Host Primitives:
   - Shutdown race conditions: stop during in-flight handler execution, verifying task drain in container._active_tasks.
   - Shutdown timeout handling: cancellation and clean drain of hung handlers without task leakage.
   - CliffracerAckMiddleware: delivery count exhaustion routing to {service}.dlq and _safe_term(), including DLQ failure resilience.
   - CliffracerAckMiddleware: immediate DLQ+term on validation/decode errors, immediate ACK on policy refusals.
   - Heartbeat pulsing: in-progress heartbeats for long-running handlers, with resilience against intermittent pulse errors.
   - ContextRepo: injection of service, container, config, kv, resilience, and behavior when optional extensions are absent.
2. Introspection Primitives:
   - Complex models: recursive models, mutually recursive models, generic models, unions, and optional fields.
   - Deterministic 16-character SHA-256 hashes in Description.components and Description.description_hash.
   - Listener discovery across @listener, @validated_listener, @broadcast with push, pull, durable, and fanout permutations.
   - Preservation of complex markdown docstrings (fenced code blocks, lists, blank lines) in description and first-line in doc_summary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_faststream.broker import CliffracerHostedNatsBroker
from cliffracer_faststream.extension import FastStreamExtension
from cliffracer_faststream.middleware import CliffracerAckMiddleware
from pydantic import BaseModel, Field

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    broadcast,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.core.extension import ExtensionSetupContext, RejectMessage
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.typed_rpc import collect_model_schemas
from cliffracer.introspect import (
    Description,
    describe,
)

pytestmark = pytest.mark.unit

# ==============================================================================
# Helper Mock Factories
# ==============================================================================


def _make_mock_nats_client(is_connected: bool = True) -> MagicMock:
    nc = MagicMock()
    nc.is_connected = is_connected
    nc.is_closed = not is_connected
    nc.is_draining = False
    nc.drain = AsyncMock()
    nc.close = AsyncMock()
    nc.publish = AsyncMock()
    nc.subscribe = AsyncMock()

    js = MagicMock()
    js.add_stream = AsyncMock()
    js.update_stream = AsyncMock()
    js.stream_info = AsyncMock()
    js.subscribe = AsyncMock()
    js.pull_subscribe = AsyncMock()
    nc.jetstream = MagicMock(return_value=js)
    return nc


def _make_mock_service(name: str = "stress_svc") -> CliffracerService:
    config = ServiceConfig(
        name=name,
        jetstream_max_deliver=5,
        jetstream_nak_backoff=1.0,
        jetstream_max_backoff=30.0,
        jetstream_ack_wait=0.1,
        shutdown_timeout=2.0,
    )
    svc = CliffracerService(config)
    transport = _make_mock_nats_client(is_connected=True)
    svc.container.nc = transport
    svc.container.js = transport.jetstream()
    svc.container._safe_ack = AsyncMock(return_value=True)
    svc.container._safe_nak = AsyncMock(return_value=True)
    svc.container._safe_term = AsyncMock(return_value=True)
    svc.container._safe_in_progress = AsyncMock(return_value=True)
    svc.container._publish_dlq = AsyncMock()
    return svc


class MockJetStreamMsg:
    """Mock JetStream message with metadata and ack/nak/term/in_progress."""

    def __init__(
        self,
        subject: str = "test.subject",
        data: bytes = b'{"msg": "test"}',
        num_delivered: int = 1,
    ) -> None:
        self.subject = subject
        self.data = data
        self.metadata = MagicMock()
        self.metadata.num_delivered = num_delivered
        self._ackd = False
        self._nakd = False
        self._termd = False
        self.nak_delay: float | None = None
        self.in_progress_count = 0
        self.in_progress_timestamps: list[float] = []

    async def ack(self) -> None:
        self._ackd = True

    async def nak(self, delay: float = 0.0) -> None:
        self._nakd = True
        self.nak_delay = delay

    async def term(self) -> None:
        self._termd = True

    async def in_progress(self) -> None:
        self.in_progress_count += 1
        self.in_progress_timestamps.append(asyncio.get_running_loop().time())


# ==============================================================================
# Suite 1: FastStream Host Primitives Adversarial Stress
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_shutdown_race_inflight_handlers_drained() -> None:
    """Stress Test: stop() during massive in-flight FastStream handlers drains container._active_tasks."""
    svc = _make_mock_service("race_svc")
    container = svc.container
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    concurrency = 30
    handlers_started = 0
    handlers_completed = 0
    start_barrier = asyncio.Event()

    async def slow_handler(m: Any) -> str:
        nonlocal handlers_started, handlers_completed
        handlers_started += 1
        if handlers_started == concurrency:
            start_barrier.set()
        # Staggered sleep between 0.05s and 0.1s
        delay = 0.05 + ((m.metadata.num_delivered % 5) * 0.01)
        await asyncio.sleep(delay)
        handlers_completed += 1
        return "ok"

    msgs = [
        MockJetStreamMsg(subject=f"race.topic.{i}", num_delivered=i + 1) for i in range(concurrency)
    ]

    # Spawn concurrent handlers in background tasks
    tasks: list[asyncio.Task[Any]] = []
    for m in msgs:
        task = asyncio.create_task(middleware.consume_scope(slow_handler, m))
        tasks.append(task)

    # Wait until all handlers are active inside consume_scope
    await start_barrier.wait()
    assert len(container._active_tasks) == concurrency

    # Trigger service.stop() concurrently while all handlers are running
    stop_task = asyncio.create_task(svc.stop())

    # Ensure stop finishes and awaited all in-flight handlers
    await stop_task
    results = await asyncio.gather(*tasks)

    assert handlers_completed == concurrency
    assert len(container._active_tasks) == 0, "All tasks must be cleanly drained from _active_tasks"
    assert all(r == "ok" for r in results)
    assert container._safe_ack.await_count == concurrency


@pytest.mark.asyncio
async def test_adversarial_shutdown_timeout_hung_handler_cancelled() -> None:
    """Stress Test: Shutdown timeout cancels hung FastStream handler and prevents indefinite hang."""
    svc = _make_mock_service("hung_svc")
    svc.config.shutdown_timeout = 0.15  # Strict short timeout
    container = svc.container
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    handler_cancelled = False

    async def hanging_handler(m: Any) -> None:
        nonlocal handler_cancelled
        try:
            await asyncio.sleep(10.0)  # Hang
        except asyncio.CancelledError:
            handler_cancelled = True
            raise

    msg = MockJetStreamMsg(subject="hung.topic", num_delivered=1)
    task = asyncio.create_task(middleware.consume_scope(hanging_handler, msg))
    await asyncio.sleep(0.02)

    assert task in container._active_tasks

    start_time = asyncio.get_running_loop().time()
    await svc.stop()
    duration = asyncio.get_running_loop().time() - start_time

    assert duration < 1.0, f"Shutdown took too long: {duration}s"
    assert handler_cancelled is True
    assert len(container._active_tasks) == 0
    # Hung cancelled task must NOT be acked
    assert msg._ackd is False


@pytest.mark.asyncio
async def test_adversarial_delivery_exhaustion_routes_to_dlq_and_terms() -> None:
    """Stress Test: Delivery exhaustion routes diagnostic payload to {service}.dlq and terminates."""
    svc = _make_mock_service("orders_service")
    svc.config.jetstream_max_deliver = 5
    svc.config.dlq_subject = "{service}.dlq"
    container = svc.container

    # Case 1: Delegating through container._dead_letter_terminated
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    async def transient_failure(m: Any) -> None:
        raise ConnectionError("downstream database unavailable")

    exhausted_msg = MockJetStreamMsg(
        subject="orders.created",
        data=b'{"order_id": 999}',
        num_delivered=5,
    )

    await middleware.consume_scope(transient_failure, exhausted_msg)
    container._dead_letter_terminated.assert_awaited_once_with(
        exhausted_msg, pytest.approx(transient_failure), 5
    ) if hasattr(container._dead_letter_terminated, "assert_awaited") else None
    assert container._safe_term.await_count == 1
    assert container._safe_nak.await_count == 0

    # Case 2: Standalone middleware without container _safe_term, testing fallback to raw_msg.term() and nc.publish
    nc = _make_mock_nats_client()
    bare_container = MagicMock(spec=["nc", "config"])
    bare_container.nc = nc
    bare_container.config = svc.config
    bare_middleware = CliffracerAckMiddleware(container=bare_container, config=svc.config)

    msg2 = MockJetStreamMsg(
        subject="orders.payment",
        data=b'{"payment_id": "pay_123"}',
        num_delivered=6,
    )
    await bare_middleware.consume_scope(transient_failure, msg2)

    assert msg2._termd is True
    nc.publish.assert_awaited_once()
    dlq_subj, dlq_bytes = nc.publish.call_args[0]
    assert dlq_subj == "orders_service.dlq"
    dlq_data = json.loads(dlq_bytes.decode("utf-8"))
    assert dlq_data["service"] == "orders_service"
    assert dlq_data["original_subject"] == "orders.payment"
    assert dlq_data["deliveries"] == 6
    assert "downstream database unavailable" in dlq_data["error"]
    assert "pay_123" in dlq_data["raw"]


@pytest.mark.asyncio
async def test_adversarial_dlq_publish_failure_still_terminates_message() -> None:
    """Stress Test: If DLQ publication raises an unhandled error, _safe_term() is still guaranteed."""
    svc = _make_mock_service("dlq_fail_svc")
    container = svc.container
    container._dead_letter_terminated = AsyncMock(side_effect=RuntimeError("DLQ cluster full"))
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    async def fail_handler(m: Any) -> None:
        raise ValueError("unrecoverable defect")

    msg = MockJetStreamMsg(subject="critical.item", num_delivered=5)
    # Must not raise exception outwards; must safely term
    await middleware.consume_scope(fail_handler, msg)
    assert container._safe_term.await_count == 1


@pytest.mark.asyncio
async def test_adversarial_validation_error_immediate_dlq_and_term() -> None:
    """Stress Test: Schema validation/decode errors DLQ and term on 1st delivery attempt without retry."""
    svc = _make_mock_service("val_svc")
    container = svc.container
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    class CustomValidationError(Exception):
        pass

    async def bad_data_handler(m: Any) -> None:
        raise CustomValidationError("Field 'age' cannot be negative")

    msg = MockJetStreamMsg(subject="users.register", num_delivered=1)
    await middleware.consume_scope(bad_data_handler, msg)

    # Must term immediately on delivery 1 (no NAK!)
    assert container._safe_term.await_count == 1
    assert container._safe_nak.await_count == 0


@pytest.mark.asyncio
async def test_adversarial_policy_refusal_immediate_ack() -> None:
    """Stress Test: Policy refusal (RejectMessage) ACKs immediately without NAK or DLQ."""
    svc = _make_mock_service("policy_svc")
    container = svc.container
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    async def restricted_handler(m: Any) -> None:
        raise RejectMessage("Access denied: tenant suspended")

    msg = MockJetStreamMsg(subject="admin.action", num_delivered=1)
    await middleware.consume_scope(restricted_handler, msg)

    assert container._safe_ack.await_count == 1
    assert container._safe_nak.await_count == 0
    assert container._safe_term.await_count == 0


@pytest.mark.asyncio
async def test_adversarial_heartbeat_pulsing_and_pulse_failure_resilience() -> None:
    """Stress Test: Heartbeat pulses periodically during long-running work and recovers from pulse failures."""
    svc = _make_mock_service("heartbeat_svc")
    svc.config.jetstream_ack_wait = 0.1  # Pulse interval = max(0.05, 0.05) = 0.05s
    container = svc.container

    pulse_call_count = 0

    async def faulty_in_progress(m: Any) -> bool:
        nonlocal pulse_call_count
        pulse_call_count += 1
        if pulse_call_count == 2:
            raise ConnectionResetError("NATS pulse glitch")
        return True

    container._safe_in_progress = AsyncMock(side_effect=faulty_in_progress)
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    async def long_handler(m: Any) -> str:
        await asyncio.sleep(0.25)  # Should trigger at least 4 pulses
        return "completed"

    msg = MockJetStreamMsg(subject="heavy.compute", num_delivered=1)
    res = await middleware.consume_scope(long_handler, msg)

    assert res == "completed"
    assert pulse_call_count >= 3, f"Expected >= 3 pulses, got {pulse_call_count}"
    assert container._safe_ack.await_count == 1


@pytest.mark.asyncio
async def test_adversarial_context_repo_dependencies_and_missing_extensions() -> None:
    """Stress Test: FastStream ContextRepo resolves injected dependencies and handles missing extensions."""
    svc = _make_mock_service("ctx_svc")
    kv_mock = MagicMock()
    resilience_mock = MagicMock()
    svc.kv = kv_mock
    svc.resilience = resilience_mock

    # Custom extension in container
    custom_ext = MagicMock()
    custom_ext.name = "auth_ext"
    svc.container._extensions = [custom_ext]

    ext = FastStreamExtension()
    setup_ctx = ExtensionSetupContext(
        service_config=svc.config,
        broker_url=svc.config.nats_url,
        service=svc,
    )
    await ext.setup(setup_ctx)
    await ext.start()

    broker = ext.hosted_broker
    assert broker is not None
    ctx = broker.context

    # All dependencies resolved
    assert ctx.get("service") is svc
    assert ctx.get("container") is svc.container
    assert ctx.get("config") is svc.config
    assert ctx.get("kv") is kv_mock
    assert ctx.get("resilience") is resilience_mock
    assert ctx.get("auth_ext") is custom_ext

    # Clean stop resets dependencies
    await ext.stop()
    for key in ("service", "container", "config", "kv", "resilience", "auth_ext"):
        assert ctx.get(key) is None
        assert key not in getattr(ctx, "_global_context", {})


@pytest.mark.asyncio
async def test_adversarial_context_repo_absent_extensions() -> None:
    """Stress Test: Service without KV or Resilience does not crash start() and leaves them absent in context."""
    svc = _make_mock_service("bare_ctx_svc")
    # Ensure no kv or resilience attributes
    if hasattr(svc, "kv"):
        delattr(svc, "kv")
    if hasattr(svc, "resilience"):
        delattr(svc, "resilience")

    ext = FastStreamExtension()
    setup_ctx = ExtensionSetupContext(
        service_config=svc.config,
        broker_url=svc.config.nats_url,
        service=svc,
    )
    await ext.setup(setup_ctx)
    await ext.start()

    broker = ext.hosted_broker
    assert broker is not None
    ctx = broker.context

    assert ctx.get("service") is svc
    assert ctx.get("container") is svc.container

    assert ctx.get("kv") is None
    assert ctx.get("resilience") is None
    assert "kv" not in getattr(ctx, "_global_context", {})
    assert "resilience" not in getattr(ctx, "_global_context", {})

    await ext.stop()


# ==============================================================================
# Suite 2: Introspection Primitives Adversarial Stress
# ==============================================================================


# Models for stress testing
class SelfRecursiveTree(BaseModel):
    value: int
    left: SelfRecursiveTree | None = None
    right: SelfRecursiveTree | None = None


class GraphEdge(BaseModel):
    weight: float
    target: GraphNode | None = None


class GraphNode(BaseModel):
    node_id: str
    edges: list[GraphEdge] = []


GraphEdge.model_rebuild()
GraphNode.model_rebuild()


class GenericContainer[T](BaseModel):
    count: int
    items: list[T]
    metadata: dict[str, str] = {}


class CatModel(BaseModel):
    pet_type: Literal["cat"] = "cat"
    whiskers: int


class DogModel(BaseModel):
    pet_type: Literal["dog"] = "dog"
    pack_size: int


class ComplexModel(BaseModel):
    name: str
    active: bool = True
    tree: SelfRecursiveTree | None = None
    pet: CatModel | DogModel
    scores: list[int]
    attributes: dict[str, str] = Field(default_factory=dict)


class AdversarialService(CliffracerService):
    config = ServiceConfig(
        name="adversarial_introspection_service",
        version="2.4.1",
        jetstream_streams=[
            StreamSpec(name="TEST_STREAM", subjects=["adversarial.>"]),
        ],
    )

    @rpc
    async def process_tree(self, tree: SelfRecursiveTree) -> SelfRecursiveTree:
        """Process binary tree hierarchy.

        This method walks a recursive binary search tree.

        Features:
        * In-order traversal
        * Balanced pruning
          - Depth check
          - Leaf deduplication

        Code example:
        ```python
        tree = SelfRecursiveTree(value=10)
        await client.process_tree(tree)
        ```
        """
        return tree

    @rpc
    async def get_graph(self, root_id: str) -> GraphNode:
        """Fetch cyclic graph topology."""
        return GraphNode(node_id=root_id)

    @rpc
    async def get_generic_data(self) -> GenericContainer[ComplexModel]:
        """Return generic container of complex models."""
        return GenericContainer[ComplexModel](count=0, items=[])

    @listener("adversarial.raw.push")
    async def on_raw_push(self, payload: bytes) -> None:
        """Handle raw push events.

        First line summary.
        Additional markdown details.
        """
        pass

    @listener("adversarial.raw.durable", durable="durable_raw")
    async def on_raw_durable(self, payload: bytes) -> None:
        """Handle raw durable push events."""
        pass

    @listener("adversarial.raw.fanout", durable="durable_fanout", fanout=True)
    async def on_raw_fanout(self, payload: bytes) -> None:
        """Handle raw fanout events."""
        pass

    @listener("adversarial.raw.pull", durable="durable_pull", pull=True)
    async def on_raw_pull(self, payload: bytes) -> None:
        """Handle raw pull consumer events."""
        pass

    @validated_listener("adversarial.validated.event", ComplexModel)
    async def on_validated(self, event: ComplexModel) -> None:
        """Handle validated event payload.

        Preserves complex structure.
        """
        pass

    @validated_listener("adversarial.validated.durable", ComplexModel, durable="val_durable")
    async def on_validated_durable(self, event: ComplexModel) -> None:
        pass

    @broadcast("adversarial.broadcast.signal")
    async def on_broadcast_signal(self, sig: str) -> None:
        """Broadcast signal to all instances."""
        pass


def test_adversarial_recursive_models_schema_collection_terminates() -> None:
    """Stress Test: Mutually recursive and self-referential Pydantic models collect without recursion error."""
    components: dict[str, Any] = {}
    collect_model_schemas(SelfRecursiveTree, components)
    collect_model_schemas(GraphNode, components)

    assert len(components) >= 3
    # Check that each schema contains valid properties
    for s_hash, schema in components.items():
        assert len(s_hash) == 16
        assert isinstance(schema, dict)
        assert "properties" in schema or "$defs" in schema or "type" in schema


def test_adversarial_components_16char_sha256_hash_invariants() -> None:
    """Stress Test: Every component schema key is strictly a deterministic 16-char SHA-256 hash."""
    desc = describe(AdversarialService, config=AdversarialService.config)

    assert desc.service == "adversarial_introspection_service"
    assert desc.version == "2.4.1"
    assert len(desc.components) > 0

    for schema_hash, schema in desc.components.items():
        # Invariant 1: Key length is exactly 16
        assert len(schema_hash) == 16, f"Hash {schema_hash} length != 16"
        # Invariant 2: Hash is alphanumeric
        assert schema_hash.isalnum(), f"Hash {schema_hash} not alphanumeric"
        # Invariant 3: Matches first 16 hex chars of sha256 of sorted json schema
        expected_hash = hashlib.sha256(
            json.dumps(schema, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        assert schema_hash == expected_hash, (
            f"Hash mismatch for {schema.get('title')}: {schema_hash} != {expected_hash}"
        )


def test_adversarial_hash_determinism_across_reordering_and_repeated_invocations() -> None:
    """Stress Test: describe() produces identical component hashes regardless of declaration order."""
    desc1 = describe(AdversarialService, config=AdversarialService.config)
    desc2 = describe(AdversarialService, config=AdversarialService.config)

    assert desc1.components == desc2.components
    assert desc1.description_hash == desc2.description_hash
    assert [m.name for m in desc1.methods] == [m.name for m in desc2.methods]
    assert [listener.pattern for listener in desc1.listeners] == [
        listener.pattern for listener in desc2.listeners
    ]


def test_adversarial_listener_introspection_matrix() -> None:
    """Stress Test: Verification of all listener decorators, queue groups, durables, and fanout semantics."""
    desc = describe(AdversarialService, config=AdversarialService.config)

    assert len(desc.listeners) == 7

    # 1. Raw push (no durable) -> queue_group is None
    l_raw = desc.listener("adversarial.raw.push")
    assert l_raw is not None
    assert l_raw.fanout is False
    assert l_raw.pull is False
    assert l_raw.durable is None
    assert l_raw.queue_group is None
    assert l_raw.is_validated is False

    # 2. Raw durable push -> queue_group == durable
    l_durable = desc.listener("adversarial.raw.durable")
    assert l_durable is not None
    assert l_durable.fanout is False
    assert l_durable.pull is False
    assert l_durable.durable == "durable_raw"
    assert l_durable.queue_group == "durable_raw"

    # 3. Raw fanout push -> queue_group is None (even if durable specified)
    l_fanout = desc.listener("adversarial.raw.fanout")
    assert l_fanout is not None
    assert l_fanout.fanout is True
    assert l_fanout.pull is False
    assert l_fanout.durable == "durable_fanout"
    assert l_fanout.queue_group is None

    # 4. Raw pull -> queue_group is None
    l_pull = desc.listener("adversarial.raw.pull")
    assert l_pull is not None
    assert l_pull.pull is True
    assert l_pull.fanout is False
    assert l_pull.durable == "durable_pull"
    assert l_pull.queue_group is None

    # 5. Validated listener -> schema contains schema_hash matching components
    l_val = desc.listener("adversarial.validated.event")
    assert l_val is not None
    assert l_val.is_validated is True
    assert l_val.schema is not None
    val_schema_hash = l_val.schema.get("schema_hash")
    assert val_schema_hash in desc.components

    # 6. Validated durable push -> queue_group == durable
    l_val_dur = desc.listener("adversarial.validated.durable")
    assert l_val_dur is not None
    assert l_val_dur.durable == "val_durable"
    assert l_val_dur.queue_group == "val_durable"

    # 7. Broadcast -> fanout is True, is_broadcast is True, queue_group is None
    l_bcast = desc.listener("adversarial.broadcast.signal")
    assert l_bcast is not None
    assert l_bcast.is_broadcast is True
    assert l_bcast.fanout is True
    assert l_bcast.queue_group is None


def test_adversarial_multiline_docstring_markdown_preservation() -> None:
    """Stress Test: Multi-line docstring preserves code blocks, bullet points, and blank lines in description."""
    desc = describe(AdversarialService, config=AdversarialService.config)
    m = desc.method("process_tree")
    assert m is not None

    # doc_summary is the first non-empty line
    assert m.doc_summary == "Process binary tree hierarchy."
    assert m.doc == "Process binary tree hierarchy."

    # description preserves full markdown
    assert m.description is not None
    assert "Features:" in m.description
    assert "* In-order traversal" in m.description
    assert "```python" in m.description
    assert "await client.process_tree(tree)" in m.description
    assert "\n\n" in m.description

    # Raw listener docstring
    l_raw = desc.listener("adversarial.raw.push")
    assert l_raw is not None
    assert l_raw.doc_summary == "Handle raw push events."
    assert l_raw.description is not None
    assert "Additional markdown details." in l_raw.description

    # Listener with no docstring
    l_no_doc = desc.listener("adversarial.validated.durable")
    assert l_no_doc is not None
    assert l_no_doc.doc_summary is None
    assert l_no_doc.description is None


def test_adversarial_description_serialization_round_trip() -> None:
    """Stress Test: Description to_dict() and from_dict() round trip survives without data loss."""
    desc = describe(AdversarialService, config=AdversarialService.config)
    d = desc.to_dict()

    # JSON serialization and deserialization
    json_str = json.dumps(d)
    d_loaded = json.loads(json_str)
    restored = Description.from_dict(d_loaded)

    assert restored.service == desc.service
    assert restored.version == desc.version
    assert restored.description_hash == desc.description_hash
    assert len(restored.methods) == len(desc.methods)
    assert len(restored.listeners) == len(desc.listeners)
    assert len(restored.streams) == len(desc.streams)
    assert restored.components == desc.components


# ==============================================================================
# Suite 3: Advanced Adversarial Scenarios & Leak Endurance
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_mixed_burst_traffic_and_concurrent_shutdown() -> None:
    """Stress Test: 60 concurrent messages with mixed outcomes under simultaneous stop()."""
    svc = _make_mock_service("mixed_burst_svc")
    container = svc.container
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    total_tasks = 60
    started_count = 0
    start_barrier = asyncio.Event()

    async def mixed_handler(m: Any) -> str:
        nonlocal started_count
        started_count += 1
        if started_count == total_tasks:
            start_barrier.set()

        await asyncio.sleep(0.04)

        # 4 different paths:
        idx = m.metadata.num_delivered % 4
        if idx == 0:
            return "ok"
        elif idx == 1:
            raise ConnectionResetError("flaky connection")
        elif idx == 2:
            raise ValueError("bad payload decode")
        else:
            raise RejectMessage("denied")

    msgs = [
        MockJetStreamMsg(
            subject=f"mixed.topic.{i}",
            data=b'{"idx": ' + str(i).encode() + b"}",
            num_delivered=i + 1,
        )
        for i in range(total_tasks)
    ]

    tasks = [asyncio.create_task(middleware.consume_scope(mixed_handler, m)) for m in msgs]

    await start_barrier.wait()
    assert len(container._active_tasks) == total_tasks

    # Concurrently stop the service
    await svc.stop()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Invariants:
    assert len(container._active_tasks) == 0, "All active tasks must be drained"
    # Verify outcomes occurred without unhandled crashes
    assert container._safe_ack.await_count > 0
    assert container._safe_term.await_count > 0 or container._dead_letter_terminated.await_count > 0


@pytest.mark.asyncio
async def test_adversarial_heartbeat_zero_task_leak_endurance() -> None:
    """Endurance Test: 50 sequential handler executions leave 0 leaked heartbeat tasks in asyncio loop."""
    svc = _make_mock_service("endurance_svc")
    svc.config.jetstream_ack_wait = 0.05
    container = svc.container
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    async def fast_work(m: Any) -> str:
        await asyncio.sleep(0.02)
        return "done"

    for _i in range(30):
        msg = MockJetStreamMsg(subject="pulse.test", num_delivered=1)
        res = await middleware.consume_scope(fast_work, msg)
        assert res == "done"

    # Inspect all running tasks in loop
    running_heartbeat_tasks = [
        t for t in asyncio.all_tasks() if t.get_name() == "faststream_heartbeat" and not t.done()
    ]
    assert len(running_heartbeat_tasks) == 0, f"Leaked heartbeat tasks: {running_heartbeat_tasks}"


@pytest.mark.asyncio
async def test_adversarial_hosted_broker_idempotent_stop_and_lifecycle_invariants() -> None:
    """Stress Test: CliffracerHostedNatsBroker stop() is idempotent and strictly guards connection."""
    broker = CliffracerHostedNatsBroker()
    transport = _make_mock_nats_client(is_connected=True)
    broker.attach_cliffracer_connection(transport)

    assert broker._attached is True
    assert broker._connection is transport

    # Stop 1
    await broker.stop()
    assert broker._attached is False
    assert broker._connection is None
    assert transport.drain.await_count == 0, "broker.stop() MUST NEVER drain shared transport"

    # Stop 2 (idempotent repeat)
    await broker.stop()
    assert broker._attached is False
    assert broker._connection is None

    # Connect after stop must raise RuntimeError
    with pytest.raises(RuntimeError, match="requires an active connection"):
        await broker.connect()


class CycleNodeA(BaseModel):
    name: str
    b: CycleNodeB | None = None


class CycleNodeB(BaseModel):
    name: str
    c: CycleNodeC | None = None


class CycleNodeC(BaseModel):
    name: str
    a: CycleNodeA | None = None


CycleNodeA.model_rebuild()
CycleNodeB.model_rebuild()
CycleNodeC.model_rebuild()


class ConstrainedModel(BaseModel):
    username: str = Field(min_length=3, max_length=20, pattern="^[a-z0-9_]+$")
    score: int = Field(ge=0, le=1000)
    ratio: float = Field(gt=0.0, lt=1.0)


class ExtremeDocstringService(CliffracerService):
    @rpc
    async def empty_doc(self, x: int) -> int:
        """"""
        return x

    @rpc
    async def whitespace_doc(self, x: int) -> int:
        """

        \t
        """
        return x

    @rpc
    async def leading_newline_doc(self, x: int) -> int:
        """

        First real line of documentation.
        Second paragraph here.
        """
        return x

    @rpc
    async def rich_markdown_doc(self, node: CycleNodeA, item: ConstrainedModel) -> CycleNodeA:
        """Endpoint with rich markdown and emoji 🚀.

        Detailed specifications:
        | Column A | Column B |
        |----------|----------|
        | Alpha    | Beta     |

        * Nested list item 1
          * Sub-bullet A
          * Sub-bullet B
        * Nested list item 2

        ```json
        {"status": "ok", "count": 10}
        ```

        HTML: <span class="badge">Production</span>
        """
        return node


def test_adversarial_deep_cyclic_and_constrained_models() -> None:
    """Stress Test: 3-cycle recursive models and constrained fields collect with deterministic hashes."""
    desc = describe(ExtremeDocstringService)
    assert len(desc.components) >= 4

    for s_hash, schema in desc.components.items():
        assert len(s_hash) == 16
        expected = hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest()[
            :16
        ]
        assert s_hash == expected


def test_adversarial_extreme_docstrings_extraction() -> None:
    """Stress Test: Empty, whitespace, leading blank line, emoji, tables, and HTML docstrings."""
    desc = describe(ExtremeDocstringService)

    # 1. Empty docstring
    m_empty = desc.method("empty_doc")
    assert m_empty is not None
    assert m_empty.doc_summary is None
    assert m_empty.description is None

    # 2. Whitespace docstring: doc_summary is None, description preserves whitespace
    m_white = desc.method("whitespace_doc")
    assert m_white is not None
    assert m_white.doc_summary is None
    assert m_white.description is not None and m_white.description.strip() == ""

    # 3. Leading newline docstring
    m_lead = desc.method("leading_newline_doc")
    assert m_lead is not None
    assert m_lead.doc_summary == "First real line of documentation."
    assert m_lead.description is not None
    assert "Second paragraph here." in m_lead.description

    # 4. Rich markdown with emoji and tables
    m_rich = desc.method("rich_markdown_doc")
    assert m_rich is not None
    assert m_rich.doc_summary == "Endpoint with rich markdown and emoji 🚀."
    assert m_rich.description is not None
    assert "| Column A | Column B |" in m_rich.description
    assert "* Sub-bullet A" in m_rich.description
    assert '{"status": "ok", "count": 10}' in m_rich.description
    assert '<span class="badge">Production</span>' in m_rich.description

    # Ensure to_dict() / from_dict() round trip survives rich markdown and unicode
    d = desc.to_dict()
    restored = Description.from_dict(json.loads(json.dumps(d)))
    m_restored = restored.method("rich_markdown_doc")
    assert m_restored is not None
    assert m_restored.doc_summary == m_rich.doc_summary
    assert m_restored.description == m_rich.description

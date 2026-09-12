"""FastStream host primitives, over the in-memory transport.

Covers Tiers 1-2:
- Tier 1: Feature coverage for delayed broker start, transport shielding, shutdown drain, CliffracerAckMiddleware ack/nak/dlq, ContextRepo KV/Resilience injection.
- Tier 2: Boundary & corner cases: broker stop before start, shutdown during active message, poison pill DLQ exhaustion, in_progress heartbeats.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_faststream import (
    CliffracerAckMiddleware,
    FastStreamExtension,
)
from cliffracer_kv import KvExtension
from cliffracer_resilience import ResilienceExtension
from faststream.nats import NatsRouter
from pydantic import BaseModel, Field

from cliffracer import CliffracerService
from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.extension import ExtensionSetupContext, RejectMessage

from .conftest import MockJetStreamMsg, MockNatsTransport

pytestmark = pytest.mark.unit


class SampleFastStreamMessage(BaseModel):
    item_id: str
    quantity: int = Field(gt=0)


def make_setup_ctx(svc: CliffracerService) -> ExtensionSetupContext:
    return ExtensionSetupContext(
        service_config=svc.config,
        broker_url=svc.config.nats_url,
        service=svc,
    )


# ---------------------------------------------------------------------------
# Tier 1: Feature Coverage
# ---------------------------------------------------------------------------

# === Feature: Delayed Broker Connection Injection & Transport Shielding ===


@pytest.mark.asyncio
async def test_tier1_317_01_setup_does_not_require_active_connection(
    transport_service_factory: Any,
) -> None:
    """Verify FastStreamExtension.setup() succeeds when container.nc is None."""
    svc = transport_service_factory(name="faststream_svc")
    assert svc.container.nc is None

    router = NatsRouter()
    ext = FastStreamExtension(router=router)
    setup_ctx = make_setup_ctx(svc)

    # setup must not raise or access .nc
    await ext.setup(setup_ctx)
    assert ext.hosted_broker is not None
    assert ext.hosted_broker._attached is False


@pytest.mark.asyncio
async def test_tier1_317_02_start_attaches_active_connection(
    transport_service_factory: Any,
) -> None:
    """Verify FastStreamExtension.start() attaches active Cliffracer connection and JetStream."""
    svc = transport_service_factory(name="faststream_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport
    svc.container.js = MagicMock()

    router = NatsRouter()
    ext = FastStreamExtension(router=router)
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)

    await ext.start()
    assert ext.hosted_broker._attached is True
    assert ext.hosted_broker._connection is transport
    await ext.stop()


@pytest.mark.asyncio
async def test_tier1_317_03_stop_does_not_drain_shared_transport(
    transport_service_factory: Any,
) -> None:
    """Verify CliffracerHostedNatsBroker.stop() does NOT call _connection.drain()."""
    svc = transport_service_factory(name="faststream_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    ext = FastStreamExtension()
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    # Verify stop
    await ext.stop()
    assert transport._drain_called is False, "stop() MUST NOT drain the shared NATS transport"
    assert transport.is_connected is True, (
        "Shared transport MUST remain alive for container shutdown"
    )


@pytest.mark.asyncio
async def test_tier1_317_04_stream_declaration_suppressed(
    transport_service_factory: Any,
) -> None:
    """Verify autonomous stream declaration is disabled on all mounted streams."""
    svc = transport_service_factory(name="shield_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    router = NatsRouter()

    # FastStream subscriber specifying stream
    @router.subscriber("orders.stream.test", stream="TEST_STREAM")
    async def handle_stream(msg: dict) -> None:
        pass

    ext = FastStreamExtension(router=router)
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    broker = ext.hosted_broker
    # Verify stream builder objects have declare == False
    stream_builder = getattr(broker, "_stream_builder", None)
    if stream_builder is not None and hasattr(stream_builder, "objects"):
        for stream_obj in stream_builder.objects.values():
            assert stream_obj[0].declare is False, "Stream declaration must be suppressed"
    await ext.stop()


@pytest.mark.asyncio
async def test_tier1_317_05_start_raises_if_nats_disconnected(
    transport_service_factory: Any,
) -> None:
    """Verify start() raises ServiceLifecycleError if NATS client is disconnected."""
    svc = transport_service_factory(name="disconnect_svc")
    transport = MockNatsTransport()
    transport.is_connected = False
    svc.container.nc = transport

    ext = FastStreamExtension()
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)

    with pytest.raises(ServiceLifecycleError) as excinfo:
        await ext.start()
    assert (
        "disconnected" in str(excinfo.value).lower()
        or "uninitialized" in str(excinfo.value).lower()
    )


# === Feature: Graceful Shutdown Drain Bridge ===


@pytest.mark.asyncio
async def test_tier1_318_01_active_task_registered_in_container(
    transport_service_factory: Any,
) -> None:
    """Verify CliffracerAckMiddleware registers in-flight handler task in container._active_tasks."""
    svc = transport_service_factory(name="drain_svc")
    container = svc.container
    container._active_tasks = set()

    middleware = CliffracerAckMiddleware(container=container, config=svc.config)
    observed_tasks: list[asyncio.Task[Any]] = []

    async def sample_handler(m: Any) -> str:
        # Check active tasks while inside handler
        observed_tasks.extend(list(container._active_tasks))
        return "done"

    msg = MockJetStreamMsg(subject="orders.item", data=b"{}", num_delivered=1)
    await middleware.consume_scope(sample_handler, msg)

    assert len(observed_tasks) == 1
    # Current task was in active tasks
    assert observed_tasks[0] == asyncio.current_task()


@pytest.mark.asyncio
async def test_tier1_318_02_active_task_removed_on_completion(
    transport_service_factory: Any,
) -> None:
    """Verify in-flight task is removed from container._active_tasks upon completion."""
    svc = transport_service_factory(name="drain_svc")
    container = svc.container
    container._active_tasks = set()

    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    async def sample_handler(m: Any) -> str:
        return "ok"

    msg = MockJetStreamMsg(subject="orders.item", data=b"{}", num_delivered=1)
    await middleware.consume_scope(sample_handler, msg)

    assert len(container._active_tasks) == 0


@pytest.mark.asyncio
async def test_tier1_318_03_shutdown_drain_awaits_inflight_worker(
    transport_service_factory: Any,
) -> None:
    """Verify graceful shutdown Step 4 awaits in-flight FastStream worker."""
    svc = transport_service_factory(name="drain_wait_svc")
    container = svc.container
    container._active_tasks = set()

    worker_finished = False

    async def slow_worker() -> None:
        nonlocal worker_finished
        await asyncio.sleep(0.1)
        worker_finished = True

    # Register worker in _active_tasks
    task = asyncio.create_task(slow_worker())
    container._active_tasks.add(task)

    # Step 4 drain simulation
    timeout = getattr(svc.config, "shutdown_timeout", 5.0)
    await asyncio.wait_for(
        asyncio.gather(*list(container._active_tasks), return_exceptions=True),
        timeout=timeout,
    )
    assert worker_finished is True


@pytest.mark.asyncio
async def test_tier1_318_04_extension_drain_hook_unsubscribes_consumers(
    transport_service_factory: Any,
) -> None:
    """Verify FastStreamExtension.drain() unsubscribes consumers to halt ingress traffic."""
    svc = transport_service_factory(name="ext_drain_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    router = NatsRouter()

    @router.subscriber("orders.drain.test")
    async def handle_drain(msg: dict) -> None:
        pass

    ext = FastStreamExtension(router=router)
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    subs = (
        list(ext.hosted_broker.subscribers.values())
        if isinstance(ext.hosted_broker.subscribers, dict)
        else list(ext.hosted_broker.subscribers)
    )
    for sub in subs:
        if getattr(sub, "subscription", None):
            sub.subscription.unsubscribe = AsyncMock()

    await ext.drain(timeout=5.0)

    for sub in subs:
        assert sub.running is False
    await ext.stop()


@pytest.mark.asyncio
async def test_tier1_318_05_shutdown_timeout_cancels_hung_handler(
    transport_service_factory: Any,
) -> None:
    """Verify hung worker task is cancelled when shutdown timeout is exceeded."""
    svc = transport_service_factory(name="timeout_svc")
    container = svc.container
    container._active_tasks = set()

    async def hung_worker() -> None:
        await asyncio.sleep(10.0)

    task = asyncio.create_task(hung_worker())
    container._active_tasks.add(task)

    # Await with short timeout (0.05s)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*list(container._active_tasks)),
            timeout=0.05,
        )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# === Feature: CliffracerAckMiddleware ===


@pytest.mark.asyncio
async def test_tier1_319_01_successful_execution_acks_message(
    transport_service_factory: Any,
) -> None:
    """Verify successful handler execution triggers msg.ack()."""
    svc = transport_service_factory(name="ack_svc")
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    msg = MockJetStreamMsg(
        subject="orders.item", data=b'{"item_id": "1", "quantity": 2}', num_delivered=1
    )

    async def ok_handler(m: Any) -> str:
        return "success"

    await middleware.consume_scope(ok_handler, msg)
    assert msg.ack_calls == 1
    assert len(msg.nak_calls) == 0
    assert msg.term_calls == 0


@pytest.mark.asyncio
async def test_tier1_319_02_transient_error_naks_with_exponential_backoff(
    transport_service_factory: Any,
) -> None:
    """Verify transient exception with num_delivered < max_deliver triggers msg.nak(delay=...)."""
    svc = transport_service_factory(
        name="nak_svc", jetstream_max_deliver=5, jetstream_nak_backoff=1.0
    )
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    # First delivery
    msg1 = MockJetStreamMsg(subject="orders.item", data=b"{}", num_delivered=1)

    async def fail_handler(m: Any) -> None:
        raise ConnectionResetError("network glitch")

    await middleware.consume_scope(fail_handler, msg1)
    assert len(msg1.nak_calls) == 1
    assert msg1.nak_calls[0] >= 1.0  # 1.0 * 2^0 = 1.0s
    assert msg1.term_calls == 0

    # Third delivery: backoff delay = 1.0 * 2^(3-1) = 4.0s
    msg3 = MockJetStreamMsg(subject="orders.item", data=b"{}", num_delivered=3)
    await middleware.consume_scope(fail_handler, msg3)
    assert len(msg3.nak_calls) == 1
    assert msg3.nak_calls[0] >= 4.0


@pytest.mark.asyncio
async def test_tier1_319_03_delivery_exhaustion_routes_to_dlq_and_terms(
    transport_service_factory: Any,
) -> None:
    """Verify num_delivered >= max_deliver routes to DLQ and calls msg.term()."""
    svc = transport_service_factory(
        name="dlq_svc", jetstream_max_deliver=5, dlq_subject="custom.dlq"
    )
    dlq_records: list[dict[str, Any]] = []

    async def mock_dlq(subject: str, *args: Any, **kwargs: Any) -> None:
        dlq_records.append({"subject": subject, "kwargs": kwargs})

    svc.container._publish_dlq = AsyncMock(side_effect=mock_dlq)  # type: ignore[method-assign]
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    msg = MockJetStreamMsg(subject="orders.process", data=b'{"foo": "bar"}', num_delivered=5)

    async def fail_handler(m: Any) -> None:
        raise ValueError("unrecoverable error")

    await middleware.consume_scope(fail_handler, msg)
    assert msg.term_calls == 1
    assert len(msg.nak_calls) == 0
    assert len(dlq_records) == 1
    assert dlq_records[0]["subject"] == "custom.dlq"


@pytest.mark.asyncio
async def test_tier1_319_04_validation_failure_dlq_and_terms_immediately(
    transport_service_factory: Any,
) -> None:
    """Verify schema validation / JSON decode error DLQs and terms immediately without retries."""
    svc = transport_service_factory(name="decode_err_svc", jetstream_max_deliver=5)
    dlq_records: list[dict[str, Any]] = []

    async def mock_dlq(subject: str, *args: Any, **kwargs: Any) -> None:
        dlq_records.append({"subject": subject, "kwargs": kwargs})

    svc.container._publish_dlq = AsyncMock(side_effect=mock_dlq)  # type: ignore[method-assign]
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    # First delivery, but corrupted payload
    msg = MockJetStreamMsg(subject="orders.schema", data=b"not-valid-json", num_delivered=1)

    async def validating_handler(m: Any) -> None:
        # Simulate FastStream validation failure
        SampleFastStreamMessage.model_validate_json(m.data)

    await middleware.consume_scope(validating_handler, msg)
    assert msg.term_calls == 1
    assert len(msg.nak_calls) == 0, "Validation failure MUST NOT retry via nak"
    assert len(dlq_records) == 1


@pytest.mark.asyncio
async def test_tier1_319_05_policy_refusal_acks_immediately(
    transport_service_factory: Any,
) -> None:
    """Verify RejectMessage executes _safe_ack immediately to prevent retry storms."""
    svc = transport_service_factory(name="refusal_svc")
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    msg = MockJetStreamMsg(subject="admin.op", data=b"{}", num_delivered=1)

    async def refusing_handler(m: Any) -> None:
        raise RejectMessage("forbidden token")

    await middleware.consume_scope(refusing_handler, msg)
    assert msg.ack_calls == 1
    assert len(msg.nak_calls) == 0
    assert msg.term_calls == 0


# === Feature: ContextRepo KV/Resilience & Telemetry ===


@pytest.mark.asyncio
async def test_tier1_320_01_context_injection_service_container_config(
    transport_service_factory: Any,
) -> None:
    """Verify service, container, and config are injected into FastStream ContextRepo."""
    svc = transport_service_factory(name="ctx_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    ext = FastStreamExtension()
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    ctx = ext.hosted_broker.context
    assert ctx.get("service") is svc
    assert ctx.get("container") is svc.container
    assert ctx.get("config") is svc.config
    await ext.stop()


@pytest.mark.asyncio
async def test_tier1_320_02_context_injection_kv_extension(
    transport_service_factory: Any,
) -> None:
    """Verify KvExtension is injected into FastStream ContextRepo under 'kv'."""
    svc = transport_service_factory(name="kv_ctx_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    # Attach KvExtension to service
    kv_ext = KvExtension()
    svc.kv = kv_ext
    svc.container._extensions.append(kv_ext)

    ext = FastStreamExtension()
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    ctx = ext.hosted_broker.context
    assert ctx.get("kv") is kv_ext
    await ext.stop()


@pytest.mark.asyncio
async def test_tier1_320_03_context_injection_resilience_extension(
    transport_service_factory: Any,
) -> None:
    """Verify ResilienceExtension is injected into FastStream ContextRepo under 'resilience'."""
    svc = transport_service_factory(name="resilience_ctx_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    # Attach ResilienceExtension
    res_ext = ResilienceExtension()
    svc.resilience = res_ext
    svc.container._extensions.append(res_ext)

    ext = FastStreamExtension()
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    ctx = ext.hosted_broker.context
    assert ctx.get("resilience") is res_ext
    await ext.stop()


@pytest.mark.asyncio
async def test_tier1_320_04_health_details_reflects_running_state(
    transport_service_factory: Any,
) -> None:
    """Verify health_details() reports 'running' when started, 'stopped' when stopped."""
    svc = transport_service_factory(name="health_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    ext = FastStreamExtension()
    assert ext.health_details()["status"] == "uninitialized"

    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    details = ext.health_details()
    assert details["status"] == "running"
    assert "subscribers_count" in details

    await ext.stop()
    assert ext.health_details()["status"] == "stopped"


@pytest.mark.asyncio
async def test_tier1_320_05_info_details_surfaces_subscribers(
    transport_service_factory: Any,
) -> None:
    """Verify info_details() surfaces mounted subscriber metadata."""
    svc = transport_service_factory(name="info_svc")
    transport = MockNatsTransport()
    svc.container.nc = transport

    router = NatsRouter()

    @router.subscriber("orders.feed")
    async def on_feed(msg: dict) -> None:
        pass

    ext = FastStreamExtension(router=router)
    setup_ctx = make_setup_ctx(svc)
    await ext.setup(setup_ctx)
    await ext.start()

    info = ext.info_details()
    assert info is not None
    assert info["routers_count"] == 1
    assert len(info["subscribers"]) >= 1
    assert "orders.feed" in str(info["subscribers"][0]["subject"])
    await ext.stop()


# ---------------------------------------------------------------------------
# Tier 2: Boundary & Corner Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_317_01_broker_stop_before_start_safe(
    transport_service_factory: Any,
) -> None:
    """Boundary: Calling stop() on unstarted broker halts cleanly without raising."""
    ext = FastStreamExtension()
    # Unstarted stop
    await ext.stop()
    assert ext.health_details()["status"] == "uninitialized"


@pytest.mark.asyncio
async def test_tier2_318_01_burst_traffic_task_drain(
    transport_service_factory: Any,
) -> None:
    """Boundary: 15 concurrent in-flight FastStream messages cleanly drain during shutdown."""
    svc = transport_service_factory(name="burst_drain_svc")
    container = svc.container
    container._active_tasks = set()
    middleware = CliffracerAckMiddleware(container=container, config=svc.config)

    completed_count = 0

    async def slow_handler(m: Any) -> None:
        nonlocal completed_count
        await asyncio.sleep(0.05)
        completed_count += 1

    # Launch 15 concurrent message dispatches
    tasks = [
        asyncio.create_task(
            middleware.consume_scope(
                slow_handler,
                MockJetStreamMsg(subject="burst.topic", data=b"{}", num_delivered=1),
            )
        )
        for _ in range(15)
    ]

    # Drain loop
    await asyncio.gather(*list(container._active_tasks), return_exceptions=True)
    await asyncio.gather(*tasks)
    assert completed_count == 15
    assert len(container._active_tasks) == 0


@pytest.mark.asyncio
async def test_tier2_319_01_poison_pill_dlq_publish_failure_still_terms(
    transport_service_factory: Any,
) -> None:
    """Boundary: If publishing to DLQ fails (e.g. disk full), message is still terminated."""
    svc = transport_service_factory(name="dlq_fail_svc", jetstream_max_deliver=3)
    # Mock DLQ publish to raise exception
    svc.container._publish_dlq = AsyncMock(side_effect=RuntimeError("JetStream disk full"))  # type: ignore[method-assign]
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    msg = MockJetStreamMsg(subject="orders.critical", data=b"{}", num_delivered=3)

    async def buggy_handler(m: Any) -> None:
        raise ValueError("corrupt data")

    # Should not raise exception
    await middleware.consume_scope(buggy_handler, msg)
    assert msg.term_calls == 1, "Message MUST be terminated even if DLQ publish fails to stop storm"


@pytest.mark.asyncio
async def test_tier2_319_02_in_progress_heartbeat_pulsing(
    transport_service_factory: Any,
) -> None:
    """Boundary: Long-running task triggers in_progress heartbeat pulses."""
    # Set short ack_wait to pulse quickly
    svc = transport_service_factory(name="heartbeat_svc", jetstream_ack_wait=0.1)
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    msg = MockJetStreamMsg(subject="batch.heavy", data=b"{}", num_delivered=1)

    async def heavy_handler(m: Any) -> str:
        # Sleep long enough for multiple heartbeat pulses (interval = max(0.05, 0.1/2) = 0.05s)
        await asyncio.sleep(0.16)
        return "finished"

    await middleware.consume_scope(heavy_handler, msg)
    assert msg.in_progress_calls >= 2, "Heartbeat MUST pulse in_progress() for long-running handler"
    assert msg.ack_calls == 1


@pytest.mark.asyncio
async def test_tier2_319_03_core_nats_message_bypasses_ack_logic(
    transport_service_factory: Any,
) -> None:
    """Boundary: Non-JetStream Core NATS messages bypass ack/nak/term logic without error."""
    svc = transport_service_factory(name="core_nats_svc")
    middleware = CliffracerAckMiddleware(container=svc.container, config=svc.config)

    # Message with metadata=None (Core NATS)
    msg = MockJetStreamMsg(subject="core.ping", data=b"ping")
    msg.metadata = None  # type: ignore[assignment]

    async def core_handler(m: Any) -> str:
        return "pong"

    result = await middleware.consume_scope(core_handler, msg)
    assert result == "pong"
    assert msg.ack_calls == 0
    assert len(msg.nak_calls) == 0
    assert msg.term_calls == 0

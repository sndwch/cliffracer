"""Unit tests for the Cliffracer FastStream host extension (#317, #318, #319, #320)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_faststream import (
    CliffracerAckMiddleware,
    CliffracerHostedNatsBroker,
    FastStreamExtension,
)
from faststream.exceptions import RejectMessage as FsRejectMessage
from faststream.middlewares import AckPolicy
from faststream.nats import JStream, NatsRouter

from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.extension import ExtensionSetupContext, SharedDependency
from cliffracer.core.extension import RejectMessage as CliffracerRejectMessage
from cliffracer.core.service_config import ServiceConfig


def _make_mock_nats_client(is_connected: bool = True) -> MagicMock:
    """Create a mock NATS client compatible with FastStream and Cliffracer."""
    nc = MagicMock()
    nc.is_connected = is_connected
    nc.is_closed = False
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


def _make_mock_service(name: str = "test_svc") -> MagicMock:
    """Create a mock Cliffracer service."""
    service = MagicMock()
    config = ServiceConfig(
        name=name,
        jetstream_max_deliver=5,
        jetstream_nak_backoff=1.0,
        jetstream_max_backoff=30.0,
        jetstream_ack_wait=0.1,
    )
    service.config = config

    container = MagicMock()
    container.config = config
    container._active_tasks = set()
    container._extensions = []
    container.nc = _make_mock_nats_client(is_connected=True)
    container.js = container.nc.jetstream()
    container._safe_ack = AsyncMock(return_value=True)
    container._safe_nak = AsyncMock(return_value=True)
    container._safe_term = AsyncMock(return_value=True)
    container._safe_in_progress = AsyncMock(return_value=True)
    container._publish_dlq = AsyncMock()
    container._dead_letter_terminated = AsyncMock()
    container._dead_letter_decode_error = AsyncMock()
    service.container = container

    return service


# ==============================================================================
# Suite 1: Lifecycle & Connection Timing (#317)
# ==============================================================================


@pytest.mark.unit
def test_broker_initial_state() -> None:
    """Hosted broker begins in an unattached, disconnected state."""
    broker = CliffracerHostedNatsBroker()
    assert broker._attached is False
    assert broker._connection is None


@pytest.mark.unit
async def test_direct_connect_without_attachment_raises() -> None:
    """Calling connect() before Cliffracer attaches connection raises RuntimeError."""
    broker = CliffracerHostedNatsBroker()
    with pytest.raises(RuntimeError, match="requires an active connection attached"):
        await broker.connect()


@pytest.mark.unit
async def test_setup_does_not_require_connection() -> None:
    """Extension setup() prepares broker and routes without accessing uninitialized transport."""
    router = NatsRouter()

    @router.subscriber("orders.created")
    async def on_order(msg: dict) -> None:
        pass

    ext = FastStreamExtension(router=SharedDependency(router))
    service = _make_mock_service()
    # Transport is not yet connected during setup
    service.container.nc = None

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)

    assert ext.hosted_broker is not None
    assert ext.hosted_broker._attached is False
    assert len(ext.hosted_broker.subscribers) == 1


@pytest.mark.unit
async def test_start_attaches_connection_and_starts_broker() -> None:
    """Extension start() attaches Cliffracer's connected NATS client."""
    ext = FastStreamExtension()
    service = _make_mock_service()
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)
    await ext.start()

    assert ext.hosted_broker is not None
    assert ext.hosted_broker._attached is True
    assert ext.hosted_broker._connection is service.container.nc


@pytest.mark.unit
async def test_start_fails_if_nats_disconnected() -> None:
    """Extension start() raises ServiceLifecycleError if NATS client is uninitialized or disconnected."""
    ext = FastStreamExtension()
    service = _make_mock_service()
    service.container.nc = _make_mock_nats_client(is_connected=False)
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)

    with pytest.raises(
        ServiceLifecycleError, match="NATS connection is uninitialized or disconnected"
    ):
        await ext.start()


@pytest.mark.unit
async def test_stop_does_not_drain_nats_transport() -> None:
    """Extension stop() halts subscribers while strictly shielding transport from drain."""
    ext = FastStreamExtension()
    service = _make_mock_service()
    nc = service.container.nc
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)
    await ext.start()

    await ext.stop()

    assert nc.drain.called is False
    assert ext.hosted_broker is not None
    assert ext.hosted_broker._connection is None
    assert ext.hosted_broker._attached is False


@pytest.mark.unit
def test_stream_declaration_suppressed() -> None:
    """Autonomous stream declarations on router streams are shielded (declare=False)."""
    broker = CliffracerHostedNatsBroker()
    router = NatsRouter()
    stream = JStream(name="test_stream", declare=True)

    @router.subscriber("events.>", stream=stream)
    async def on_event(msg: dict) -> None:
        pass

    assert stream.declare is True
    broker.include_router(router)
    assert stream.declare is False

    nc = _make_mock_nats_client(is_connected=True)
    broker.attach_cliffracer_connection(nc)
    assert stream.declare is False


# ==============================================================================
# Suite 2: Graceful Shutdown Task Drain Bridge (#318)
# ==============================================================================


@pytest.mark.unit
async def test_inflight_handler_registered_in_active_tasks() -> None:
    """In-flight worker task is tracked in container._active_tasks during handler execution."""
    service = _make_mock_service()
    container = service.container
    middleware = CliffracerAckMiddleware(container=container, config=service.config)

    task_present_during_call = False

    async def sample_handler(msg: Any) -> str:
        nonlocal task_present_during_call
        current = asyncio.current_task()
        task_present_during_call = current in container._active_tasks
        await asyncio.sleep(0.01)
        return "done"

    mock_msg = MagicMock()
    mock_msg.metadata = MagicMock(num_delivered=1)
    mock_msg.ack = AsyncMock()

    result = await middleware.consume_scope(sample_handler, mock_msg)
    assert result == "done"
    assert task_present_during_call is True
    # Once complete, task is removed from _active_tasks
    assert len(container._active_tasks) == 0


@pytest.mark.unit
async def test_drain_hook_unsubscribes_consumers() -> None:
    """Extension drain() unsubscribes consumer subscriptions to halt incoming traffic."""
    ext = FastStreamExtension()
    service = _make_mock_service()
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)

    sub_mock = MagicMock()
    sub_mock.subscription = MagicMock()
    sub_mock.subscription.unsubscribe = AsyncMock()
    sub_mock._fetch_sub = MagicMock()
    sub_mock._fetch_sub.unsubscribe = AsyncMock()
    sub_mock.running = True

    assert ext.hosted_broker is not None
    ext.hosted_broker._subscribers.add(sub_mock)

    await ext.drain(timeout=5.0)

    sub_mock.subscription.unsubscribe.assert_awaited_once()
    sub_mock._fetch_sub.unsubscribe.assert_awaited_once()
    assert sub_mock.running is False


@pytest.mark.unit
async def test_drain_handles_exceptions_gracefully() -> None:
    """Unsubscribe failures during drain() are swallowed and do not interrupt shutdown."""
    ext = FastStreamExtension()
    service = _make_mock_service()
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)

    sub_mock = MagicMock()
    sub_mock.subscription = MagicMock()
    sub_mock.subscription.unsubscribe = AsyncMock(side_effect=RuntimeError("connection closed"))
    sub_mock.running = True

    assert ext.hosted_broker is not None
    ext.hosted_broker._subscribers.add(sub_mock)

    # Should not raise
    await ext.drain(timeout=5.0)
    assert sub_mock.running is False


@pytest.mark.unit
async def test_core_nats_message_bypasses_ack_logic_and_tracks_tasks() -> None:
    """Non-JetStream (Core NATS) messages bypass ack/nak/term while tracking tasks."""
    service = _make_mock_service()
    container = service.container
    middleware = CliffracerAckMiddleware(container=container, config=service.config)

    core_msg = MagicMock()
    core_msg.metadata = None  # Core NATS has no JetStream metadata

    task_tracked = False

    async def handler(msg: Any) -> str:
        nonlocal task_tracked
        task_tracked = asyncio.current_task() in container._active_tasks
        return "core_result"

    result = await middleware.consume_scope(handler, core_msg)
    assert result == "core_result"
    assert task_tracked is True
    assert len(container._active_tasks) == 0
    assert container._safe_ack.called is False
    assert container._safe_nak.called is False
    assert container._safe_term.called is False


# ==============================================================================
# Suite 3: CliffracerAckMiddleware & JetStream Resilience (#319)
# ==============================================================================


@pytest.mark.unit
def test_subscribers_enforced_manual_ack_policy() -> None:
    """Mounted subscribers have AckPolicy.MANUAL enforced to remove default REJECT_ON_ERROR."""
    broker = CliffracerHostedNatsBroker()
    router = NatsRouter()

    @router.subscriber("test.ack")
    async def handle_test(msg: dict) -> None:
        pass

    broker.include_router(router)
    for sub in broker.subscribers:
        assert sub.ack_policy == AckPolicy.MANUAL
        assert getattr(sub, "_SubscriberUsecase__auto_ack_disabled", False) is True


@pytest.mark.unit
async def test_successful_handler_acks_message() -> None:
    """Successful JetStream message execution triggers _safe_ack()."""
    service = _make_mock_service()
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    js_msg = MagicMock()
    js_msg.metadata = MagicMock(num_delivered=1)
    js_msg._ackd = False

    async def ok_handler(msg: Any) -> dict:
        return {"status": "ok"}

    result = await middleware.consume_scope(ok_handler, js_msg)
    assert result == {"status": "ok"}
    service.container._safe_ack.assert_awaited_once_with(js_msg)
    service.container._safe_nak.assert_not_called()
    service.container._safe_term.assert_not_called()


@pytest.mark.unit
async def test_transient_error_naks_with_exponential_backoff() -> None:
    """Transient errors trigger _safe_nak() with exponential backoff based on num_delivered."""
    service = _make_mock_service()
    service.config.jetstream_nak_backoff = 1.5
    service.config.jetstream_max_backoff = 20.0
    service.config.jetstream_max_deliver = 5
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    async def failing_handler(msg: Any) -> None:
        raise ConnectionResetError("temporary network glitch")

    # Delivery #1: 1.5 * 2^0 = 1.5s
    msg1 = MagicMock()
    msg1.metadata = MagicMock(num_delivered=1)
    await middleware.consume_scope(failing_handler, msg1)
    service.container._safe_nak.assert_awaited_with(msg1, delay=1.5)

    # Delivery #3: 1.5 * 2^2 = 6.0s
    msg3 = MagicMock()
    msg3.metadata = MagicMock(num_delivered=3)
    await middleware.consume_scope(failing_handler, msg3)
    service.container._safe_nak.assert_awaited_with(msg3, delay=6.0)

    # Delivery #4: 1.5 * 2^3 = 12.0s
    msg4 = MagicMock()
    msg4.metadata = MagicMock(num_delivered=4)
    await middleware.consume_scope(failing_handler, msg4)
    service.container._safe_nak.assert_awaited_with(msg4, delay=12.0)


@pytest.mark.unit
async def test_delivery_exhaustion_routes_to_dlq_and_terms() -> None:
    """Exhausted deliveries (num_delivered >= max_deliver) publish to DLQ and execute _safe_term()."""
    service = _make_mock_service("order_svc")
    service.config.jetstream_max_deliver = 5
    service.config.dlq_subject = "{service}.dlq"
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    err = RuntimeError("persistent database failure")

    async def fatal_handler(msg: Any) -> None:
        raise err

    msg = MagicMock()
    msg.subject = "orders.create"
    msg.data = b'{"order_id": 123}'
    msg.metadata = MagicMock(num_delivered=5)

    await middleware.consume_scope(fatal_handler, msg)

    service.container._safe_term.assert_awaited_once_with(msg)
    service.container._safe_nak.assert_not_called()
    service.container._safe_ack.assert_not_called()


@pytest.mark.unit
async def test_dlq_failure_still_terminates_message() -> None:
    """Failure during DLQ publication does not prevent _safe_term() to avoid redelivery loops."""
    service = _make_mock_service("order_svc")
    service.config.jetstream_max_deliver = 3
    # Force DLQ publish to raise
    service.container._dead_letter_terminated = AsyncMock(
        side_effect=RuntimeError("DLQ unavailable")
    )
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    async def fatal_handler(msg: Any) -> None:
        raise ValueError("corrupt state")

    msg = MagicMock()
    msg.subject = "orders.create"
    msg.metadata = MagicMock(num_delivered=3)

    await middleware.consume_scope(fatal_handler, msg)

    # Even though DLQ raised, term was executed
    service.container._safe_term.assert_awaited_once_with(msg)


@pytest.mark.unit
async def test_malformed_payload_dlqs_and_terms_immediately() -> None:
    """Malformed or invalid message payloads route to DLQ immediately without retrying."""
    service = _make_mock_service("order_svc")
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    async def decode_error_handler(msg: Any) -> None:
        raise json.JSONDecodeError("Expecting value", "{bad_json", 1)

    msg = MagicMock()
    msg.subject = "orders.create"
    msg.data = b"{bad_json"
    # Even on delivery 1, malformed messages should not retry
    msg.metadata = MagicMock(num_delivered=1)

    await middleware.consume_scope(decode_error_handler, msg)

    service.container._safe_term.assert_awaited_once_with(msg)
    service.container._safe_nak.assert_not_called()


@pytest.mark.unit
async def test_policy_refusal_acks_immediately() -> None:
    """Policy refusals (RejectMessage) acknowledge immediately per Cliffracer invariants."""
    service = _make_mock_service()
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    # Cliffracer RejectMessage
    async def cliffracer_rejected_handler(msg: Any) -> None:
        raise CliffracerRejectMessage("unauthorized request")

    msg1 = MagicMock()
    msg1.metadata = MagicMock(num_delivered=1)
    msg1._ackd = False

    await middleware.consume_scope(cliffracer_rejected_handler, msg1)
    service.container._safe_ack.assert_awaited_with(msg1)
    service.container._safe_nak.assert_not_called()
    service.container._safe_term.assert_not_called()

    # FastStream RejectMessage
    async def fs_rejected_handler(msg: Any) -> None:
        raise FsRejectMessage()

    msg2 = MagicMock()
    msg2.metadata = MagicMock(num_delivered=1)
    msg2._ackd = False

    await middleware.consume_scope(fs_rejected_handler, msg2)
    service.container._safe_ack.assert_awaited_with(msg2)


@pytest.mark.unit
async def test_heartbeat_pulses_during_long_execution() -> None:
    """In-progress heartbeat pulses active JetStream messages during execution."""
    service = _make_mock_service()
    # Fast heartbeat interval for testing
    service.config.jetstream_ack_wait = 0.05
    middleware = CliffracerAckMiddleware(container=service.container, config=service.config)

    async def slow_handler(msg: Any) -> str:
        await asyncio.sleep(0.12)
        return "slow_done"

    msg = MagicMock()
    msg.metadata = MagicMock(num_delivered=1)
    msg.in_progress = AsyncMock()

    result = await middleware.consume_scope(slow_handler, msg)
    assert result == "slow_done"
    assert service.container._safe_in_progress.call_count >= 1


# ==============================================================================
# Suite 4: ContextRepo Surfacing & Health Telemetry (#320)
# ==============================================================================


@pytest.mark.unit
async def test_context_repo_injection_service_container_config() -> None:
    """Extension start() surfaces service, container, config, KV, and resilience into ContextRepo."""
    ext = FastStreamExtension()
    service = _make_mock_service("hypervisor_svc")
    kv_mock = MagicMock()
    resilience_mock = MagicMock()
    service.kv = kv_mock
    service.resilience = resilience_mock
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)
    await ext.start()

    assert ext.hosted_broker is not None
    context = ext.hosted_broker.context

    assert context.get("service") is service
    assert context.get("container") is service.container
    assert context.get("config") is service.config
    assert context.get("kv") is kv_mock
    assert context.get("resilience") is resilience_mock


@pytest.mark.unit
async def test_context_repo_cleanup_on_stop() -> None:
    """Extension stop() cleanly resets global dependencies from ContextRepo."""
    ext = FastStreamExtension()
    service = _make_mock_service()
    service.kv = MagicMock()
    service.resilience = MagicMock()
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)
    await ext.start()

    assert ext.hosted_broker is not None
    context = ext.hosted_broker.context
    assert context.get("service") is service

    await ext.stop()

    assert context.get("service") is None
    assert context.get("container") is None
    assert context.get("kv") is None
    assert context.get("resilience") is None


@pytest.mark.unit
async def test_health_details_reflects_broker_state() -> None:
    """health_details() accurately reflects broker status, subscriber counts, and active routes."""
    router = NatsRouter()

    @router.subscriber("orders.created")
    async def sub1(msg: dict) -> None:
        pass

    @router.subscriber("orders.shipped")
    async def sub2(msg: dict) -> None:
        pass

    ext = FastStreamExtension(router=SharedDependency(router))

    # Before setup: uninitialized
    assert ext.health_details() == {"status": "uninitialized"}

    service = _make_mock_service()
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)

    # Setup but not started
    health = ext.health_details()
    assert health is not None
    assert health["status"] == "stopped"
    assert health["subscribers_count"] == 2
    assert "orders.created" in health["active_routes"]
    assert "orders.shipped" in health["active_routes"]

    await ext.start()
    health_started = ext.health_details()
    assert health_started is not None
    assert health_started["status"] == "running"

    await ext.stop()
    health_stopped = ext.health_details()
    assert health_stopped is not None
    assert health_stopped["status"] == "stopped"


@pytest.mark.unit
async def test_info_details_lists_mounted_routes_and_subscribers() -> None:
    """info_details() surfaces structured subscriber route and queue introspection."""
    router = NatsRouter()

    @router.subscriber("payments.charge", queue="payment_workers")
    async def charge_sub(msg: dict) -> None:
        pass

    ext = FastStreamExtension(router=SharedDependency(router))
    assert ext.info_details() is None

    service = _make_mock_service()
    ext = ext.bind(service, "faststream")

    ctx = ExtensionSetupContext(
        service_config=service.config,
        broker_url="nats://localhost:4222",
        service=service,
    )
    await ext.setup(ctx)

    info = ext.info_details()
    assert info is not None
    assert info["routers_count"] == 1
    assert len(info["subscribers"]) == 1
    assert info["subscribers"][0]["subject"] == "payments.charge"
    assert info["subscribers"][0]["queue"] == "payment_workers"

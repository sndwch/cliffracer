"""Comprehensive verification test suite for core architectural decomposition.

Verifies:
1. Module boundaries and subsystem isolation (Registry, Discovery, Connection, Dispatcher, Lifecycle).
2. Complete absence of the 250+ line deprecated delegation layer on CliffracerService.
3. Elimination of circular callback ping-pong between Container and CliffracerService.
4. Deterministic phased startup, shutdown, and abortive cleanup sequences.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer.core.connection import (
    BrokerConnectionState,
    ConnectionManager,
    redact_nats_url,
)
from cliffracer.core.decorators import listener, rpc, timer
from cliffracer.core.dependencies import dependency
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.dispatcher import MessageDispatcher
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.extension import Extension
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec
from cliffracer.core.lifecycle import LifecycleManager
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing.messages import MockMessage

# ==============================================================================
# 1. Module Boundaries and Subsystem Isolation
# ==============================================================================


@pytest.mark.unit
def test_registry_pure_data_repository_isolation() -> None:
    """ServiceRegistry is a pure data repository with no network or transport dependencies."""
    reg = ServiceRegistry()
    assert reg.feature_counts() == {"rpc": 0, "events": 0, "timers": 0, "broadcasts": 0}

    # Register handlers and metadata
    def sample_rpc() -> str:
        return "ok"

    def sample_event(msg: Any) -> None:
        pass

    reg.rpc_handlers["sample_rpc"] = sample_rpc
    reg.event_handlers["orders.created"] = sample_event
    reg.event_durables["orders.created"] = "order_worker"
    reg.event_fanout.add("orders.broadcast")
    reg.event_pull.add("orders.created")
    reg.timers.append(MagicMock())
    reg.broadcast_handlers["orders.broadcast"] = sample_event

    assert reg.feature_counts() == {"rpc": 1, "events": 1, "timers": 1, "broadcasts": 1}

    reg.clear()
    assert reg.feature_counts() == {"rpc": 0, "events": 0, "timers": 0, "broadcasts": 0}
    assert len(reg.event_durables) == 0
    assert len(reg.event_fanout) == 0
    assert len(reg.event_pull) == 0


@pytest.mark.unit
def test_discovery_stateless_inspection_and_validation() -> None:
    """HandlerDiscovery inspects class methods and populates registry without service mutation."""

    class SampleService(CliffracerService):
        @rpc
        async def compute(self, a: int, b: int) -> int:
            return a + b

        @listener("events.created", fanout=True)
        async def on_event(self) -> None:
            pass

        @timer(interval=60.0)
        async def tick(self) -> None:
            pass

        @dependency(name="postgres", timeout=5.0)
        async def check_pg(self) -> bool:
            return True

    cfg = ServiceConfig(name="sample_svc", jetstream_enabled=False)
    reg = ServiceRegistry()
    svc = SampleService(cfg)

    # Inspect and discover without running lifecycle or connecting
    HandlerDiscovery.discover(svc, cfg, registry=reg)

    assert "compute" in reg.rpc_handlers
    assert "events.created" in reg.event_handlers
    assert "events.created" in reg.event_fanout
    assert len(reg.timers) == 1
    assert len(reg.dependencies) == 1
    assert reg.dependencies[0].name == "postgres"


@pytest.mark.unit
def test_discovery_validates_semantic_invariants() -> None:
    """HandlerDiscovery enforces strict topological constraints and raises ConfigurationError."""

    # 1. Listener with neither fanout nor durable
    class BadListenerService(CliffracerService):
        @listener("events.unspecified")
        async def bad_listener(self) -> None:
            pass

    cfg = ServiceConfig(name="bad_svc", jetstream_enabled=True)
    svc = BadListenerService(cfg)
    with pytest.raises(ConfigurationError, match="declare neither a durable nor fanout"):
        HandlerDiscovery.discover(svc, cfg)

    # 2. Conflicting fanout and durable
    class ConflictingService(CliffracerService):
        @listener("events.conflict", durable="c_worker", fanout=True)
        async def conflict_listener(self) -> None:
            pass

    svc_conflict = ConflictingService(cfg)
    with pytest.raises(ConfigurationError, match="declare\\(s\\) BOTH a durable and fanout=True"):
        HandlerDiscovery.discover(svc_conflict, cfg)

    # 3. Duplicate durable names
    class DuplicateDurableService(CliffracerService):
        @listener("events.one", durable="shared_worker")
        async def one(self) -> None:
            pass

        @listener("events.two", durable="shared_worker")
        async def two(self) -> None:
            pass

    svc_dup = DuplicateDurableService(cfg)
    with pytest.raises(
        ConfigurationError, match="durable 'shared_worker' is claimed by 2 event subjects"
    ):
        HandlerDiscovery.discover(svc_dup, cfg)

    # 4. DLQ stream coverage assertion
    cfg_no_dlq = ServiceConfig(
        name="no_dlq_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="orders", subjects=["orders.*"])],
        dlq_subject="uncovered.dlq",
    )
    with pytest.raises(
        StreamDeclarationError, match="no declared stream covers the dead-letter subject"
    ):
        HandlerDiscovery.validate_dlq_coverage(cfg_no_dlq)


@pytest.mark.unit
def test_connection_manager_state_machine_and_redaction() -> None:
    """ConnectionManager isolates broker dialing, URL redaction, and transport states."""
    cfg = ServiceConfig(name="conn_svc", nats_url="nats://user:secret@nats.internal:4222")
    conn = ConnectionManager(cfg)

    # Check URL redaction
    assert redact_nats_url(cfg.nats_url) == "nats://***@nats.internal:4222"
    assert redact_nats_url("nats://plain-host:4222") == "nats://plain-host:4222"

    # Initial state
    assert conn.broker_state == BrokerConnectionState.DISCONNECTED
    assert not conn.is_broker_connected
    assert not conn.jetstream_active

    # Mock NATS connection transitions
    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False
    conn.nc = mock_nc
    assert conn.broker_state == BrokerConnectionState.CONNECTED
    assert conn.is_broker_connected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatcher_executes_rpc_and_envelopes() -> None:
    """MessageDispatcher validates schema, invokes handlers, and formats response envelopes."""
    reg = ServiceRegistry()

    async def add(a: int, b: int) -> int:
        return a + b

    from cliffracer.core.typed_rpc import build_handler_spec

    reg.rpc_handlers["add"] = add
    reg.rpc_specs["add"] = build_handler_spec("add", add, owner=object)

    cfg = ServiceConfig(name="calc_svc")
    dispatcher = MessageDispatcher(
        registry=reg,
        config=cfg,
        connection_provider=lambda: MagicMock(nc=None, js=None, jetstream_active=False),
        extensions=[],
    )

    msg = MockMessage(subject="calc_svc.rpc.add", data=b'{"a": 10, "b": 25}')
    await dispatcher.handle_rpc_request(msg)

    assert msg.responded_data is not None
    import json

    payload = json.loads(msg.responded_data)
    assert payload["success"] is True
    assert payload["result"] == 35


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifecycle_manager_supervision_and_drain() -> None:
    """LifecycleManager tracks active background tasks and drains them on shutdown."""
    cfg = ServiceConfig(name="lifecycle_svc", shutdown_timeout=2.0)
    lm = LifecycleManager(config=cfg)

    executed = False

    async def long_running() -> None:
        nonlocal executed
        await asyncio.sleep(0.01)
        executed = True

    task = lm.spawn_supervised_task(long_running(), name="test_task")
    assert task in lm.active_tasks
    await lm.drain_active_tasks(timeout=1.0)
    assert executed is True
    assert len(lm.active_tasks) == 0


# ==============================================================================
# 2. Absence of Deprecated Delegation Layer on CliffracerService
# ==============================================================================


@pytest.mark.unit
def test_complete_absence_of_deprecated_delegation_methods_on_service() -> None:
    """Verify all 26 deprecated delegation methods are completely removed from CliffracerService."""
    deprecated_methods = [
        "_setup_subscriptions",
        "_on_rpc_request",
        "_on_describe_request",
        "_on_async_request",
        "_handle_rpc_request",
        "_handle_async_request",
        "_handle_describe_request",
        "_dispatch_event",
        "_handle_event",
        "_handle_jetstream_event",
        "_run_worker",
        "_closed_callback",
        "_assert_dlq_covered",
        "_publish_dlq",
        "_spawn_supervised_task",
        "_setup_extensions",
        "_start_extensions",
        "_stop_extensions",
        "_bind_extension",
        "_with_namespace",
        "_effective_event_subject",
        "_subject_matches",
        "_make_event_callback",
        "_make_jetstream_event_callback",
        "_pull_once",
        "_report_consumer_drift",
        "_handle_invalid_message",
        "_error_callback",
        "_disconnected_callback",
        "_reconnected_callback",
        "_dead_letter_terminated",
    ]

    svc = CliffracerService(ServiceConfig(name="clean_svc"))

    for method_name in deprecated_methods:
        assert not hasattr(CliffracerService, method_name), (
            f"CliffracerService must not define deprecated method '{method_name}'"
        )
        assert not hasattr(svc, method_name), (
            f"CliffracerService instance must not define deprecated method '{method_name}'"
        )


@pytest.mark.unit
def test_complete_absence_of_deprecated_delegation_properties_on_service() -> None:
    """Verify deprecated private properties are completely removed from CliffracerService."""
    deprecated_properties = [
        "_subscriptions",
        "_active_tasks",
        "_rpc_handlers",
        "_rpc_specs",
        "_event_handlers",
        "_event_schemas",
        "_event_durables",
        "_event_fanout",
        "_event_pull",
        "_event_handler_names",
        "_entrypoint_kinds",
        "_jetstream_active",
    ]

    svc = CliffracerService(ServiceConfig(name="clean_svc"))

    for prop_name in deprecated_properties:
        assert not hasattr(CliffracerService, prop_name), (
            f"CliffracerService must not define deprecated property '{prop_name}'"
        )
        assert not hasattr(svc, prop_name), (
            f"CliffracerService instance must not define deprecated property '{prop_name}'"
        )


@pytest.mark.unit
def test_service_preserves_clean_public_api() -> None:
    """Verify CliffracerService preserves its clean public façade methods and properties."""
    svc = CliffracerService(ServiceConfig(name="façade_svc"))

    # Public methods
    assert callable(getattr(svc, "start", None))
    assert callable(getattr(svc, "stop", None))
    assert callable(getattr(svc, "run", None))
    assert callable(getattr(svc, "health_check", None))
    assert callable(getattr(svc, "liveness_check", None))
    assert callable(getattr(svc, "is_live", None))
    assert callable(getattr(svc, "get_service_info", None))
    assert callable(getattr(svc, "call_rpc", None))
    assert callable(getattr(svc, "call_async", None))
    assert callable(getattr(svc, "call_rpc_no_wait", None))
    assert callable(getattr(svc, "publish_event", None))
    assert callable(getattr(svc, "broadcast_message", None))
    assert callable(getattr(svc, "add_dependency", None))
    assert callable(getattr(svc, "add_extension", None))

    # Public properties
    assert hasattr(svc, "container")
    assert hasattr(svc, "is_broker_connected")
    assert hasattr(svc, "broker_state")
    assert hasattr(svc, "nc")
    assert hasattr(svc, "js")
    assert hasattr(svc, "config")


# ==============================================================================
# 3. Elimination of Circular Callback Ping-Pong
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_dispatcher_subscription_routing_without_service_bounce() -> None:
    """NATS subscriptions bind directly to Container/Dispatcher callbacks, bypassing CliffracerService."""
    cfg = ServiceConfig(name="pingpong_test_svc")
    svc = CliffracerService(cfg)
    container = svc.container

    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False
    container.connection.nc = mock_nc

    # Execute subscription setup
    await container._setup_subscriptions()

    # Verify subscribed callbacks are directly on Dispatcher, NOT on CliffracerService
    sub_calls = mock_nc.subscribe.call_args_list
    assert len(sub_calls) >= 3  # RPC, describe, async

    # Inspect RPC subscription callback
    rpc_call = next(c for c in sub_calls if "rpc.*" in c.args[0])
    registered_cb = rpc_call.kwargs.get("cb")
    assert registered_cb == container.dispatcher.on_rpc_request
    assert registered_cb != getattr(svc, "_on_rpc_request", None)

    # Inspect Describe subscription callback
    desc_call = next(c for c in sub_calls if "describe" in c.args[0])
    assert desc_call.kwargs.get("cb") == container.dispatcher.on_describe_request

    # Inspect Async RPC subscription callback
    async_call = next(c for c in sub_calls if "async.*" in c.args[0])
    assert async_call.kwargs.get("cb") == container.dispatcher.on_async_request


# ==============================================================================
# 4. Clean Startup and Shutdown Sequences
# ==============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_phased_startup_and_shutdown_sequence() -> None:
    """Startup and shutdown follow deterministic, phased order across extensions and subsystems."""
    events: list[str] = []

    class OrderExtension(Extension):
        def __init__(self, tag: str) -> None:
            super().__init__()
            self.tag = tag

        async def setup(self, ctx: Any) -> None:
            events.append(f"setup:{self.tag}")

        async def start(self) -> None:
            events.append(f"start:{self.tag}")

        async def stop(self) -> None:
            events.append(f"stop:{self.tag}")

    class OrderService(CliffracerService):
        ext1 = OrderExtension("ext1")
        ext2 = OrderExtension("ext2")

        async def on_startup(self) -> None:
            events.append("on_startup")

        async def on_shutdown(self) -> None:
            events.append("on_shutdown")

    cfg = ServiceConfig(name="order_svc", health_port=0, health_listener=False)
    svc = OrderService(cfg)

    # Mock NATS connection to avoid real network calls
    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False

    async def fake_connect() -> None:
        events.append("connect")
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        events.append("disconnect")
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    # Start service
    await svc.start()

    # Verify startup sequence:
    # Extensions setup -> Discovery -> NATS connect -> on_startup -> Extensions start -> Health -> Subscriptions
    assert events[:6] == [
        "setup:ext1",
        "setup:ext2",
        "connect",
        "on_startup",
        "start:ext1",
        "start:ext2",
    ]
    assert svc._running is True

    # Stop service
    await svc.stop()

    # Verify teardown sequence:
    # on_shutdown -> Extensions stop (reverse) -> NATS disconnect
    assert events[6:] == [
        "on_shutdown",
        "stop:ext2",
        "stop:ext1",
        "disconnect",
    ]
    assert svc._running is False
    assert svc.container.lifecycle.is_stopped is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abortive_startup_cleans_up_and_reraises() -> None:
    """If a startup step fails, partial resources are cleanly torn down and the original exception is re-raised."""
    cleanup_events: list[str] = []

    class FailingService(CliffracerService):
        async def on_startup(self) -> None:
            raise ValueError("Intentional on_startup failure")

    cfg = ServiceConfig(name="fail_svc", health_port=0, health_listener=False)
    svc = FailingService(cfg)

    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False

    async def fake_connect() -> None:
        cleanup_events.append("connected")
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        cleanup_events.append("disconnected")
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="Intentional on_startup failure"):
        await svc.start()

    assert "connected" in cleanup_events
    assert "disconnected" in cleanup_events
    assert svc._running is False
    assert svc.container.connection.nc is None

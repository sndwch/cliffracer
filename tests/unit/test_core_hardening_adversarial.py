"""Adversarial validation suite for core event typing, error hierarchy, and container encapsulation.

Invariants:
- Rejects untyped, variadic, positional-only, and bare-container handler signatures at startup with UntypedHandler.
- Routes invalid event payloads to DLQ and safely terminates JetStream messages without dispatcher crash.
- Establishes RpcError as the unified parent class for all client and server RPC exceptions.
- Trips cliffracer-resilience CircuitBreaker on any monitored RpcError subclass.
- Encapsulates Container on CliffracerService._container without __setattr__ monkey-patching or reverse delegation leaks.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock

import nats
import pytest
from cliffracer_resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    RpcCircuitOpenError,
)
from pydantic import BaseModel, Field

from cliffracer.core.container import Container
from cliffracer.core.decorators import broadcast, listener
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.dispatch.events import DispatchOutcome
from cliffracer.core.exceptions import (
    ClientError,
    ClientOutOfDate,
    ClientOutOfDateError,
    CliffracerError,
    RpcClientError,
    RPCError,
    RpcError,
    RpcNoResponders,
    RpcNoRespondersError,
    RpcRefused,
    RpcRefusedError,
    RpcRemoteError,
    RpcServerError,
    RpcTimeout,
    RpcTimeoutError,
    RpcUnknownMethod,
    RpcUnknownMethodError,
    RpcValidationError,
    ServiceError,
)
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.typed_rpc import UntypedHandler

pytestmark = pytest.mark.unit


class UserCreated(BaseModel):
    user_id: str
    email: str


# ==============================================================================
# 1. Strict Event Typing
# ==============================================================================


def test_unannotated_parameter_fails_startup_with_untyped_handler() -> None:
    """Refuse unannotated event parameters during discovery."""

    class UnannotatedService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, payload: Any = ...) -> None:
            pass

    # We dynamically construct unannotated method to test UntypedHandler cleanly
    async def unannotated_handler(self: Any, payload) -> None:  # type: ignore[no-untyped-def]
        pass

    unannotated_handler._cliffracer_events = ["events.user"]  # type: ignore[attr-defined]
    unannotated_handler._cliffracer_event_fanout = {"events.user"}  # type: ignore[attr-defined]
    UnannotatedService.handle_user = unannotated_handler

    svc = UnannotatedService(ServiceConfig(name="test_unannotated"))
    with pytest.raises(UntypedHandler, match="parameter 'payload' has no annotation"):
        svc.container.discover_handlers()


def test_positional_only_arg_fails_startup_with_untyped_handler() -> None:
    """Refuse positional-only event parameters during discovery."""

    class PositionalOnlyService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, user_id: str, /) -> None:
            pass

    svc = PositionalOnlyService(ServiceConfig(name="test_pos_only"))
    with pytest.raises(UntypedHandler, match="positional-only parameter 'user_id' is not allowed"):
        svc.container.discover_handlers()


def test_var_positional_args_fails_startup_with_untyped_handler() -> None:
    """Refuse *args in event handlers during discovery."""

    class VarArgsService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, *args: str) -> None:
            pass

    svc = VarArgsService(ServiceConfig(name="test_var_args"))
    with pytest.raises(UntypedHandler, match=r"\*args is not allowed on an event handler"):
        svc.container.discover_handlers()


def test_var_keyword_kwargs_fails_startup_with_untyped_handler() -> None:
    """Refuse **kwargs in event handlers during discovery."""

    class VarKwargsService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, **kwargs: Any) -> None:
            pass

    svc = VarKwargsService(ServiceConfig(name="test_var_kwargs"))
    with pytest.raises(UntypedHandler, match=r"\*kwargs is not allowed on an event handler"):
        svc.container.discover_handlers()


def test_var_keyword_data_fails_startup_with_untyped_handler() -> None:
    """Refuse **data in event handlers during discovery."""

    class VarDataService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, **data: Any) -> None:
            pass

    svc = VarDataService(ServiceConfig(name="test_var_data"))
    with pytest.raises(UntypedHandler, match=r"\*data is not allowed on an event handler"):
        svc.container.discover_handlers()


def test_bare_dict_fails_startup_with_untyped_handler() -> None:
    """Refuse bare dict parameters during discovery."""

    class BareDictService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, payload: dict) -> None:
            pass

    svc = BareDictService(ServiceConfig(name="test_bare_dict"))
    with pytest.raises(UntypedHandler, match="parameter 'payload': dict is unsupported"):
        svc.container.discover_handlers()


def test_bare_list_fails_startup_with_untyped_handler() -> None:
    """Refuse bare list parameters during discovery."""

    class BareListService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, payload: list) -> None:
            pass

    svc = BareListService(ServiceConfig(name="test_bare_list"))
    with pytest.raises(UntypedHandler, match="parameter 'payload': list is unsupported"):
        svc.container.discover_handlers()


def test_bare_any_fails_startup_with_untyped_handler() -> None:
    """Refuse bare Any parameters during discovery."""

    class BareAnyService(CliffracerService):
        @listener("events.user", fanout=True)
        async def handle_user(self, payload: Any) -> None:
            pass

    svc = BareAnyService(ServiceConfig(name="test_bare_any"))
    with pytest.raises(UntypedHandler, match="parameter 'payload': Any is unsupported"):
        svc.container.discover_handlers()


def test_broadcast_handler_strict_typing_enforced() -> None:
    """Refuse untyped parameters and bare containers on @broadcast handlers."""

    class UntypedBroadcastService(CliffracerService):
        @broadcast("alerts.system")
        async def handle_alert(self, **data: Any) -> None:
            pass

    svc = UntypedBroadcastService(ServiceConfig(name="test_broadcast_untyped"))
    with pytest.raises(UntypedHandler, match=r"\*data is not allowed on an event handler"):
        svc.container.discover_handlers()


def test_reserved_and_private_names_fail_startup() -> None:
    """Refuse private parameter names and reserved BaseModel attributes."""

    class PrivateNameService(CliffracerService):
        @listener("events.test", fanout=True)
        async def handle_private(self, _secret: str) -> None:
            pass

    svc_priv = PrivateNameService(ServiceConfig(name="test_priv"))
    with pytest.raises(UntypedHandler, match="starting with '_'"):
        svc_priv.container.discover_handlers()

    class ReservedNameService(CliffracerService):
        @listener("events.test", fanout=True)
        async def handle_reserved(self, schema: str) -> None:
            pass

    svc_res = ReservedNameService(ServiceConfig(name="test_res"))
    with pytest.raises(UntypedHandler, match="conflicts with BaseModel member"):
        svc_res.container.discover_handlers()


@pytest.mark.asyncio
async def test_invalid_payload_routes_to_dlq_and_calls_safe_term() -> None:
    """Route invalid payload to DLQ, invoke safe_term(msg), and return INVALID without crash."""
    invoked = False

    class StrictEventService(CliffracerService):
        @listener("orders.checkout", fanout=True)
        async def on_checkout(self, order_id: str, amount: Annotated[float, Field(gt=0)]) -> None:
            nonlocal invoked
            invoked = True

    svc = StrictEventService(ServiceConfig(name="strict_svc", dlq_subject="dlq.orders"))
    svc.container.discover_handlers()

    dispatcher = svc.container.event_dispatcher
    mock_dlq = AsyncMock(spec=DeadLetterPublisher)
    dispatcher.dlq = mock_dlq

    # Case A: Wrong type (string for float) and extra field (violates extra="forbid")
    msg_a = AsyncMock()
    msg_a.subject = "orders.checkout"
    msg_a.data = b'{"order_id": "ord-1", "amount": "not_a_float", "extra_bad": 999}'
    msg_a.headers = {"Content-Type": "application/json"}

    outcome_a = await dispatcher.handle_event(msg_a)
    assert outcome_a == DispatchOutcome.INVALID
    assert invoked is False
    assert mock_dlq.handle_invalid_message.called
    assert msg_a.term.called

    mock_dlq.reset_mock()

    # Case B: Missing required field (order_id omitted)
    msg_b = AsyncMock()
    msg_b.subject = "orders.checkout"
    msg_b.data = b'{"amount": 19.95}'
    msg_b.headers = {"Content-Type": "application/json"}

    outcome_b = await dispatcher.handle_event(msg_b)
    assert outcome_b == DispatchOutcome.INVALID
    assert invoked is False
    assert mock_dlq.handle_invalid_message.called
    assert msg_b.term.called

    # Case C: Valid payload succeeds
    msg_c = AsyncMock()
    msg_c.subject = "orders.checkout"
    msg_c.data = b'{"order_id": "ord-1", "amount": 19.95}'
    msg_c.headers = {"Content-Type": "application/json"}

    outcome_c = await dispatcher.handle_event(msg_c)
    assert outcome_c == DispatchOutcome.OK
    assert invoked is True


@pytest.mark.asyncio
async def test_safe_term_survives_missing_or_failing_term() -> None:
    """Verify safe_term does not crash if msg has no term method or if term raises."""
    invoked = False

    class StrictService(CliffracerService):
        @listener("events.strict", fanout=True)
        async def on_event(self, count: int) -> None:
            nonlocal invoked
            invoked = True

    svc = StrictService(ServiceConfig(name="test_safe_term"))
    svc.container.discover_handlers()

    dispatcher = svc.container.event_dispatcher
    dispatcher.dlq = AsyncMock(spec=DeadLetterPublisher)

    # 1. Message with no term method (standard core NATS message)
    plain_msg = MagicMock()
    plain_msg.subject = "events.strict"
    plain_msg.data = b'{"count": "not_an_int"}'
    plain_msg.headers = {"Content-Type": "application/json"}
    del plain_msg.term  # ensure no term attribute

    outcome_plain = await dispatcher.handle_event(plain_msg)
    assert outcome_plain == DispatchOutcome.INVALID
    assert invoked is False

    # 2. Message whose term() raises an exception
    failing_msg = AsyncMock()
    failing_msg.subject = "events.strict"
    failing_msg.data = b'{"count": "not_an_int"}'
    failing_msg.headers = {"Content-Type": "application/json"}
    failing_msg.term.side_effect = RuntimeError("broker error on term")

    outcome_failing = await dispatcher.handle_event(failing_msg)
    assert outcome_failing == DispatchOutcome.INVALID
    assert invoked is False
    assert failing_msg.term.called


# ==============================================================================
# 2. Error Hierarchy Unification
# ==============================================================================


def test_issubclass_client_error_rpc_error() -> None:
    """Verify issubclass(ClientError, RpcError) and all RPC error hierarchy relationships."""
    assert issubclass(ClientError, RpcError)
    assert issubclass(RpcClientError, RpcError)
    assert issubclass(RpcServerError, RpcError)
    assert issubclass(RpcRemoteError, RpcError)
    assert issubclass(RpcTimeoutError, RpcError)
    assert issubclass(RpcNoRespondersError, RpcError)
    assert issubclass(RpcValidationError, RpcError)
    assert issubclass(RpcUnknownMethodError, RpcError)
    assert issubclass(RpcRefusedError, RpcError)
    assert issubclass(ClientOutOfDateError, RpcError)
    assert issubclass(RPCError, RpcError)

    # Invariant: ClientError is a CliffracerError, but deliberately not a ServiceError
    assert issubclass(ClientError, CliffracerError)
    assert not issubclass(ClientError, ServiceError)
    assert not issubclass(RpcError, ServiceError)


def test_try_except_rpc_error_catches_all_client_and_server_errors() -> None:
    """Verify try: ... except RpcError catches both client-side and server-side errors."""
    client_and_server_exceptions: list[RpcError] = [
        RpcClientError("client error"),
        RpcServerError("server error"),
        RpcRemoteError("remote error"),
        RpcTimeoutError("timeout"),
        RpcNoRespondersError("no responders"),
        RpcValidationError([{"loc": ["arg"], "msg": "invalid"}]),
        RpcUnknownMethodError("unknown method"),
        RpcRefusedError("rate limit exceeded"),
        ClientOutOfDateError("svc", ["m1"], ["m2"]),
        ClientError("legacy client error alias"),
        RPCError("legacy rpc error alias"),
        RpcTimeout("legacy timeout alias"),
        RpcNoResponders("legacy no responders alias"),
        RpcUnknownMethod("legacy unknown method alias"),
        RpcRefused("legacy refused alias"),
        ClientOutOfDate("svc", ["m1"], ["m2"]),
    ]

    for exc in client_and_server_exceptions:
        caught = False
        try:
            raise exc
        except RpcError as caught_exc:
            caught = True
            assert caught_exc is exc

        assert caught is True, f"Failed to catch {exc.__class__.__name__} under RpcError"


@pytest.mark.asyncio
async def test_resilience_circuit_breaker_trips_on_rpc_error_and_subclasses() -> None:
    """Verify cliffracer-resilience CircuitBreaker trips on RpcError and its subclasses."""
    cb = CircuitBreaker("test_breaker", CircuitBreakerConfig(failure_threshold=3))
    assert cb.state == CircuitState.CLOSED

    # Failures with different RpcError subclasses
    failures = [
        RpcError("base rpc error"),
        ClientError("client error alias"),
        RpcTimeoutError("timed out waiting for reply"),
    ]

    for exc in failures:

        async def failing_call(err: BaseException = exc) -> None:
            raise err

        with pytest.raises(RpcError):
            await cb.call(failing_call)

    # After 3 consecutive RpcError failures, circuit must be OPEN
    assert cb.state == CircuitState.OPEN
    assert cb.is_open is True

    # Call while open must fail fast with RpcCircuitOpenError without executing function
    called = False

    async def candidate_call() -> str:
        nonlocal called
        called = True
        return "ok"

    with pytest.raises(RpcCircuitOpenError):
        await cb.call(candidate_call)

    assert called is False


@pytest.mark.asyncio
async def test_resilience_circuit_breaker_ignores_non_monitored_exceptions() -> None:
    """Verify CircuitBreaker does not increment failure count on non-monitored exceptions."""
    cb = CircuitBreaker("test_unmonitored", CircuitBreakerConfig(failure_threshold=2))
    assert cb.state == CircuitState.CLOSED

    async def non_rpc_failing_call() -> None:
        raise ValueError("unrelated error")

    # ValueError is not an RpcError or ConnectionError
    with pytest.raises(ValueError):
        await cb.call(non_rpc_failing_call)

    # Failure count must remain 0 and circuit remains CLOSED
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


# ==============================================================================
# 3. Container Internalization
# ==============================================================================


def test_container_encapsulation_and_no_magic_setattr() -> None:
    """Verify svc._container is encapsulated and attributes do not magically sync via __setattr__."""

    class EmptyService(CliffracerService):
        pass

    svc = EmptyService(ServiceConfig(name="encapsulated_svc"))

    # Invariant: svc._container is the Container instance and svc.container property exposes it
    assert hasattr(svc, "_container")
    assert isinstance(svc._container, Container)
    assert svc.container is svc._container

    # Invariant: svc.container has no setter
    with pytest.raises(AttributeError):
        svc.container = None

    # Invariant: Container does NOT have custom __setattr__ monkey-patching back to service
    assert Container.__setattr__ is object.__setattr__

    # Mutating an attribute on svc.container does NOT modify svc
    svc.container.isolated_attribute = "container_value"
    assert not hasattr(svc, "isolated_attribute")
    assert svc.container.isolated_attribute == "container_value"

    # Mutating an attribute on svc does NOT modify svc.container
    svc.service_attribute = "service_value"
    assert not hasattr(svc.container, "service_attribute")
    assert svc.service_attribute == "service_value"


def test_service_lifecycle_properties_reflect_container_state() -> None:
    """Verify CliffracerService lifecycle properties reflect container.lifecycle state."""

    class LifecycleService(CliffracerService):
        pass

    svc = LifecycleService(ServiceConfig(name="lifecycle_svc"))

    # Reading state reflects container.lifecycle
    assert svc._running is svc.container.lifecycle.is_running
    assert svc._starting is svc.container.lifecycle.is_starting
    assert svc._stopped is svc.container.lifecycle.is_stopped

    # Mutating _running on svc forwards to container.lifecycle._running without monkey-patching
    svc._running = True
    assert svc.container.lifecycle._running is True
    assert svc._running is True

    svc._running = False
    assert svc.container.lifecycle._running is False
    assert svc._running is False


@pytest.mark.asyncio
async def test_clean_lifecycle_state_transitions_without_reverse_delegations() -> None:
    """Verify deterministic startup and shutdown lifecycle transitions without reverse delegations."""

    class CleanService(CliffracerService):
        pass

    svc = CleanService(ServiceConfig(name="clean_svc", health_listener=False))
    mgr = svc.container.lifecycle

    # State before startup
    assert mgr.is_running is False
    assert mgr.is_stopped is False

    # Simulate broker connection and subscriptions
    mock_nc = AsyncMock(spec=nats.NATS)
    mock_nc.is_connected = True
    mock_nc.subscribe = AsyncMock(return_value=AsyncMock())
    mock_nc.flush = AsyncMock()
    mock_nc.close = AsyncMock()
    mock_nc.drain = AsyncMock()
    svc.nc = mock_nc

    async def mock_connect() -> None:
        pass

    async def mock_disconnect() -> None:
        pass

    svc.connect = mock_connect  # type: ignore[method-assign]
    svc.disconnect = mock_disconnect  # type: ignore[method-assign]

    # Run lifecycle start
    start_task = asyncio.create_task(mgr.start())
    await start_task
    assert mgr.is_running is True
    assert mgr.is_starting is False
    assert mgr.is_stopped is False

    # Run lifecycle stop
    stop_task = asyncio.create_task(mgr.stop())
    await stop_task
    assert mgr.is_running is False
    assert mgr.is_stopped is True

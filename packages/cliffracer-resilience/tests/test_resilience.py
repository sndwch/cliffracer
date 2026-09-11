"""Comprehensive tests for cliffracer-resilience: Circuit Breaker and Rate Limiting."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_resilience import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
    InMemoryRateLimiter,
    KvRateLimiter,
    RateLimitExceeded,
    ResilienceExtension,
    ResilientRpcProxy,
    RpcCircuitOpenError,
    rate_limit,
)

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import RpcRefused
from cliffracer.core.exceptions import ConnectionError as CliffracerConnectionError
from cliffracer.core.exceptions import RPCError, RPCTimeoutError


def _rpc_msg(subject: str, data: dict, headers: dict | None = None) -> AsyncMock:
    """Helper to build a mock incoming NATS message."""
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data).encode()
    m.headers = headers or {}
    m.reply = "_INBOX.test1234"
    return m


def _get_replies(msg: AsyncMock) -> list[dict]:
    """Helper to extract JSON decoded replies from msg.respond."""
    return [json.loads(c.args[0].decode()) for c in msg.respond.await_args_list]


# ============================================================================
# Circuit Breaker Unit Tests
# ============================================================================


@pytest.mark.unit
def test_circuit_breaker_initial_state():
    cb = CircuitBreaker("test-service")
    assert cb.state == CLOSED
    assert cb.is_closed is True
    assert cb.is_open is False
    assert cb.is_half_open is False
    assert cb.failure_count == 0
    assert cb.success_count == 0


@pytest.mark.unit
async def test_circuit_breaker_success_keeps_closed():
    cb = CircuitBreaker("test-service")
    async with cb:
        pass
    assert cb.state == CLOSED
    assert cb.success_count == 1
    assert cb.failure_count == 0


@pytest.mark.unit
async def test_circuit_breaker_trips_to_open_after_threshold():
    config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=10.0)
    cb = CircuitBreaker("test-service", config=config)

    for i in range(2):
        with pytest.raises(RPCTimeoutError):
            async with cb:
                raise RPCTimeoutError("timeout")
        assert cb.state == CLOSED
        assert cb.failure_count == i + 1

    # Third failure breaches threshold -> trips to OPEN
    with pytest.raises(RPCTimeoutError):
        async with cb:
            raise RPCTimeoutError("timeout")

    assert cb.state == OPEN
    assert cb.is_open is True
    assert cb.is_closed is False


@pytest.mark.unit
async def test_circuit_breaker_fast_fail_when_open():
    """Verify that when OPEN, calls fail fast locally without wire traffic."""
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60.0)
    cb = CircuitBreaker("payment-service", config=config)

    with pytest.raises(RPCTimeoutError):
        async with cb:
            raise RPCTimeoutError("timeout")

    assert cb.state == OPEN

    network_called = False

    async def mock_remote_call():
        nonlocal network_called
        network_called = True
        return "result"

    t0 = time.monotonic()
    with pytest.raises(RpcCircuitOpenError) as exc_info:
        await cb.call(mock_remote_call)
    elapsed = time.monotonic() - t0

    assert network_called is False, "No network call must be made when circuit is OPEN"
    assert elapsed < 0.05, f"Fast-fail must be near-instantaneous (took {elapsed:.4f}s)"
    assert "payment-service" in str(exc_info.value)
    assert exc_info.value.details.get("state") == "open"


@pytest.mark.unit
async def test_circuit_breaker_transition_to_half_open_after_cooldown():
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05)
    cb = CircuitBreaker("test-service", config=config)

    with pytest.raises(RPCTimeoutError):
        async with cb:
            raise RPCTimeoutError("timeout")

    assert cb.state == OPEN

    # Wait for recovery timeout to elapse
    await asyncio.sleep(0.06)

    # State property dynamically reflects HALF_OPEN
    assert cb.state == HALF_OPEN
    assert cb.is_half_open is True


@pytest.mark.unit
async def test_circuit_breaker_half_open_probe_success_resets_to_closed():
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05)
    cb = CircuitBreaker("test-service", config=config)

    with pytest.raises(RPCTimeoutError):
        async with cb:
            raise RPCTimeoutError("timeout")

    await asyncio.sleep(0.06)
    assert cb.state == HALF_OPEN

    # Probe call succeeds
    async with cb:
        pass

    assert cb.state == CLOSED
    assert cb.is_closed is True
    assert cb.failure_count == 0


@pytest.mark.unit
async def test_circuit_breaker_half_open_probe_failure_trips_to_open():
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05)
    cb = CircuitBreaker("test-service", config=config)

    with pytest.raises(RPCTimeoutError):
        async with cb:
            raise RPCTimeoutError("timeout")

    await asyncio.sleep(0.06)
    assert cb.state == HALF_OPEN

    # Probe call fails
    with pytest.raises(RpcRefused):
        async with cb:
            raise RpcRefused("service unavailable")

    # Trips immediately back to OPEN with renewed cooldown
    assert cb.state == OPEN
    assert cb.is_open is True


@pytest.mark.unit
async def test_circuit_breaker_half_open_max_calls():
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=1)
    cb = CircuitBreaker("test-service", config=config)
    cb.trip()
    await asyncio.sleep(0.06)
    assert cb.state == HALF_OPEN

    # First probe enters context
    await cb.__aenter__()
    try:
        # Second call while probe in progress fails fast
        with pytest.raises(RpcCircuitOpenError) as exc_info:
            async with cb:
                pass
        assert "HALF-OPEN" in str(exc_info.value)
    finally:
        await cb.__aexit__(None, None, None)

    assert cb.state == CLOSED


@pytest.mark.unit
async def test_circuit_breaker_monitored_exceptions_filtering():
    config = CircuitBreakerConfig(failure_threshold=2)
    cb = CircuitBreaker("test-service", config=config)

    # Unmonitored exception does not count towards threshold
    with pytest.raises(ValueError):
        async with cb:
            raise ValueError("bad argument")

    assert cb.failure_count == 0
    assert cb.state == CLOSED

    # Monitored exceptions do count
    with pytest.raises(CliffracerConnectionError):
        async with cb:
            raise CliffracerConnectionError("connection dropped")

    assert cb.failure_count == 1

    with pytest.raises(RPCError):
        async with cb:
            raise RPCError("RPC Error: refused: rate limit exceeded")

    assert cb.failure_count == 2
    assert cb.state == OPEN


@pytest.mark.unit
def test_circuit_breaker_manual_trip_and_reset():
    cb = CircuitBreaker("test-service")
    cb.trip()
    assert cb.state == OPEN
    cb.reset()
    assert cb.state == CLOSED
    assert cb.failure_count == 0


@pytest.mark.unit
def test_circuit_breaker_registry():
    registry = CircuitBreakerRegistry(default_config=CircuitBreakerConfig(failure_threshold=3))
    cb1 = registry.get("auth")
    cb2 = registry.get("auth")
    cb3 = registry.get("orders")

    assert cb1 is cb2
    assert cb1 is not cb3
    assert cb1.config.failure_threshold == 3

    cb1.trip()
    assert cb1.is_open is True
    registry.reset_all()
    assert cb1.is_closed is True


# ============================================================================
# ResilientRpcProxy Unit Tests
# ============================================================================


@pytest.mark.unit
async def test_resilient_rpc_proxy_success_and_failure():
    class TestService(CliffracerService):
        inventory = ResilientRpcProxy(
            "inventory_service",
            config=CircuitBreakerConfig(failure_threshold=2, recovery_timeout=10.0),
        )

    svc = TestService(ServiceConfig(name="test_svc"))

    # Mock call_rpc on service
    svc.call_rpc = AsyncMock(return_value={"stock": 42})

    # Successful call
    result = await svc.inventory.check_stock(item_id="item-1")
    assert result == {"stock": 42}
    svc.call_rpc.assert_awaited_once_with(
        "inventory_service", "check_stock", namespace=None, item_id="item-1"
    )

    cb = svc.inventory.circuit_breaker
    assert cb.state == CLOSED
    assert cb.failure_count == 0

    # Induce 2 failures
    svc.call_rpc.side_effect = RPCTimeoutError("timeout calling inventory")
    for _ in range(2):
        with pytest.raises(RPCTimeoutError):
            await svc.inventory.check_stock(item_id="item-1")

    assert cb.state == OPEN

    # Third call fails fast without invoking call_rpc
    svc.call_rpc.reset_mock()
    with pytest.raises(RpcCircuitOpenError):
        await svc.inventory.check_stock(item_id="item-1")

    svc.call_rpc.assert_not_called()


@pytest.mark.unit
def test_resilient_rpc_proxy_call_async_fails_when_open():
    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    svc.inventory.circuit_breaker.trip()

    with pytest.raises(RpcCircuitOpenError):
        svc.inventory.check_stock.call_async(item_id="item-1")


@pytest.mark.unit
def test_resilient_rpc_proxy_call_async_returns_coroutine_when_closed():
    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    dummy_coro = object()
    svc.call_rpc_no_wait = MagicMock(return_value=dummy_coro)

    assert svc.inventory.circuit_breaker.state == CLOSED
    coro = svc.inventory.check_stock.call_async(item_id="item-1", count=5)

    svc.call_rpc_no_wait.assert_called_once_with(
        "inventory_service", "check_stock", namespace=None, item_id="item-1", count=5
    )
    assert coro is dummy_coro


# ============================================================================
# Rate Limiter Unit Tests (InMemory & KV)
# ============================================================================


@pytest.mark.unit
async def test_in_memory_rate_limiter_sliding_window():
    limiter = InMemoryRateLimiter()
    key = "user_1"

    # Allow up to 3 calls in 0.05s window
    assert await limiter.acquire(key, calls=3, window=0.05) is True
    assert await limiter.acquire(key, calls=3, window=0.05) is True
    assert await limiter.acquire(key, calls=3, window=0.05) is True

    # 4th call exceeded
    assert await limiter.acquire(key, calls=3, window=0.05) is False

    retry_after = await limiter.get_retry_after(key, window=0.05)
    assert retry_after > 0.0

    # Wait for window to slide
    await asyncio.sleep(0.06)

    # Allowed again
    assert await limiter.acquire(key, calls=3, window=0.05) is True


@pytest.mark.unit
async def test_in_memory_rate_limiter_reset():
    limiter = InMemoryRateLimiter()
    await limiter.acquire("k1", calls=1, window=10.0)
    assert await limiter.acquire("k1", calls=1, window=10.0) is False

    await limiter.reset("k1")
    assert await limiter.acquire("k1", calls=1, window=10.0) is True


@dataclass
class _MockKvEntry:
    value: bytes
    revision: int


class _MockKvStore:
    """Mock NATS KV store implementing get, create, update, delete with CAS."""

    def __init__(self):
        self.store: dict[str, _MockKvEntry] = {}
        self.rev_counter = 1

    async def get(self, key: str) -> _MockKvEntry:
        if key not in self.store:
            raise KeyError(key)
        return self.store[key]

    async def create(self, key: str, value: bytes) -> int:
        if key in self.store:
            raise ValueError("key already exists")
        entry = _MockKvEntry(value=value, revision=self.rev_counter)
        self.rev_counter += 1
        self.store[key] = entry
        return entry.revision

    async def update(self, key: str, value: bytes, last: int) -> int:
        if key not in self.store:
            raise KeyError(key)
        if self.store[key].revision != last:
            raise ValueError("CAS mismatch")
        entry = _MockKvEntry(value=value, revision=self.rev_counter)
        self.rev_counter += 1
        self.store[key] = entry
        return entry.revision

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


@pytest.mark.unit
async def test_kv_rate_limiter_distributed_sliding_window():
    mock_kv = _MockKvStore()
    limiter = KvRateLimiter(kv=mock_kv, bucket_name="rate_limits")

    key = "tenant_xyz:handler"
    # Allow 2 calls in 0.05s window
    assert await limiter.acquire(key, calls=2, window=0.05) is True
    assert await limiter.acquire(key, calls=2, window=0.05) is True
    assert await limiter.acquire(key, calls=2, window=0.05) is False

    # Check store has safe key
    safe_key = limiter._safe_key(key)
    entry = await mock_kv.get(safe_key)
    timestamps = json.loads(entry.value.decode())
    assert len(timestamps) == 2

    # Wait for window to expire
    await asyncio.sleep(0.06)
    assert await limiter.acquire(key, calls=2, window=0.05) is True


@pytest.mark.unit
async def test_kv_rate_limiter_fallback_to_in_memory():
    mock_kv = MagicMock()
    mock_kv.get = AsyncMock(side_effect=ConnectionError("NATS disconnected"))

    limiter = KvRateLimiter(kv=mock_kv, in_memory_fallback=True)

    # Should not raise, falls back to in-memory limiter
    assert await limiter.acquire("key1", calls=2, window=10.0) is True
    assert await limiter.acquire("key1", calls=2, window=10.0) is True
    assert await limiter.acquire("key1", calls=2, window=10.0) is False


# ============================================================================
# @rate_limit Decorator Tests
# ============================================================================


@pytest.mark.unit
async def test_rate_limit_decorator_standalone():
    @rate_limit(calls=2, window=0.05)
    async def greet(name: str) -> str:
        return f"hello {name}"

    assert await greet("alice") == "hello alice"
    assert await greet("bob") == "hello bob"

    with pytest.raises(RateLimitExceeded) as exc_info:
        await greet("charlie")

    assert "rate limit exceeded" in str(exc_info.value)

    # Window expires
    await asyncio.sleep(0.06)
    assert await greet("dave") == "hello dave"


@pytest.mark.unit
async def test_rate_limit_decorator_metadata_attachment():
    @rate_limit(calls=10, window=60.0, key="client_ip")
    @rpc
    async def my_endpoint(data: dict) -> dict:
        return data

    assert hasattr(my_endpoint, "_cliffracer_rate_limit")
    assert hasattr(my_endpoint, "_cliffracer_rpc")
    config = my_endpoint._cliffracer_rate_limit
    assert config.calls == 10
    assert config.window == 60.0
    assert config.key == "client_ip"


# ============================================================================
# ResilienceExtension & Wire Error Translation Tests
# ============================================================================


@pytest.mark.unit
async def test_resilience_extension_allows_calls_within_limit():
    class OrdersService(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=5, window=10.0)
        async def ping(self) -> str:
            return "pong"

    svc = OrdersService(ServiceConfig(name="orders"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _rpc_msg("orders.rpc.ping", {})
    await svc.container._handle_rpc_request(msg)

    replies = _get_replies(msg)
    assert len(replies) == 1
    assert replies[0].get("result") == "pong"


@pytest.mark.unit
async def test_resilience_extension_rejects_exceeded_requests_with_wire_refusal():
    """Verify that when a rate limit is exceeded, ResilienceExtension raises
    RateLimitExceeded in worker_setup, skipping the handler, and the container
    replies over the wire with {"error": "refused: rate limit exceeded"}.
    """
    handler_executed = 0

    class RateLimitedService(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=2, window=10.0)
        async def work(self) -> str:
            nonlocal handler_executed
            handler_executed += 1
            return "done"

    svc = RateLimitedService(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    # Call 1: permitted
    msg1 = _rpc_msg("svc.rpc.work", {})
    await svc.container._handle_rpc_request(msg1)
    rep1 = _get_replies(msg1)
    assert rep1[0].get("result") == "done"
    assert handler_executed == 1

    # Call 2: permitted
    msg2 = _rpc_msg("svc.rpc.work", {})
    await svc.container._handle_rpc_request(msg2)
    rep2 = _get_replies(msg2)
    assert rep2[0].get("result") == "done"
    assert handler_executed == 2

    # Call 3: rate limit exceeded -> wire rejection
    msg3 = _rpc_msg("svc.rpc.work", {})
    await svc.container._handle_rpc_request(msg3)
    rep3 = _get_replies(msg3)
    assert len(rep3) == 1
    assert handler_executed == 2, "Handler MUST NOT execute when rate limit is exceeded"

    wire_response = rep3[0]
    assert "error" in wire_response
    assert wire_response["error"] == "refused: rate limit exceeded"
    assert "correlation_id" in wire_response


@pytest.mark.unit
async def test_resilience_extension_partitioned_by_key():
    class MultiTenantService(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=1, window=10.0, key=lambda ctx: ctx.payload.get("tenant"))
        async def tenant_op(self, tenant: str) -> str:
            return f"ok-{tenant}"

    svc = MultiTenantService(ServiceConfig(name="mt"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    # Tenant A call 1 -> OK
    msg_a1 = _rpc_msg("mt.rpc.tenant_op", {"tenant": "A"})
    await svc.container._handle_rpc_request(msg_a1)
    assert _get_replies(msg_a1)[0].get("result") == "ok-A"

    # Tenant B call 1 -> OK (different partition)
    msg_b1 = _rpc_msg("mt.rpc.tenant_op", {"tenant": "B"})
    await svc.container._handle_rpc_request(msg_b1)
    assert _get_replies(msg_b1)[0].get("result") == "ok-B"

    # Tenant A call 2 -> Exceeded
    msg_a2 = _rpc_msg("mt.rpc.tenant_op", {"tenant": "A"})
    await svc.container._handle_rpc_request(msg_a2)
    assert _get_replies(msg_a2)[0]["error"] == "refused: rate limit exceeded"

    # Tenant B call 2 -> Exceeded
    msg_b2 = _rpc_msg("mt.rpc.tenant_op", {"tenant": "B"})
    await svc.container._handle_rpc_request(msg_b2)
    assert _get_replies(msg_b2)[0]["error"] == "refused: rate limit exceeded"


@pytest.mark.unit
async def test_resilience_extension_sliding_window_replenishes():
    class FastService(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=1, window=0.05)
        async def tick(self) -> str:
            return "tock"

    svc = FastService(ServiceConfig(name="fast"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    m1 = _rpc_msg("fast.rpc.tick", {})
    await svc.container._handle_rpc_request(m1)
    assert _get_replies(m1)[0].get("result") == "tock"

    # Blocked immediately
    m2 = _rpc_msg("fast.rpc.tick", {})
    await svc.container._handle_rpc_request(m2)
    assert _get_replies(m2)[0]["error"] == "refused: rate limit exceeded"

    # Wait for window to elapse
    await asyncio.sleep(0.06)

    # Allowed again
    m3 = _rpc_msg("fast.rpc.tick", {})
    await svc.container._handle_rpc_request(m3)
    assert _get_replies(m3)[0].get("result") == "tock"


@pytest.mark.unit
def test_resilient_rpc_proxy_call_async_preserves_half_open_state():
    """Fire-and-forget calls do not reset HALF_OPEN circuit state."""

    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    cb = svc.inventory.circuit_breaker
    cb._state = CircuitState.HALF_OPEN
    dummy_coro = object()
    svc.call_rpc_no_wait = MagicMock(return_value=dummy_coro)

    coro = svc.inventory.check_stock.call_async(item_id="item-1")
    assert coro is dummy_coro
    # Circuit state must remain HALF_OPEN; must not reset to CLOSED
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.success_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resilient_rpc_proxy_call_async_awaitable_and_zero_warnings():
    """Awaiting call_async coroutine executes without unawaited warnings."""
    import warnings

    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    called = False

    async def mock_call_rpc_no_wait(*args, **kwargs):
        nonlocal called
        called = True

    svc.call_rpc_no_wait = mock_call_rpc_no_wait

    with warnings.catch_warnings(record=True) as recorded_warnings:
        warnings.simplefilter("always")
        coro = svc.inventory.check_stock.call_async(item_id="item-1")
        await coro

    assert called is True
    coroutine_warnings = [w for w in recorded_warnings if "coroutine" in str(w.message).lower()]
    assert len(coroutine_warnings) == 0


@pytest.mark.unit
def test_resilient_method_proxy_call_async_typing():
    """Inspect return type of ResilientMethodProxy.call_async."""
    import typing
    from collections.abc import Coroutine

    from cliffracer_resilience.circuit_breaker import ResilientMethodProxy

    hints = typing.get_type_hints(ResilientMethodProxy.call_async)
    assert hints["return"] == Coroutine[typing.Any, typing.Any, typing.Any]

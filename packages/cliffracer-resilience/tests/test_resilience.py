"""Comprehensive tests for cliffracer-resilience: Circuit Breaker and Rate Limiting."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import nats.js.errors
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
from cliffracer_resilience import rate_limiter as rate_limiter_module

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.exceptions import (
    RpcConnectionError,
    RPCError,
    RpcServerError,
    RPCTimeoutError,
)

pytestmark = pytest.mark.unit


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


class _LimiterClock:
    """The time the rate limiters read, moved by the test and by nothing else."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def limiter_clock(monkeypatch: pytest.MonkeyPatch) -> _LimiterClock:
    """Replace the `time` the limiter module reads, and only that module's.

    A window of 50 ms is a window the host can outrun: two calls the test means
    to be "immediately" apart land on opposite sides of it on a loaded runner,
    and the second is allowed. With the clock held by the test, how long the
    host takes between two calls no longer decides anything.
    """
    clock = _LimiterClock()
    monkeypatch.setattr(rate_limiter_module, "time", clock)
    return clock


# ============================================================================
# Circuit Breaker Unit Tests
# ============================================================================


def test_circuit_breaker_initial_state():
    cb = CircuitBreaker("test-service")
    assert cb.state == CLOSED
    assert cb.is_closed is True
    assert cb.is_open is False
    assert cb.is_half_open is False
    assert cb.failure_count == 0
    assert cb.success_count == 0


async def test_circuit_breaker_success_keeps_closed():
    cb = CircuitBreaker("test-service")
    async with cb:
        pass
    assert cb.state == CLOSED
    assert cb.success_count == 1
    assert cb.failure_count == 0


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

    with pytest.raises(RpcCircuitOpenError) as exc_info:
        await cb.call(mock_remote_call)

    assert network_called is False, "No network call must be made when circuit is OPEN"
    assert "payment-service" in str(exc_info.value)
    assert exc_info.value.details.get("state") == "open"


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


async def test_circuit_breaker_half_open_probe_failure_trips_to_open():
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05)
    cb = CircuitBreaker("test-service", config=config)

    with pytest.raises(RPCTimeoutError):
        async with cb:
            raise RPCTimeoutError("timeout")

    await asyncio.sleep(0.06)
    assert cb.state == HALF_OPEN

    # Probe call fails
    with pytest.raises(RpcServerError):
        async with cb:
            raise RpcServerError("service unavailable")

    # Trips immediately back to OPEN with renewed cooldown
    assert cb.state == OPEN
    assert cb.is_open is True


@pytest.mark.parametrize("half_open_max_calls", [1, 3])
async def test_circuit_breaker_half_open_admits_exactly_the_configured_probes(half_open_max_calls):
    """The knob, at a value that is not its default: with 3, three probes enter and the fourth is
    refused. 1 is the default, and a hard-coded `>= 1` satisfies it."""
    config = CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=half_open_max_calls
    )
    cb = CircuitBreaker("test-service", config=config)
    cb.trip()
    await asyncio.sleep(0.06)
    assert cb.state == HALF_OPEN

    entered = 0
    try:
        for _ in range(half_open_max_calls):
            await cb.__aenter__()
            entered += 1
        with pytest.raises(RpcCircuitOpenError) as exc_info:
            async with cb:
                pass
        assert "HALF-OPEN" in str(exc_info.value)
    finally:
        for _ in range(entered):
            await cb.__aexit__(None, None, None)


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
    with pytest.raises(RpcConnectionError):
        async with cb:
            raise RpcConnectionError("broker unreachable")

    assert cb.failure_count == 1

    with pytest.raises(RpcServerError):
        async with cb:
            raise RpcServerError("handler raised")

    assert cb.failure_count == 2
    assert cb.state == OPEN


def test_circuit_breaker_manual_trip_and_reset():
    cb = CircuitBreaker("test-service")
    cb.trip()
    assert cb.state == OPEN
    cb.reset()
    assert cb.state == CLOSED
    assert cb.failure_count == 0


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


def test_resilient_rpc_proxy_call_async_fails_when_open():
    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    svc.inventory.circuit_breaker.trip()

    with pytest.raises(RpcCircuitOpenError):
        svc.inventory.check_stock.call_async(item_id="item-1")


def test_resilient_rpc_proxy_call_async_passes_the_dispatch_through_when_closed():
    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    dummy_coro = object()
    svc.call_async = MagicMock(return_value=dummy_coro)

    assert svc.inventory.circuit_breaker.state == CLOSED
    coro = svc.inventory.check_stock.call_async(item_id="item-1", count=5)

    svc.call_async.assert_called_once_with(
        "inventory_service", "check_stock", namespace=None, item_id="item-1", count=5
    )
    assert coro is dummy_coro


# ============================================================================
# Rate Limiter Unit Tests (InMemory & KV)
# ============================================================================


async def test_in_memory_rate_limiter_sliding_window(limiter_clock):
    limiter = InMemoryRateLimiter()
    key = "user_1"

    # Allow up to 3 calls in 0.05s window
    assert await limiter.acquire(key, calls=3, window=0.05) is True
    assert await limiter.acquire(key, calls=3, window=0.05) is True
    assert await limiter.acquire(key, calls=3, window=0.05) is True

    # 4th call exceeded
    assert await limiter.acquire(key, calls=3, window=0.05) is False

    retry_after = await limiter.get_retry_after(key, window=0.05)
    assert retry_after == pytest.approx(0.05)

    # Still inside the window: still refused, and the wait has shrunk
    limiter_clock.advance(0.04)
    assert await limiter.acquire(key, calls=3, window=0.05) is False
    assert await limiter.get_retry_after(key, window=0.05) == pytest.approx(0.01)

    # The window slides past the first three calls
    limiter_clock.advance(0.02)

    # Nothing is in flight now, so there is nothing to wait for: 0.0 says "retry now", where a
    # positive number says "wait". A function that always answered a large number fails here.
    assert await limiter.get_retry_after(key, window=0.05) == 0.0

    # Allowed again
    assert await limiter.acquire(key, calls=3, window=0.05) is True


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
            raise nats.js.errors.KeyNotFoundError()
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


async def test_kv_rate_limiter_distributed_sliding_window(limiter_clock):
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

    # Move past the window
    limiter_clock.advance(0.06)
    assert await limiter.acquire(key, calls=2, window=0.05) is True


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


async def test_rate_limit_decorator_standalone(limiter_clock):
    @rate_limit(calls=2, window=0.05)
    async def greet(name: str) -> str:
        return f"hello {name}"

    assert await greet("alice") == "hello alice"
    assert await greet("bob") == "hello bob"

    with pytest.raises(RateLimitExceeded) as exc_info:
        await greet("charlie")

    assert "rate limit exceeded" in str(exc_info.value)

    # Window expires
    limiter_clock.advance(0.06)
    assert await greet("dave") == "hello dave"


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


async def test_resilience_extension_sliding_window_replenishes(limiter_clock):
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

    # Move past the window
    limiter_clock.advance(0.06)

    # Allowed again
    m3 = _rpc_msg("fast.rpc.tick", {})
    await svc.container._handle_rpc_request(m3)
    assert _get_replies(m3)[0].get("result") == "tock"


async def test_CONTROL_real_time_passing_between_calls_does_not_open_the_window(limiter_clock):
    """The straddle is impossible, not merely unlikely.

    The same two calls as above with 60 ms of REAL time, longer than the 50 ms
    window, between them. The limiter reads the test's clock, which has not
    moved, so the second call is still refused. Under the wall clock it would be
    allowed, which is what reddened CI when a loaded host took that long.
    """

    class FastService(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=1, window=0.05)
        async def tick(self) -> str:
            return "tock"

    svc = FastService(ServiceConfig(name="fast"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    first = _rpc_msg("fast.rpc.tick", {})
    await svc.container._handle_rpc_request(first)
    assert _get_replies(first)[0].get("result") == "tock"

    await asyncio.sleep(0.06)

    second = _rpc_msg("fast.rpc.tick", {})
    await svc.container._handle_rpc_request(second)
    assert _get_replies(second)[0]["error"] == "refused: rate limit exceeded"


def test_resilient_rpc_proxy_call_async_preserves_half_open_state():
    """Fire-and-forget calls do not reset HALF_OPEN circuit state."""

    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    cb = svc.inventory.circuit_breaker
    cb._state = CircuitState.HALF_OPEN
    dummy_coro = object()
    svc.call_async = MagicMock(return_value=dummy_coro)

    coro = svc.inventory.check_stock.call_async(item_id="item-1")
    assert coro is dummy_coro
    # Circuit state must remain HALF_OPEN; must not reset to CLOSED
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.success_count == 0


@pytest.mark.asyncio
async def test_resilient_rpc_proxy_call_async_awaitable_and_zero_warnings():
    """Awaiting call_async coroutine executes without unawaited warnings."""
    import warnings

    class TestService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = TestService(ServiceConfig(name="test_svc"))
    called = False

    async def mock_call_async(*args, **kwargs):
        nonlocal called
        called = True

    svc.call_async = mock_call_async

    with warnings.catch_warnings(record=True) as recorded_warnings:
        warnings.simplefilter("always")
        coro = svc.inventory.check_stock.call_async(item_id="item-1")
        await coro

    assert called is True
    coroutine_warnings = [w for w in recorded_warnings if "coroutine" in str(w.message).lower()]
    assert len(coroutine_warnings) == 0


async def test_circuit_breaker_custom_monitored_exceptions_strictly_honored():
    """Verify custom monitored_exceptions excludes RpcError when configured."""
    config = CircuitBreakerConfig(
        monitored_exceptions=[ConnectionError],
        failure_threshold=1,
    )
    cb = CircuitBreaker("custom-cb", config=config)

    # RpcError is not in monitored_exceptions; breaker must remain CLOSED
    with pytest.raises(RPCError):
        async with cb:
            raise RPCError("RPC Error: validation failure")

    assert cb.failure_count == 0
    assert cb.state == CLOSED

    # The builtin ConnectionError is monitored; trips breaker to OPEN
    with pytest.raises(ConnectionError):
        async with cb:
            raise ConnectionError("connection dropped")

    assert cb.failure_count == 1
    assert cb.state == OPEN


async def test_in_memory_rate_limiter_prunes_expired_tokens_and_empty_keys():
    """Verify expired timestamps and empty key queues are pruned from memory."""
    limiter = InMemoryRateLimiter()

    # get_retry_after on non-existent key must not allocate empty queue
    retry_after = await limiter.get_retry_after("nonexistent", window=1.0)
    assert retry_after == 0.0
    assert "nonexistent" not in limiter._windows

    # Acquire and let window expire
    assert await limiter.acquire("k1", calls=2, window=0.04) is True
    assert await limiter.acquire("k1", calls=2, window=0.04) is True
    assert "k1" in limiter._windows

    await asyncio.sleep(0.05)

    # prune_expired removes expired key from _windows
    pruned = await limiter.prune_expired(window=0.04)
    assert pruned >= 1
    assert "k1" not in limiter._windows

    # Acquire again succeeds with clean queue
    assert await limiter.acquire("k1", calls=2, window=0.04) is True


async def test_kv_rate_limiter_safe_key_sanitization_and_nats_safety():
    """Verify _safe_key produces valid NATS KV keys without empty subject tokens."""
    limiter = KvRateLimiter()

    # Pathological keys that would break NATS subject parsing
    keys = [".", "..", "...", ".lead", "trail.", "a..b", "a...b", "", "x" * 300]
    for raw_key in keys:
        safe = limiter._safe_key(raw_key)
        assert safe, f"Empty safe key for {raw_key!r}"
        assert not safe.startswith("."), f"Leading dot in safe key: {safe}"
        assert not safe.endswith("."), f"Trailing dot in safe key: {safe}"
        assert ".." not in safe, f"Consecutive dots in safe key: {safe}"
        assert len(safe) <= 128, f"Key exceeds maximum length: {safe}"


async def test_two_partition_keys_never_share_one_kv_key():
    """Shape rules cannot catch the failure that matters here.

    The partition key comes off the wire. Sanitising `.lead`, `trail.` and
    `a..b` used to yield `lead`, `trail` and `a.b` -- the same keys their
    already-clean neighbours produce -- so a caller that picked its own key
    could spend another tenant's budget by choosing one that sanitised onto
    it. Every assertion about dots and lengths still passed while that was
    true.
    """
    limiter = KvRateLimiter()
    colliding_pairs = [
        (".lead", "lead"),
        ("trail.", "trail"),
        ("a..b", "a.b"),
        ("tenant a", "tenant_a"),
    ]

    for left, right in colliding_pairs:
        assert limiter._safe_key(left) != limiter._safe_key(right), (
            f"{left!r} and {right!r} share a KV key, and so share a budget"
        )

    many = [".lead", "lead", "trail.", "trail", "a..b", "a.b", ".", "..", "", "x" * 300]
    mapped = [limiter._safe_key(k) for k in many]
    assert len(set(mapped)) == len(set(many)), "distinct keys must stay distinct"
    assert all(len(k) <= 128 for k in mapped), "and stay inside the NATS KV limit"


async def test_keys_that_used_to_collide_get_their_own_budget():
    """Read through the store rather than off the key function.

    `_safe_key` returning two strings proves nothing on its own if `acquire`
    reaches the store by some other route. This spends one key's whole
    allowance and then asserts the other key is still allowed, and that the
    store holds an entry apiece.
    """
    mock_kv = _MockKvStore()
    limiter = KvRateLimiter(kv=mock_kv, bucket_name="rate_limits")

    assert await limiter.acquire(".lead", calls=1, window=60.0) is True
    assert await limiter.acquire(".lead", calls=1, window=60.0) is False, "budget spent"

    assert await limiter.acquire("lead", calls=1, window=60.0) is True, (
        "a different partition key has its own budget"
    )

    assert len(mock_kv.store) == 2, f"one entry per key, got {sorted(mock_kv.store)}"


async def test_kv_rate_limiter_prunes_expired_tokens_on_rejection():
    """Verify expired timestamps in KV store are pruned when request is rejected."""
    mock_kv = _MockKvStore()
    limiter = KvRateLimiter(kv=mock_kv, bucket_name="rate_limits")

    # Seed an entry with two expired timestamps and one fresh timestamp
    safe_key = limiter._safe_key("tenant:api")
    now = time.time()
    old_ts = [now - 10.0, now - 9.0, now - 0.01]
    await mock_kv.create(safe_key, json.dumps(old_ts).encode())

    # Limit is 1 call per 1.0 second. Active count is 1, so acquiring 1 call should reject.
    allowed = await limiter.acquire("tenant:api", calls=1, window=1.0)
    assert allowed is False

    # Stored entry should have had the two expired timestamps pruned
    entry = await mock_kv.get(safe_key)
    stored_ts = json.loads(entry.value.decode())
    assert len(stored_ts) == 1
    assert stored_ts[0] == old_ts[2]

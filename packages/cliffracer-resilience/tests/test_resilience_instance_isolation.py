"""Unit tests verifying state isolation across service instances for cliffracer-resilience.

Ensures that:
1. Two service instances with ResilienceExtension do not share InMemoryRateLimiter instances.
2. Rate limit exhaustion on instance 1 does not affect instance 2.
3. ResilientRpcProxy circuit breaker state is isolated per service instance.
4. Explicit custom limiter or circuit breaker instances are preserved if provided.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import (
    CircuitBreaker,
    InMemoryRateLimiter,
    ResilienceExtension,
    ResilientRpcProxy,
    rate_limit,
)

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


def _rpc_msg(subject: str, data: dict, headers: dict | None = None) -> AsyncMock:
    """Helper to build a mock incoming NATS message."""
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data).encode()
    m.headers = headers or {}
    m.reply = "_INBOX.test_isolation"
    return m


def _get_replies(msg: AsyncMock) -> list[dict]:
    """Helper to extract JSON decoded replies from msg.respond."""
    return [json.loads(c.args[0].decode()) for c in msg.respond.await_args_list]


async def test_rate_limiter_not_shared_across_service_instances():
    """Verify that 2 service instances of a class with ResilienceExtension do not share

    InMemoryRateLimiter instances or state.
    """

    class IsolatedService(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=1, window=10.0)
        async def execute_task(self) -> str:
            return "done"

    s1 = IsolatedService(ServiceConfig(name="service_a"))
    await s1.container._setup_extensions()
    s1._discover_handlers()

    s2 = IsolatedService(ServiceConfig(name="service_b"))
    await s2.container._setup_extensions()
    s2._discover_handlers()

    # Verify instance isolation
    assert s1.resilience is not s2.resilience
    assert s1.resilience.limiter is not s2.resilience.limiter
    assert s1.resilience._rate_limits is not s2.resilience._rate_limits
    assert s1.resilience._event_rate_limits is not s2.resilience._event_rate_limits
    assert isinstance(s1.resilience.limiter, InMemoryRateLimiter)
    assert isinstance(s2.resilience.limiter, InMemoryRateLimiter)

    # Call 1 on instance 1: allowed
    msg1 = _rpc_msg("service_a.rpc.execute_task", {})
    await s1.container._handle_rpc_request(msg1)
    rep1 = _get_replies(msg1)
    assert len(rep1) == 1
    assert rep1[0].get("result") == "done"

    # Call 2 on instance 1: rate limit exhausted
    msg2 = _rpc_msg("service_a.rpc.execute_task", {})
    await s1.container._handle_rpc_request(msg2)
    rep2 = _get_replies(msg2)
    assert len(rep2) == 1
    assert rep2[0].get("error") == "refused: rate limit exceeded"

    # Call 1 on instance 2: MUST SUCCEED (not affected by instance 1 exhaustion)
    msg3 = _rpc_msg("service_b.rpc.execute_task", {})
    await s2.container._handle_rpc_request(msg3)
    rep3 = _get_replies(msg3)
    assert len(rep3) == 1
    assert rep3[0].get("result") == "done", (
        "Instance 2's rate limit was prematurely exhausted by instance 1"
    )

    # Call 2 on instance 2: now instance 2 is exhausted
    msg4 = _rpc_msg("service_b.rpc.execute_task", {})
    await s2.container._handle_rpc_request(msg4)
    rep4 = _get_replies(msg4)
    assert len(rep4) == 1
    assert rep4[0].get("error") == "refused: rate limit exceeded"


async def test_custom_rate_limiter_preserved_when_explicitly_configured():
    """Verify that if a custom RateLimiter is explicitly provided to ResilienceExtension,

    it is preserved and reused across instances as configured by the user.
    """
    shared_limiter = InMemoryRateLimiter()

    class CustomLimiterService(CliffracerService):
        resilience = ResilienceExtension(limiter=shared_limiter)

        @rpc
        @rate_limit(calls=1, window=10.0)
        async def execute_task(self) -> str:
            return "ok"

    s1 = CustomLimiterService(ServiceConfig(name="custom_a"))
    await s1.container._setup_extensions()
    s1._discover_handlers()

    s2 = CustomLimiterService(ServiceConfig(name="custom_b"))
    await s2.container._setup_extensions()
    s2._discover_handlers()

    assert s1.resilience.limiter is shared_limiter
    assert s2.resilience.limiter is shared_limiter
    assert s1.resilience.limiter is s2.resilience.limiter


def test_resilient_rpc_proxy_circuit_breaker_isolation_across_instances():
    """Verify that ResilientRpcProxy creates per-instance CircuitBreaker instances."""

    class ProxyTestService(CliffracerService):
        downstream = ResilientRpcProxy("downstream_service")

    s1 = ProxyTestService(ServiceConfig(name="proxy_svc_1"))
    s2 = ProxyTestService(ServiceConfig(name="proxy_svc_2"))

    cb1 = s1.downstream.circuit_breaker
    cb2 = s2.downstream.circuit_breaker

    assert cb1 is not cb2
    assert cb1.is_closed is True
    assert cb2.is_closed is True

    # Tripping cb1 on instance 1 must not affect instance 2
    cb1.trip()
    assert cb1.is_open is True
    assert cb2.is_open is False
    assert cb2.is_closed is True


def test_resilient_rpc_proxy_custom_circuit_breaker_preserved():
    """Verify that explicitly passing a custom CircuitBreaker to ResilientRpcProxy is preserved."""
    shared_cb = CircuitBreaker("custom_downstream")

    class CustomProxyService(CliffracerService):
        downstream = ResilientRpcProxy("custom_downstream", circuit_breaker=shared_cb)

    s1 = CustomProxyService(ServiceConfig(name="custom_proxy_1"))
    s2 = CustomProxyService(ServiceConfig(name="custom_proxy_2"))

    assert s1.downstream.circuit_breaker is shared_cb
    assert s2.downstream.circuit_breaker is shared_cb

"""Cliffracer Resilience: Circuit Breaking and Rate Limiting extension."""

from __future__ import annotations

from cliffracer_resilience.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
    ResilientMethodProxy,
    ResilientRpcProxy,
    ResilientServiceProxy,
    RpcCircuitOpenError,
)
from cliffracer_resilience.extension import ResilienceExtension
from cliffracer_resilience.rate_limiter import (
    InMemoryRateLimiter,
    KvRateLimiter,
    RateLimitConfig,
    RateLimiter,
    RateLimitExceeded,
    rate_limit,
)

__all__ = [
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "CircuitState",
    "InMemoryRateLimiter",
    "KvRateLimiter",
    "RateLimitConfig",
    "RateLimitExceeded",
    "RateLimiter",
    "ResilienceExtension",
    "ResilientMethodProxy",
    "ResilientRpcProxy",
    "ResilientServiceProxy",
    "RpcCircuitOpenError",
    "rate_limit",
]

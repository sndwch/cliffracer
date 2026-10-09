"""Cliffracer Resilience: Circuit Breaking and Rate Limiting extension."""

from __future__ import annotations

from cliffracer_resilience.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
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
    RateLimiterUnavailableError,
    RateLimitExceeded,
    RateLimitKeyError,
    rate_limit,
)

__all__ = [
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerRegistry",
    "CircuitState",
    "InMemoryRateLimiter",
    "KvRateLimiter",
    "RateLimitConfig",
    "RateLimitKeyError",
    "RateLimitExceeded",
    "RateLimiter",
    "RateLimiterUnavailableError",
    "ResilienceExtension",
    "ResilientMethodProxy",
    "ResilientRpcProxy",
    "ResilientServiceProxy",
    "RpcCircuitOpenError",
    "rate_limit",
]

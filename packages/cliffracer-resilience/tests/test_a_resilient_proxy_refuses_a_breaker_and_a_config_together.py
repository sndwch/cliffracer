"""`ResilientRpcProxy` takes a breaker or a config, and says so when given both.

A breaker brings its own thresholds, so `config=` next to `circuit_breaker=` was stored and never
read: the thresholds the caller wrote had no effect and nothing said so.
"""

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, ResilientRpcProxy

pytestmark = pytest.mark.unit


def test_both_are_refused_naming_the_two_arguments():
    with pytest.raises(ValueError, match=r"circuit_breaker= or config=, not both"):
        ResilientRpcProxy(
            "payments",
            circuit_breaker=CircuitBreaker("shared"),
            config=CircuitBreakerConfig(failure_threshold=2),
        )


def test_a_breaker_alone_is_kept():
    shared = CircuitBreaker("shared")

    assert ResilientRpcProxy("payments", circuit_breaker=shared).circuit_breaker is shared


def test_a_config_alone_is_kept():
    config = CircuitBreakerConfig(failure_threshold=2)

    assert ResilientRpcProxy("payments", config=config).config is config

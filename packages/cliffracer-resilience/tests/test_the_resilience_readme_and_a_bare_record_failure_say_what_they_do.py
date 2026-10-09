"""The README's list of what opens a circuit is the code's, and a bare `record_failure()` forgets nothing.

The README listed a builtin `ConnectionError` among the errors that count toward opening a circuit.
`DEFAULT_MONITORED_EXCEPTIONS` has the four `Rpc*` classes only (the entry predates the removal of
cliffracer's own `ConnectionError`), so a user who chose `monitored_exceptions` from the README was
protected against a case that is not one. And `record_failure()` with no exception replaced the last
recorded failure with `None`, which neither the property nor the docstring says.
"""

import re
from pathlib import Path

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, CircuitState
from cliffracer_resilience.circuit_breaker import DEFAULT_MONITORED_EXCEPTIONS

from cliffracer.core.exceptions import (
    RpcConnectionError,
    RpcNoRespondersError,
    RpcServerError,
    RpcTimeoutError,
)

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[1] / "README.md"


def test_the_default_list_is_the_four_rpc_classes():
    assert set(DEFAULT_MONITORED_EXCEPTIONS) == {
        RpcTimeoutError,
        RpcNoRespondersError,
        RpcConnectionError,
        RpcServerError,
    }


def test_the_readme_names_exactly_the_classes_that_count():
    text = README.read_text()
    section = re.search(r"\*\*What Counts\*\*: (.*?) A builtin", text, re.S)
    assert section, "the README no longer has the sentence this test reads"
    named = set(re.findall(r"`(\w+)`", section.group(1)))

    assert named == {cls.__name__ for cls in DEFAULT_MONITORED_EXCEPTIONS}


@pytest.mark.parametrize("builtin", [ConnectionError, TimeoutError])
async def test_a_builtin_connection_or_timeout_error_does_not_open_the_default_circuit(builtin):
    breaker = CircuitBreaker("backend", CircuitBreakerConfig(failure_threshold=3))

    for _ in range(3):
        with pytest.raises(builtin):
            async with breaker:
                raise builtin("down")

    assert breaker.state is CircuitState.CLOSED and breaker.failure_count == 0


async def test_CONTROL_an_rpc_connection_error_opens_it_and_a_builtin_named_in_the_config_does_too():
    default = CircuitBreaker("a", CircuitBreakerConfig(failure_threshold=3))
    for _ in range(3):
        with pytest.raises(RpcConnectionError):
            async with default:
                raise RpcConnectionError("down")
    named = CircuitBreaker(
        "b", CircuitBreakerConfig(failure_threshold=3, monitored_exceptions=(ConnectionError,))
    )
    for _ in range(3):
        with pytest.raises(ConnectionError):
            async with named:
                raise ConnectionError("down")

    assert default.state is CircuitState.OPEN and named.state is CircuitState.OPEN


async def test_a_bare_record_failure_keeps_the_last_recorded_failure():
    breaker = CircuitBreaker("backend", CircuitBreakerConfig(failure_threshold=5))
    boom = RpcServerError("boom")
    await breaker.record_failure(boom)

    await breaker.record_failure()

    assert breaker.last_failure is boom
    assert breaker.failure_count == 2, "the bare call still counts"


async def test_a_failure_with_an_exception_replaces_the_one_before_it():
    breaker = CircuitBreaker("backend", CircuitBreakerConfig(failure_threshold=5))
    await breaker.record_failure(RpcServerError("first"))
    second = RpcServerError("second")

    await breaker.record_failure(second)

    assert breaker.last_failure is second


async def test_a_bare_record_failure_on_a_fresh_breaker_leaves_no_last_failure():
    breaker = CircuitBreaker("backend", CircuitBreakerConfig(failure_threshold=5))

    await breaker.record_failure()

    assert breaker.last_failure is None and breaker.failure_count == 1


async def test_a_bare_record_failure_still_trips_the_circuit_at_the_threshold():
    breaker = CircuitBreaker("backend", CircuitBreakerConfig(failure_threshold=2))

    await breaker.record_failure()
    await breaker.record_failure()

    assert breaker.state is CircuitState.OPEN

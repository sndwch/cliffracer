"""A default circuit breaker opens for a dependency that is failing, not for a caller that is wrong.

`RpcError` is the root of the whole RPC hierarchy, and most of it means the
dependency ANSWERED: the arguments were invalid, the method does not exist, a
policy refused the message, the client is out of date. Opening a breaker on
those takes a healthy dependency offline for `recovery_timeout` because of a
fault on the calling side -- and a dependency that sheds load with this
package's rate limiter answers `RpcRefused`, so its own throttling would open
every caller's circuit. The errors that mean the dependency is unhealthy or
unreachable are the ones that should.

Each case runs the same loop: a breaker that opens after three failures, and
three calls that raise the error. The second group is the control: without it,
"stays closed" would also hold for a breaker that never opens.
"""

import pytest
from cliffracer_resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

from cliffracer.core.exceptions import (
    ClientOutOfDateError,
    RpcConnectionError,
    RpcNoRespondersError,
    RpcRefusedError,
    RpcServerError,
    RpcTimeoutError,
    RpcUnknownMethodError,
    RpcValidationError,
)

pytestmark = pytest.mark.unit

THRESHOLD = 3

THE_CALLER_IS_WRONG = [
    RpcValidationError(),
    RpcUnknownMethodError("no such method"),
    RpcRefusedError("rate limit exceeded"),
    ClientOutOfDateError("orders", ["create"], []),
]

THE_DEPENDENCY_IS_FAILING = [
    RpcTimeoutError("no reply"),
    RpcNoRespondersError("nothing subscribed"),
    RpcConnectionError("broker unreachable"),
    RpcServerError("handler raised"),
]


async def _failures(error: Exception) -> CircuitBreaker:
    breaker = CircuitBreaker(
        "orders", CircuitBreakerConfig(failure_threshold=THRESHOLD, recovery_timeout=30.0)
    )

    async def fails() -> None:
        raise error

    for _ in range(THRESHOLD):
        with pytest.raises(type(error)):
            await breaker.call(fails)
    return breaker


@pytest.mark.parametrize("error", THE_CALLER_IS_WRONG, ids=lambda e: type(e).__name__)
async def test_an_error_that_blames_the_caller_does_not_open_the_circuit(error):
    breaker = await _failures(error)

    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


@pytest.mark.parametrize("error", THE_DEPENDENCY_IS_FAILING, ids=lambda e: type(e).__name__)
async def test_CONTROL_an_error_that_blames_the_dependency_opens_the_circuit(error):
    breaker = await _failures(error)

    assert breaker.state == CircuitState.OPEN

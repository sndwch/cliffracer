"""`RpcCircuitOpenError.details` names the same keys whichever path refused the call.

`call_async` raised it with `{"service", "state"}` while the awaited path raised it with `{"name",
"state", "failure_count", "recovery_timeout"}`, so `exc.details["name"]` worked or raised
`KeyError` depending on how the call was made.
"""

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, RpcCircuitOpenError
from cliffracer_resilience.circuit_breaker import ResilientMethodProxy

pytestmark = pytest.mark.unit


class Owner:
    pass


async def _open() -> CircuitBreaker:
    breaker = CircuitBreaker(
        "inventory", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=30.0)
    )
    await breaker.record_failure()
    await breaker.record_failure()
    return breaker


async def test_call_async_and_the_awaited_path_refuse_with_the_same_details():
    breaker = await _open()
    owner = Owner()
    proxy = ResilientMethodProxy(owner, "inventory_service", "check", circuit_breaker=breaker)

    with pytest.raises(RpcCircuitOpenError) as fire_and_forget:
        proxy.call_async()
    with pytest.raises(RpcCircuitOpenError) as awaited:
        async with breaker:
            pass

    assert fire_and_forget.value.details == awaited.value.details
    assert awaited.value.details == {
        "name": "inventory",
        "state": "open",
        "failure_count": 2,
        "recovery_timeout": 30.0,
    }
    assert str(fire_and_forget.value) == str(awaited.value)

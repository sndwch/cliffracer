"""`call_async` is refused while a circuit is OPEN and admitted without limit while it is HALF_OPEN.

The behaviour the docstring and the README state: a fire-and-forget call has no reply to decide a
probe, so it takes no probe slot and `half_open_max_calls` bounds the awaited calls only.
"""

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, RpcCircuitOpenError
from cliffracer_resilience.circuit_breaker import CircuitState, ResilientMethodProxy

pytestmark = pytest.mark.unit

OPENED_AT = 1000.0


class Clock:
    now = OPENED_AT

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    fake.now = OPENED_AT
    monkeypatch.setattr("cliffracer_resilience.circuit_breaker.time", fake)
    return fake


def _proxy(breaker: CircuitBreaker) -> ResilientMethodProxy:
    class Service:
        async def call_async(self, service, method, *, namespace=None, **kwargs):
            return "sent"

    service = Service()
    proxy = ResilientMethodProxy(service, "inventory", "check", circuit_breaker=breaker)
    proxy._service_keepalive = service  # type: ignore[attr-defined]  # the proxy holds it weakly
    return proxy


async def _opened(clock: Clock, *, half_open_max_calls: int = 1) -> CircuitBreaker:
    breaker = CircuitBreaker(
        "inventory",
        CircuitBreakerConfig(
            failure_threshold=1, recovery_timeout=30.0, half_open_max_calls=half_open_max_calls
        ),
    )
    await breaker.record_failure()
    return breaker


async def test_call_async_is_refused_while_the_circuit_is_open(clock):
    breaker = await _opened(clock)
    assert breaker.state == CircuitState.OPEN

    with pytest.raises(RpcCircuitOpenError):
        _proxy(breaker).call_async()


async def test_call_async_is_admitted_without_limit_while_the_circuit_is_half_open(clock):
    breaker = await _opened(clock, half_open_max_calls=1)
    clock.now = OPENED_AT + 31.0
    assert breaker.state == CircuitState.HALF_OPEN

    sent = [_proxy(breaker).call_async() for _ in range(25)]

    assert len(sent) == 25
    for coroutine in sent:
        assert await coroutine == "sent"
    assert breaker._half_open_calls == 0, "none of them took a probe slot"


async def test_an_awaited_call_is_still_held_to_the_probe_budget_while_half_open(clock):
    """The budget the fire-and-forget calls are exempt from, still bounding the awaited ones."""
    breaker = await _opened(clock, half_open_max_calls=1)
    clock.now = OPENED_AT + 31.0

    async with breaker:
        with pytest.raises(RpcCircuitOpenError):
            async with breaker:
                pass

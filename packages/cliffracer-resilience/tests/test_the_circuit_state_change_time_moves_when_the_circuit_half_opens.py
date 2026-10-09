"""`last_state_change` is when the circuit last changed state, including going half-open.

An OPEN circuit whose cooldown has passed is HALF_OPEN: the `state` property says so before any call
arrives, and the first call admitted as a probe makes it the stored state. Neither moved the
timestamp, so a breaker reported the moment it opened for as long as it sat half-open.
"""

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig
from cliffracer_resilience.circuit_breaker import CircuitState

pytestmark = pytest.mark.unit

OPENED_AT = 1000.0
COOLDOWN = 30.0


class Clock:
    def __init__(self) -> None:
        self.now = OPENED_AT

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr("cliffracer_resilience.circuit_breaker.time", fake)
    return fake


async def _open_breaker(clock: Clock) -> CircuitBreaker:
    breaker = CircuitBreaker(
        "inventory",
        CircuitBreakerConfig(
            failure_threshold=1, recovery_timeout=COOLDOWN, monitored_exceptions=(ConnectionError,)
        ),
    )
    await breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    return breaker


async def test_an_open_circuit_reports_when_it_opened(clock):
    breaker = await _open_breaker(clock)
    clock.now = OPENED_AT + 10.0

    assert breaker.last_state_change == OPENED_AT


async def test_a_circuit_whose_cooldown_has_passed_reports_when_it_became_half_open(clock):
    breaker = await _open_breaker(clock)
    clock.now = OPENED_AT + COOLDOWN + 25.0

    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.last_state_change == OPENED_AT + COOLDOWN


async def test_a_probe_in_flight_leaves_the_half_open_time_at_the_end_of_the_cooldown(clock):
    breaker = await _open_breaker(clock)
    clock.now = OPENED_AT + COOLDOWN + 25.0

    async with breaker:
        assert breaker._state == CircuitState.HALF_OPEN
        assert breaker.last_state_change == OPENED_AT + COOLDOWN


async def test_a_successful_probe_closes_the_circuit_at_the_time_it_closed(clock):
    breaker = await _open_breaker(clock)
    clock.now = OPENED_AT + COOLDOWN + 25.0

    async with breaker:
        pass

    assert breaker.state == CircuitState.CLOSED
    assert breaker.last_state_change == clock.now


async def test_a_failed_probe_reopens_the_circuit_at_the_time_it_failed(clock):
    breaker = await _open_breaker(clock)
    clock.now = OPENED_AT + COOLDOWN + 25.0

    with pytest.raises(ConnectionError):
        async with breaker:
            raise ConnectionError("still down")

    assert breaker.state == CircuitState.OPEN
    assert breaker.last_state_change == clock.now


async def test_a_closed_circuit_reports_its_creation_until_something_changes(clock):
    breaker = CircuitBreaker("inventory")

    assert breaker.last_state_change == OPENED_AT

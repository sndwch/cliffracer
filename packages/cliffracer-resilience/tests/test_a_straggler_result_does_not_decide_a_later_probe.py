"""`record_failure()` and `record_success()` do not let a straggler decide a later probe.

They decided from the breaker's CURRENT state with no notion of which circuit the call belonged
to. A call admitted while CLOSED whose failure arrived after the cooldown had expired was read
as a failed probe: the circuit went back to OPEN and the cooldown restarted, so slow-failing
calls issued before a trip could keep it open for ever; a straggler's success closed a circuit
whose probe had not run. The `async with` path already treats a result that was not admitted as a
probe as a closed-state result that decides nothing once the circuit has left CLOSED; these
public methods now do the same, because nothing here says which circuit a caller's result
belongs to.
"""

import pytest
from cliffracer_resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

pytestmark = pytest.mark.unit


def _tripped_after_its_cooldown() -> CircuitBreaker:
    """OPEN, with the cooldown elapsed, so `state` reads HALF_OPEN and a probe may run."""
    breaker = CircuitBreaker(
        "dep", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0)
    )
    breaker.trip()
    breaker._opened_at -= 60.0
    assert breaker.state == CircuitState.HALF_OPEN
    return breaker


async def test_a_straggler_failure_does_not_reopen_the_circuit_or_restart_its_cooldown():
    breaker = _tripped_after_its_cooldown()

    await breaker.record_failure(RuntimeError("a call from before the trip, failing late"))

    assert breaker.state == CircuitState.HALF_OPEN, "the straggler was read as a failed probe"
    async with breaker:  # the probe still gets its window...
        pass
    assert breaker.state == CircuitState.CLOSED  # ...and closes the circuit


async def test_a_straggler_failure_while_open_does_not_restart_the_cooldown():
    breaker = CircuitBreaker(
        "dep", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0)
    )
    breaker.trip()
    opened_at = breaker._opened_at

    await breaker.record_failure(RuntimeError("late"))

    assert breaker._opened_at == opened_at, "the cooldown was restarted by a straggler"
    assert breaker.state == CircuitState.OPEN


async def test_a_straggler_success_does_not_close_a_circuit_whose_probe_has_not_run():
    breaker = _tripped_after_its_cooldown()

    await breaker.record_success()

    assert breaker.state == CircuitState.HALF_OPEN, "a straggler's success closed the circuit"


async def test_CONTROL_while_closed_failures_still_count_to_the_threshold_and_a_success_resets():
    breaker = CircuitBreaker(
        "dep", CircuitBreakerConfig(failure_threshold=3, recovery_timeout=30.0)
    )

    await breaker.record_failure(RuntimeError("1"))
    await breaker.record_failure(RuntimeError("2"))
    assert breaker.state == CircuitState.CLOSED and breaker.failure_count == 2
    await breaker.record_success()
    assert breaker.failure_count == 0
    for _ in range(3):
        await breaker.record_failure(RuntimeError("again"))

    assert breaker.state == CircuitState.OPEN
    assert isinstance(breaker.last_failure, RuntimeError)


class _MonitoredError(Exception):
    pass


async def test_CONTROL_a_failed_probe_through_async_with_still_reopens_the_circuit():
    breaker = CircuitBreaker(
        "dep",
        CircuitBreakerConfig(
            failure_threshold=1, recovery_timeout=30.0, monitored_exceptions=(_MonitoredError,)
        ),
    )
    breaker.trip()
    breaker._opened_at -= 60.0

    with pytest.raises(_MonitoredError):
        async with breaker:
            raise _MonitoredError("probe failed")

    assert breaker.state == CircuitState.OPEN


async def test_a_straggler_failure_during_a_probe_in_flight_does_not_decide_the_probe():
    """With a real probe admitted (`_state` HALF_OPEN), not just a cooled-down OPEN circuit."""
    breaker = CircuitBreaker(
        "dep", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0)
    )
    breaker.trip()
    breaker._opened_at -= 60.0

    async with breaker:  # the probe is in flight
        assert breaker._state == CircuitState.HALF_OPEN
        await breaker.record_failure(RuntimeError("a call from before the trip, failing late"))
        assert breaker._state == CircuitState.HALF_OPEN, "a straggler decided the probe"

    assert breaker.state == CircuitState.CLOSED  # the probe's own success decided it


async def test_a_straggler_success_during_a_probe_in_flight_does_not_close_the_circuit():
    breaker = CircuitBreaker(
        "dep",
        CircuitBreakerConfig(
            failure_threshold=1, recovery_timeout=30.0, monitored_exceptions=(_MonitoredError,)
        ),
    )
    breaker.trip()
    breaker._opened_at -= 60.0

    with pytest.raises(_MonitoredError):
        async with breaker:
            assert breaker._state == CircuitState.HALF_OPEN
            await breaker.record_success()
            assert breaker._state == CircuitState.HALF_OPEN, "a straggler closed the circuit"
            raise _MonitoredError("the probe itself failed")

    assert breaker.state == CircuitState.OPEN  # the probe's own failure decided it

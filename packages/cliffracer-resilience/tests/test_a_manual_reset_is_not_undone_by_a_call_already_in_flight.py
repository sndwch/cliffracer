"""`reset()` and `trip()` are final for the calls admitted before them.

A call admitted while the circuit was CLOSED reports its outcome when it finishes. After an
operator's `reset()` that outcome belonged to a circuit that no longer exists, but it was counted
toward the new run of failures: calls in flight at the reset could reopen the circuit it had just
closed. `reset()` and `trip()` run on the event loop with no await in them, so nothing interleaves
inside either; what undid them was the late arrivals.
"""

import asyncio

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, CircuitState

pytestmark = pytest.mark.unit


class Down(Exception):
    pass


def _breaker(threshold: int = 3) -> CircuitBreaker:
    return CircuitBreaker(
        "backend",
        CircuitBreakerConfig(failure_threshold=threshold, monitored_exceptions=(Down,)),
    )


async def _admit(breaker: CircuitBreaker, count: int) -> asyncio.Event:
    """`count` calls admitted now, which finish with a failure when the event is set."""
    release = asyncio.Event()
    admitted = asyncio.Event()
    remaining = count

    async def call() -> None:
        nonlocal remaining
        try:
            async with breaker:
                remaining -= 1
                if remaining == 0:
                    admitted.set()
                await release.wait()
                raise Down()
        except Down:
            pass

    for _ in range(count):
        asyncio.get_running_loop().create_task(call())
    await admitted.wait()
    return release


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_failures_already_in_flight_do_not_reopen_a_circuit_that_was_just_reset():
    breaker = _breaker(threshold=3)
    release = await _admit(breaker, 3)

    breaker.reset()
    release.set()
    await _settle()

    assert breaker.state is CircuitState.CLOSED
    assert breaker._failure_count == 0


async def test_a_failure_after_the_reset_counts_again():
    breaker = _breaker(threshold=2)
    release = await _admit(breaker, 1)
    breaker.reset()
    release.set()
    await _settle()

    for _ in range(2):
        with pytest.raises(Down):
            async with breaker:
                raise Down()

    assert breaker.state is CircuitState.OPEN


async def test_a_success_in_flight_at_the_reset_does_not_clear_the_failures_since():
    breaker = _breaker(threshold=3)
    release = asyncio.Event()

    async def slow_success() -> None:
        async with breaker:
            await release.wait()

    task = asyncio.get_running_loop().create_task(slow_success())
    await _settle()
    breaker.reset()
    with pytest.raises(Down):
        async with breaker:
            raise Down()
    assert breaker._failure_count == 1

    release.set()
    await task

    assert breaker._failure_count == 1


async def test_a_trip_stays_open_for_the_calls_that_finish_after_it():
    breaker = _breaker(threshold=5)
    release = await _admit(breaker, 1)

    breaker.trip()
    release.set()
    await _settle()

    assert breaker.state is CircuitState.OPEN


async def test_CONTROL_without_a_reset_the_same_failures_open_the_circuit():
    breaker = _breaker(threshold=3)
    release = await _admit(breaker, 3)

    release.set()
    await _settle()

    assert breaker.state is CircuitState.OPEN


async def test_a_failure_in_flight_at_a_trip_does_not_count_once_the_circuit_has_closed_again():
    """Admitted while CLOSED, then an operator trips the circuit, it recovers through a probe, and
    the call finishes with a failure: it belongs to the circuit that was tripped, so it does not
    count toward the new run of failures. Without the epoch `trip()` moves, it would reopen it."""
    breaker = CircuitBreaker(
        "backend",
        CircuitBreakerConfig(
            failure_threshold=1, recovery_timeout=0.0, monitored_exceptions=(Down,)
        ),
    )
    release = await _admit(breaker, 1)

    breaker.trip()
    async with breaker:  # the cooldown is zero, so this is the probe, and it succeeds
        pass
    assert breaker.state is CircuitState.CLOSED
    release.set()
    await _settle()

    assert breaker.state is CircuitState.CLOSED
    assert breaker._failure_count == 0

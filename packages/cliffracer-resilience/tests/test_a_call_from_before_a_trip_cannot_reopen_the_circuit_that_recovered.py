"""A call admitted before a trip cannot count toward the circuit that follows the recovery.

A call admitted while CLOSED carries no probe generation and, until now, an epoch that only `reset()`
and `trip()` moved. With `request_timeout` longer than `recovery_timeout`, such a call could fail after
a successful probe had closed the circuit again, and its failure was counted in the new closed run: with
a low threshold it reopened a dependency that had just recovered, on evidence from before the recovery.
The era moves when a probe closes the circuit and when an operator calls `reset()` or `trip()`. It does
not move when the failure threshold opens the circuit or when a failed probe reopens it, and a probe that
succeeds after another probe has already closed the circuit moves nothing. A call from an earlier era is
recorded (`last_failure`, the totals) and not counted toward the failure run.
"""

import asyncio

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, CircuitState

pytestmark = pytest.mark.unit

START = 1000.0
COOLDOWN = 5.0


class Down(Exception):
    pass


class Clock:
    def __init__(self) -> None:
        self.now = START

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr("cliffracer_resilience.circuit_breaker.time", fake)
    return fake


def _breaker(threshold: int = 2, probes: int = 1) -> CircuitBreaker:
    return CircuitBreaker(
        "backend",
        CircuitBreakerConfig(
            failure_threshold=threshold,
            recovery_timeout=COOLDOWN,
            half_open_max_calls=probes,
            monitored_exceptions=(Down,),
        ),
    )


class InFlight:
    """Calls admitted now, each finishing with a failure (or a success) when it is released."""

    def __init__(self, breaker: CircuitBreaker) -> None:
        self.breaker = breaker
        self.releases: list[asyncio.Event] = []
        self.tasks: list[asyncio.Task[None]] = []

    async def admit(self, count: int, *, fail: bool = True) -> list[asyncio.Event]:
        events = []
        for _ in range(count):
            release, admitted = asyncio.Event(), asyncio.Event()

            async def call(release=release, admitted=admitted) -> None:
                try:
                    async with self.breaker:
                        admitted.set()
                        await release.wait()
                        if fail:
                            raise Down()
                except Down:
                    pass

            self.tasks.append(asyncio.get_running_loop().create_task(call()))
            await admitted.wait()
            events.append(release)
        return events

    async def finish(self, events: list[asyncio.Event]) -> None:
        for event in events:
            event.set()
        for _ in range(5):
            await asyncio.sleep(0)


async def _trip_recover_and_close(clock: Clock, breaker: CircuitBreaker) -> None:
    """Two fast failures open the circuit, the cooldown passes, and a probe closes it again."""
    for _ in range(2):
        with pytest.raises(Down):
            async with breaker:
                raise Down()
    assert breaker.state is CircuitState.OPEN
    clock.now += COOLDOWN + 1
    async with breaker:
        pass
    assert breaker.state is CircuitState.CLOSED


async def test_calls_admitted_before_the_trip_do_not_reopen_the_circuit_that_recovered(clock):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    old = await flight.admit(2)
    await _trip_recover_and_close(clock, breaker)

    await flight.finish(old)

    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0, "evidence from before the recovery was counted"


async def test_a_failure_from_the_new_run_still_counts_and_opens_the_circuit(clock):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    old = await flight.admit(2)
    await _trip_recover_and_close(clock, breaker)
    new = await flight.admit(2)

    await flight.finish(old)
    assert breaker.state is CircuitState.CLOSED
    await flight.finish(new)

    assert breaker.state is CircuitState.OPEN


async def test_calls_admitted_before_a_probe_failure_reopened_it_do_not_count_after_the_next_recovery(
    clock,
):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    old = await flight.admit(2)
    for _ in range(2):
        with pytest.raises(Down):
            async with breaker:
                raise Down()
    clock.now += COOLDOWN + 1
    with pytest.raises(Down):
        async with breaker:  # the probe fails and reopens the circuit
            raise Down()
    assert breaker.state is CircuitState.OPEN
    clock.now += COOLDOWN + 1
    async with breaker:  # the next probe closes it
        pass
    assert breaker.state is CircuitState.CLOSED

    await flight.finish(old)

    assert breaker.state is CircuitState.CLOSED and breaker.failure_count == 0


async def test_calls_in_flight_when_the_circuit_trips_are_recorded_and_not_counted_twice(clock):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    old = await flight.admit(2)
    for _ in range(2):
        with pytest.raises(Down):
            async with breaker:
                raise Down()
    assert breaker.state is CircuitState.OPEN

    await flight.finish(old)

    assert breaker.state is CircuitState.OPEN
    assert breaker.last_failure is not None


async def test_CONTROL_a_manual_reset_still_protects_the_new_run(clock):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    old = await flight.admit(2)
    breaker.reset()

    await flight.finish(old)

    assert breaker.state is CircuitState.CLOSED and breaker.failure_count == 0


async def test_CONTROL_calls_admitted_in_the_current_closed_run_still_trip_the_circuit(clock):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    current = await flight.admit(2)

    await flight.finish(current)

    assert breaker.state is CircuitState.OPEN


async def test_a_late_second_probe_success_does_not_discard_the_new_runs_failures(clock):
    """A probe that succeeds after another probe closed the circuit starts no new era.

    With two probes admitted, the first closes the circuit and two calls of the new run are
    admitted. The second probe then succeeds late. Moving the era on that success would put both
    new-run calls behind it, so two failures that reach the threshold would leave the circuit
    closed with a count of 0.
    """
    breaker = _breaker(threshold=2, probes=2)
    flight = InFlight(breaker)
    for _ in range(2):
        with pytest.raises(Down):
            async with breaker:
                raise Down()
    assert breaker.state is CircuitState.OPEN
    clock.now += COOLDOWN + 1
    first, second = await flight.admit(1, fail=False), await flight.admit(1, fail=False)
    assert breaker.state is CircuitState.HALF_OPEN

    await flight.finish(first)
    assert breaker.state is CircuitState.CLOSED
    new = await flight.admit(2)
    await flight.finish(second)
    await flight.finish(new)

    assert breaker.state is CircuitState.OPEN


async def test_a_failure_from_an_earlier_era_is_recorded_as_the_last_failure_and_not_counted(clock):
    breaker = _breaker(threshold=2)
    flight = InFlight(breaker)
    old = await flight.admit(1)
    breaker.reset()
    assert breaker.last_failure is None

    await flight.finish(old)

    assert isinstance(breaker.last_failure, Down)
    assert breaker.failure_count == 0 and breaker.state is CircuitState.CLOSED

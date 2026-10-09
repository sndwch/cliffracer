"""An OPEN circuit fails fast without waiting for the lock.

`__aenter__` acquired `_lock` and only then read `self.state`. The OPEN branch
mutates nothing -- it reads a pure function of `_state`, `_opened_at` and
`config.recovery_timeout` -- so the one path whose whole promise is "fail fast,
locally, without wire traffic" was the path that waited for a lock it does not
need.

ORDERING, NOT A CLOCK. A wall-clock ceiling cannot tell the two designs apart:
it passes on the serialised one whenever the holder happens to be quick, and it
fails on the fast one whenever the box is loaded. These assert that the
fast-fail has already happened *while the lock is still held*, which is true of
one design and false of the other regardless of timing.

WHAT THIS DOES NOT CLAIM. No current holder of `_lock` awaits inside its
critical section, so there is no latency to measure in the package as it
stands, and none is claimed. The test constructs the contention deliberately.
What it pins is the invariant: a fast-fail does not inherit a holder's wait,
whatever a future holder does inside the lock.
"""

import asyncio

import pytest
from cliffracer_resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    RpcCircuitOpenError,
)

pytestmark = pytest.mark.unit


def _open_breaker(name: str = "cb") -> CircuitBreaker:
    """A breaker already OPEN, without going through failures to get there."""
    cb = CircuitBreaker(name, CircuitBreakerConfig(recovery_timeout=3600))
    cb._state = CircuitState.OPEN
    cb._opened_at = asyncio.get_event_loop().time()
    assert cb.state is CircuitState.OPEN, "the fixture did not reach OPEN"
    return cb


@pytest.mark.asyncio
async def test_an_open_circuit_fails_before_the_lock_is_released():
    """The load-bearing one: the raise happens while another task holds the lock."""
    cb = _open_breaker()
    holder_has_lock = asyncio.Event()
    may_release = asyncio.Event()
    released = False

    async def hold_the_lock():
        nonlocal released
        async with cb._lock:
            holder_has_lock.set()
            await may_release.wait()
            released = True

    holder = asyncio.create_task(hold_the_lock())
    await holder_has_lock.wait()

    try:
        with pytest.raises(RpcCircuitOpenError):
            # `wait_for` so the serialised design FAILS rather than hangs. Against
            # it this never returns on its own: the entry waits for the lock, and
            # the holder cannot release until the `finally` below runs, which is
            # after this line. A deadlock reads as a stuck suite rather than as a
            # result, so the bound is here to make the failure legible -- it is
            # not the assertion. The assertion is the ordering one underneath.
            await asyncio.wait_for(cb.__aenter__(), timeout=5.0)
        assert not released, (
            "the fast-fail only completed after the lock was released, so an "
            "OPEN circuit is serialised behind whoever holds the lock"
        )
    finally:
        may_release.set()
        await holder


@pytest.mark.asyncio
async def test_CONTROL_the_holder_really_holds_the_lock():
    """Without this, the test above passes on a lock nobody held.

    A `_lock` that was never acquired, or a holder that released before the
    breaker ran, would let the assertion above succeed for the wrong reason.
    """
    cb = _open_breaker()
    holder_has_lock = asyncio.Event()
    may_release = asyncio.Event()

    async def hold_the_lock():
        async with cb._lock:
            holder_has_lock.set()
            await may_release.wait()

    holder = asyncio.create_task(hold_the_lock())
    await holder_has_lock.wait()
    try:
        assert cb._lock.locked(), "the holder task did not actually hold the lock"
    finally:
        may_release.set()
        await holder
    assert not cb._lock.locked()


@pytest.mark.asyncio
async def test_a_half_open_probe_still_takes_the_lock():
    """The branch that mutates must stay serialised.

    `HALF_OPEN` reads `_half_open_calls`, compares it against
    `half_open_max_calls` and increments it. Two tasks entering at once could
    both pass the comparison before either increments, so this branch keeps the
    lock that the OPEN branch gives up.

    ENTRY ONLY, not the full context. `__aexit__` calls `record_success`, which
    closes the circuit and resets `_half_open_calls` to zero -- so a version of
    this test using `async with` admitted the first probe, closed the circuit,
    and then legitimately admitted the other seven. That measured the recovery
    path, not the admission limit. The property under test is `__aenter__`'s
    alone.
    """
    cb = CircuitBreaker("cb", CircuitBreakerConfig(recovery_timeout=0, half_open_max_calls=1))
    cb._state = CircuitState.OPEN
    cb._opened_at = asyncio.get_event_loop().time() - 10_000
    assert cb.state is CircuitState.HALF_OPEN, "the fixture did not reach HALF_OPEN"

    admitted = 0
    refused = 0

    async def probe():
        nonlocal admitted, refused
        try:
            await cb.__aenter__()
            admitted += 1
        except RpcCircuitOpenError:
            refused += 1

    await asyncio.gather(*(probe() for _ in range(8)))

    assert admitted == 1, f"{admitted} probes were admitted; half_open_max_calls is 1"
    assert refused == 7, refused
    assert cb._half_open_calls == 1, cb._half_open_calls

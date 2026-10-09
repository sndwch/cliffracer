"""What a circuit breaker counts, when it changes state, and how many probes it admits.

An open-circuit error carries its message as its text. A default breaker opens on the fifth failure
and admits one probe when half open. The breaker binds itself on entry; an exit without an entry
raises `RuntimeError`, and a nested call's exit pops only its own entry. A probe that ends in an
unmonitored error frees exactly its slot and leaves the circuit half open; a successful probe counts
one success, and a failed one is the last failure. `reset()` clears the count and the last failure,
and `reset()`, `trip()` and a threshold trip each move the state-change time. The circuit is half
open the instant its cooldown ends. A resilient proxy exposes its breaker, has no private methods,
and says so when its owner is gone.
"""

import asyncio
import gc

import pytest
from cliffracer_resilience import circuit_breaker as cb_module
from cliffracer_resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    ResilientServiceProxy,
    RpcCircuitOpenError,
)

pytestmark = pytest.mark.unit


class Down(Exception):
    pass


class Clock:
    """Stands in for the module's `time`: monotonic() returns `now`."""

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class Owner:
    pass


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock(100.0)
    monkeypatch.setattr(cb_module, "time", fake)
    return fake


def _breaker(**kw: object) -> CircuitBreaker:
    kw.setdefault("monitored_exceptions", (Down,))
    return CircuitBreaker("backend", CircuitBreakerConfig(**kw))  # type: ignore[arg-type]


# --- RpcCircuitOpenError -------------------------------------------------------------


def test_an_open_circuit_error_carries_its_message_as_its_text():
    assert str(RpcCircuitOpenError()) == "Circuit breaker is open"
    assert str(RpcCircuitOpenError(message="backend down")) == "backend down"
    assert RpcCircuitOpenError(message="backend down").args == ("backend down",)


# --- defaults ------------------------------------------------------------------------


async def test_a_default_breaker_opens_on_the_fifth_failure():
    breaker = CircuitBreaker("backend")
    for _ in range(4):
        await breaker.record_failure(Down())
    assert breaker.state == CircuitState.CLOSED
    await breaker.record_failure(Down())
    assert breaker.state == CircuitState.OPEN


async def test_a_default_half_open_circuit_admits_one_probe():
    breaker = CircuitBreaker("backend", CircuitBreakerConfig(recovery_timeout=0.0))
    breaker.trip()
    async with breaker:
        with pytest.raises(RpcCircuitOpenError):
            async with breaker:
                pass


# --- context manager -----------------------------------------------------------------


async def test_entering_the_breaker_binds_the_breaker():
    breaker = _breaker()
    async with breaker as bound:
        assert bound is breaker


async def test_exiting_without_entering_raises_runtime_error():
    breaker = _breaker()
    with pytest.raises(RuntimeError, match="without a matching entry"):
        await breaker.__aexit__(None, None, None)


async def test_an_outer_call_from_before_a_reset_is_not_counted_after_a_nested_call():
    breaker = _breaker(failure_threshold=1)
    with pytest.raises(Down):
        async with breaker:
            breaker.reset()
            async with breaker:
                pass
            raise Down()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


async def test_a_probe_failing_with_an_unmonitored_error_frees_its_slot_and_keeps_half_open(
    clock: Clock,
):
    breaker = _breaker(recovery_timeout=10.0)
    breaker.trip()
    clock.now = 110.0
    with pytest.raises(ValueError):
        async with breaker:
            raise ValueError("caller error")
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.last_failure is None
    # the slot came back: another probe is admitted, and its success closes the circuit
    async with breaker:
        pass
    assert breaker.state == CircuitState.CLOSED


async def test_a_released_probe_returns_exactly_one_slot():
    breaker = _breaker(recovery_timeout=0.0, half_open_max_calls=2)
    breaker.trip()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def held() -> None:
        async with breaker:
            entered.set()
            await release.wait()

    holder = asyncio.get_running_loop().create_task(held())
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(ValueError):
        async with breaker:  # second slot, released by an unmonitored error
            raise ValueError()
    # one slot back: a second probe enters, a third is refused
    async with breaker:
        with pytest.raises(RpcCircuitOpenError):
            async with breaker:
                pass
    release.set()
    await asyncio.wait_for(holder, 5)


async def test_a_call_waiting_on_the_lock_is_refused_when_the_circuit_opened_meanwhile():
    breaker = _breaker()
    await breaker._lock.acquire()
    outcome: list[object] = []

    async def call() -> None:
        try:
            async with breaker:
                outcome.append("admitted")
        except RpcCircuitOpenError as exc:
            outcome.append(exc)

    task = asyncio.get_running_loop().create_task(call())
    for _ in range(5):
        await asyncio.sleep(0)
    breaker.trip()
    breaker._lock.release()
    await asyncio.wait_for(task, 5)
    assert len(outcome) == 1
    assert isinstance(outcome[0], RpcCircuitOpenError)


# --- probe bookkeeping ---------------------------------------------------------------


async def test_a_successful_probe_counts_one_success():
    breaker = _breaker(recovery_timeout=0.0)
    breaker.trip()
    before = breaker.success_count
    async with breaker:
        pass
    assert breaker.success_count == before + 1


async def test_a_failed_probe_is_the_last_failure():
    breaker = _breaker(recovery_timeout=0.0)
    breaker.trip()
    err = Down()
    with pytest.raises(Down):
        async with breaker:
            raise err
    assert breaker.last_failure is err


# --- reset / trip --------------------------------------------------------------------


async def test_reset_clears_the_failure_count_and_last_failure():
    breaker = _breaker(failure_threshold=3)
    await breaker.record_failure(Down())
    await breaker.record_failure(Down())
    breaker.reset()
    assert breaker.failure_count == 0
    assert breaker.last_failure is None
    await breaker.record_failure(Down())
    await breaker.record_failure(Down())
    assert breaker.state == CircuitState.CLOSED


def test_reset_moves_the_state_change_time(clock: Clock):
    breaker = _breaker(recovery_timeout=30.0)
    breaker.trip()
    clock.now = 105.0
    breaker.reset()
    assert breaker.last_state_change == 105.0


def test_trip_moves_the_state_change_time(clock: Clock):
    breaker = _breaker(recovery_timeout=30.0)
    clock.now = 110.0
    breaker.trip()
    clock.now = 112.0
    assert breaker.last_state_change == 110.0


async def test_tripping_by_failures_moves_the_state_change_time(clock: Clock):
    breaker = _breaker(failure_threshold=1, recovery_timeout=30.0)
    clock.now = 120.0
    await breaker.record_failure(Down())
    clock.now = 125.0
    assert breaker.state == CircuitState.OPEN
    assert breaker.last_state_change == 120.0


async def test_a_trip_during_a_probe_gives_the_next_recovery_a_fresh_slot():
    breaker = _breaker(recovery_timeout=0.0)
    breaker.trip()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def probe() -> None:
        async with breaker:
            entered.set()
            await release.wait()

    task = asyncio.get_running_loop().create_task(probe())
    await asyncio.wait_for(entered.wait(), 5)
    breaker.trip()
    async with breaker:  # the next recovery's probe is admitted
        pass
    assert breaker.state == CircuitState.CLOSED
    release.set()
    await asyncio.wait_for(task, 5)


def test_a_circuit_is_half_open_the_instant_its_cooldown_ends(clock: Clock):
    breaker = _breaker(recovery_timeout=10.0)
    breaker.trip()
    clock.now = 110.0
    assert breaker.state == CircuitState.HALF_OPEN
    breaker2 = _breaker(recovery_timeout=0.0)
    breaker2.trip()
    assert breaker2.state == CircuitState.HALF_OPEN


# --- proxies -------------------------------------------------------------------------


def test_a_method_proxy_exposes_the_breaker_it_runs_under():
    owner = Owner()
    breaker = _breaker()
    proxy = ResilientServiceProxy(owner, "inventory", circuit_breaker=breaker)
    assert proxy.check_stock.circuit_breaker is breaker


def test_a_resilient_service_proxy_has_no_private_methods():
    owner = Owner()
    proxy = ResilientServiceProxy(owner, "inventory", circuit_breaker=_breaker())
    with pytest.raises(AttributeError):
        _ = proxy._private
    assert not hasattr(proxy, "_private")


def test_a_resilient_service_proxy_whose_owner_is_gone_says_so():
    owner = Owner()
    proxy = ResilientServiceProxy(owner, "inventory", circuit_breaker=_breaker())
    del owner
    gc.collect()
    with pytest.raises(RuntimeError, match="Service instance was garbage collected"):
        _ = proxy.check_stock

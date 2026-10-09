"""A timer reads time and waits through the clock it is given, and a test moves a fake one.

`Timer(clock=)` defaults to the real clock. A `FakeClock` moves only when the test moves it:
`advance` wakes each wait that ends inside the step, in deadline order, and returns once every
task that waits on it is waiting again or has finished. `ServiceTestHarness.start_timers(clock=)`
puts every timer a service declared on one clock.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core.clock import REAL_CLOCK, Clock, RealClock
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.timer import Timer
from cliffracer.testing import FakeClock, ServiceTestHarness

pytestmark = pytest.mark.unit


class Ticks:
    def __init__(self) -> None:
        self.at: list[float] = []
        self.clock: FakeClock | None = None

    async def tick(self) -> None:
        assert self.clock is not None
        self.at.append(self.clock.monotonic())


async def _started(t: Timer, clock: FakeClock) -> Ticks:
    service = Ticks()
    service.clock = clock
    t.method_name = "tick"
    await t.start(service)
    clock.watch(t.task)
    return service


# --- the timer's clock ------------------------------------------------------------------------------


def test_a_timer_given_no_clock_reads_the_real_one():
    assert Timer(interval=1).clock is REAL_CLOCK
    assert isinstance(REAL_CLOCK, Clock) and isinstance(FakeClock(), Clock)


def test_a_timer_keeps_the_clock_it_is_given_and_a_clone_shares_it():
    clock = FakeClock()
    t = Timer(interval=1, clock=clock)
    assert t.clock is clock
    assert t.clone().clock is clock


@pytest.mark.parametrize("not_a_clock", [object(), 1.0, "real"], ids=["object", "float", "str"])
def test_a_timer_refuses_a_clock_that_is_not_one(not_a_clock):
    with pytest.raises(ConfigurationError, match=r"Timer clock must have monotonic\(\)"):
        Timer(interval=1, clock=not_a_clock)


async def test_a_timer_fires_once_per_interval_of_its_clock():
    clock = FakeClock()
    t = Timer(interval=0.25, clock=clock)
    service = await _started(t, clock)
    try:
        await clock.advance(1.0)
    finally:
        await t.stop()

    assert service.at == [0.25, 0.5, 0.75, 1.0]


async def test_a_stop_ends_a_timer_waiting_on_a_fake_clock_without_moving_it():
    clock = FakeClock()
    t = Timer(interval=60, clock=clock)
    service = await _started(t, clock)
    await clock.advance(0)

    await asyncio.wait_for(t.stop(), timeout=5)

    assert t.task is not None and t.task.done()
    assert service.at == [] and clock.monotonic() == 0


async def test_an_error_backoff_is_slept_on_the_clock():
    clock = FakeClock()
    t = Timer(interval=1.0, eager=True, error_backoff=30.0, clock=clock)
    t.method_name = "absent"

    async def raises() -> None:
        raise RuntimeError("the firing failed outside the handler")

    t._execute_method = raises  # type: ignore[method-assign]
    await t.start(Ticks())
    clock.watch(t.task)
    try:
        await clock.advance(30.0)
        after_the_backoff = t.error_count
        await clock.advance(1.0)
    finally:
        await t.stop()

    # The eager error, then 30 s of backoff, then the first scheduled firing an interval later.
    assert (after_the_backoff, t.error_count) == (1, 2)


# --- the fake clock -------------------------------------------------------------------------------


async def test_advance_wakes_waits_in_deadline_order_not_the_order_they_began():
    clock = FakeClock()
    woke: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(name)

    tasks = [
        asyncio.create_task(sleeper("late", 0.3)),
        asyncio.create_task(sleeper("early", 0.1)),
        asyncio.create_task(sleeper("middle", 0.2)),
    ]
    for task in tasks:
        clock.watch(task)
    await clock.advance(0.25)
    assert woke == ["early", "middle"]
    await clock.advance(0.05)
    assert woke == ["early", "middle", "late"]


async def test_a_wait_returns_true_when_its_event_is_set_and_false_when_its_time_runs_out():
    clock = FakeClock()
    event = asyncio.Event()
    set_first = asyncio.create_task(clock.wait(event, 10))
    times_out = asyncio.create_task(clock.wait(asyncio.Event(), 10))
    clock.watch(set_first)
    clock.watch(times_out)
    await clock.advance(1)

    event.set()
    assert await set_first is True
    await clock.advance(9)
    assert await times_out is False


async def test_a_wall_step_moves_now_and_leaves_monotonic_and_the_waits_alone():
    start = datetime(2026, 3, 8, 7, 59, 59, tzinfo=UTC)
    clock = FakeClock(start=start)
    woke = asyncio.Event()

    async def one_second() -> None:
        await clock.sleep(1)
        woke.set()

    clock.watch(asyncio.create_task(one_second()))
    await clock.advance(0)
    clock.step_wall(-3600)

    assert clock.now(UTC) == start - timedelta(hours=1)
    assert clock.monotonic() == 0 and not woke.is_set()
    await clock.advance(1)
    assert woke.is_set()
    assert clock.now(ZoneInfo("America/Chicago")) == start - timedelta(hours=1, seconds=-1)


def test_a_fake_clock_refuses_a_naive_start():
    with pytest.raises(ValueError, match="aware"):
        FakeClock(start=datetime(2026, 1, 1))


async def test_advance_refuses_to_go_backwards():
    with pytest.raises(ValueError, match="does not go backwards"):
        await FakeClock().advance(-1)


async def test_advance_names_a_task_that_never_comes_back_to_the_clock():
    clock = FakeClock(settle_within=0.2)
    elsewhere = asyncio.Event()

    async def wanders() -> None:
        await clock.sleep(1)
        await elsewhere.wait()

    task = asyncio.create_task(wanders(), name="wanders")
    clock.watch(task)
    try:
        with pytest.raises(AssertionError, match="wanders did not wait on the clock again"):
            await clock.advance(1)
    finally:
        task.cancel()


# --- the real clock -------------------------------------------------------------------------------


async def test_the_real_clock_waits_for_an_event_or_its_timeout():
    clock = RealClock()
    event = asyncio.Event()
    event.set()
    assert await clock.wait(event, 5) is True
    assert await clock.wait(asyncio.Event(), 0.01) is False
    assert clock.now(UTC).tzinfo is UTC


# --- the harness ----------------------------------------------------------------------------------


class Polling(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="polling", health_port=0))
        self.polls = 0

    @timer(interval=10)
    async def poll(self) -> None:
        self.polls += 1


async def test_the_harness_starts_every_timer_on_the_clock_and_its_teardown_stops_them():
    clock = FakeClock()
    harness = ServiceTestHarness(Polling())
    await harness.start_timers(clock=clock)
    (started,) = harness.container.registry.timers
    try:
        assert started.clock is clock and started.is_running
        await clock.advance(30)
        assert harness.service.polls == 3
    finally:
        await harness.teardown()
    assert not started.is_running and started.task is not None and started.task.done()


async def test_a_harness_over_a_broker_refuses_to_start_the_timers_its_start_already_ran():
    connect = AsyncMock()
    harness = ServiceTestHarness(Polling(), broker=SimpleNamespace(connect=connect))
    with pytest.raises(RuntimeError, match=r"timers run under start\(\) on a broker harness"):
        await harness.start_timers(clock=FakeClock())
    connect.assert_not_awaited()
    await harness.teardown()


async def test_the_harness_refuses_a_clock_that_is_not_one():
    harness = ServiceTestHarness(Polling())
    with pytest.raises(TypeError, match="clock must have"):
        await harness.start_timers(clock=object())  # type: ignore[arg-type]
    await harness.teardown()

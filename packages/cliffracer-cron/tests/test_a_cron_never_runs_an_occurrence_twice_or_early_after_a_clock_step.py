"""A cron job runs each occurrence once, and not before its time, when the wall clock steps back.

`CronTimer` guarded a step made WHILE it waited, but kept no record of the occurrence it had just run:
after a firing the loop searched again from `now`, and a clock stepped back past that occurrence found
it again and ran it again. `DistributedCronTimer` had no wall-clock guard at all, so after a backward
step it ran the job, and claimed the interval key, before its scheduled time. Both loops now wait in
one place, which starts no search before the last occurrence it ran and does not return until the wall
clock has reached the target.

The clock and the waits are stand-ins, the same as in `test_the_cron_loops_decisions` and copied here because package tests cannot import one another when the whole suite is collected: a wait advances a fake
wall clock by its length, then by any step the test schedules for it, so the monotonic wait and the
wall clock can disagree as they do when an operator or NTP moves the clock.
"""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cliffracer_cron import CronTimer, DistributedCronTimer

pytestmark = pytest.mark.unit

# By module path: the package exports its `cron` decorator under the name of the module.
CRON_MODULE = importlib.import_module("cliffracer_cron.cron")
#: A timer reads time through its clock; the real clock reads this module's `datetime` and waits
#: through its `asyncio`, so a stand-in for either goes here.
CLOCK_MODULE = importlib.import_module("cliffracer.core.clock")

START = datetime(2026, 1, 2, 12, 0, 30, tzinfo=UTC)
EVERY_TEN_SECONDS = "* * * * * */10"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now = datetime.fromtimestamp(self.now.timestamp() + seconds, tz=self.now.tzinfo)


def _datetime_reading(clock: _Clock) -> type[datetime]:
    """`datetime` for a module whose `now()` is the clock. croniter still gets a datetime class."""

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return clock.now if tz is None else clock.now.astimezone(tz)

    return _Datetime


class _LoopAsyncio:
    """Stands in for `asyncio` inside one cron module."""

    def __init__(self, clock: _Clock, real_asyncio: Any) -> None:
        self._clock = clock
        self._real = real_asyncio
        self.waits: list[float] = []
        self.sleeps: list[float] = []
        self.events: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def wait_for(self, awaitable: Any, timeout: float) -> None:
        awaitable.close()
        self.waits.append(timeout)
        self.events.append(f"wait {timeout:g}")
        self._clock.advance(timeout)
        # A suspension point, so that `asyncio.wait_for` around a loop can stop it.
        await self._real.sleep(0)
        raise TimeoutError

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.events.append(f"sleep {seconds:g}")
        self._clock.advance(seconds)
        await self._real.sleep(0)


MODULES = {
    CronTimer: CRON_MODULE,
    DistributedCronTimer: importlib.import_module("cliffracer_cron.distributed"),
}
TIMERS = pytest.mark.parametrize(
    "timer_cls", [CronTimer, DistributedCronTimer], ids=["cron", "distributed"]
)


class _SteppingAsyncio(_LoopAsyncio):
    """Waits that end with the wall clock moved by a scheduled step."""

    def __init__(
        self,
        clock: _Clock,
        real_asyncio: Any,
        steps_after_waits: list[float],
        *,
        stopped_during_the_wait: bool = False,
    ) -> None:
        super().__init__(clock, real_asyncio)
        self.steps_after_waits = list(steps_after_waits)
        self.stopped_during_the_wait = stopped_during_the_wait

    async def wait_for(self, awaitable: Any, timeout: float) -> None:
        awaitable.close()
        self.waits.append(timeout)
        await self._real.sleep(0)
        if self.stopped_during_the_wait:
            return None  # the stop event was set: `wait_for` returns instead of timing out
        self._clock.advance(timeout)
        if self.steps_after_waits:
            self._clock.advance(self.steps_after_waits.pop(0))
        raise TimeoutError


def _run_timer(
    monkeypatch,
    timer_cls,
    *,
    firings: int,
    steps_after_waits=(),
    steps_in_firings=(),
    stopped_during_the_wait=False,
):
    """Run `firings` firings; returns the timer, the wall time at each firing, and the waits."""
    clock = _Clock(START)
    loop = _SteppingAsyncio(
        clock,
        CLOCK_MODULE.asyncio,
        list(steps_after_waits),
        stopped_during_the_wait=stopped_during_the_wait,
    )
    for module in {MODULES[timer_cls], CRON_MODULE, CLOCK_MODULE}:
        monkeypatch.setattr(module, "datetime", _datetime_reading(clock))
    monkeypatch.setattr(CLOCK_MODULE, "asyncio", loop)
    timer = timer_cls(EVERY_TEN_SECONDS)
    timer.method_name = "tick"
    timer.is_running = True
    walls: list[datetime] = []
    in_firing = list(steps_in_firings)

    def fire() -> None:
        walls.append(clock.now)
        if in_firing:
            clock.advance(in_firing.pop(0))
        if len(walls) >= firings:
            timer.is_running = False

    if timer_cls is DistributedCronTimer:

        async def execute_distributed(target_time, eager=False):
            fire()

        timer._execute_distributed = execute_distributed  # type: ignore[method-assign]
    else:

        async def execute():
            fire()

        timer._execute_method = execute  # type: ignore[method-assign]
    return timer, walls, loop


async def _go(timer) -> None:
    await asyncio.wait_for(timer._timer_loop(), timeout=3.0)


def _at(minute: int, second: int) -> datetime:
    return datetime(2026, 1, 2, 12, minute, second, tzinfo=UTC)


@TIMERS
async def test_CONTROL_with_a_steady_clock_each_occurrence_runs_once_in_order(
    monkeypatch, timer_cls
):
    timer, walls, loop = _run_timer(monkeypatch, timer_cls, firings=3)

    await _go(timer)

    assert walls == [_at(0, 40), _at(0, 50), _at(1, 0)]
    assert len(loop.waits) == 3


@TIMERS
async def test_a_clock_stepped_back_during_a_firing_does_not_run_the_occurrence_again(
    monkeypatch, timer_cls
):
    timer, walls, _ = _run_timer(monkeypatch, timer_cls, firings=3, steps_in_firings=[-4.0])

    await _go(timer)

    assert walls == [_at(0, 40), _at(0, 50), _at(1, 0)], walls


@TIMERS
async def test_a_clock_stepped_back_by_more_than_an_interval_still_moves_on(monkeypatch, timer_cls):
    timer, walls, loop = _run_timer(monkeypatch, timer_cls, firings=3, steps_in_firings=[-25.0])

    await _go(timer)

    assert walls == [_at(0, 40), _at(0, 50), _at(1, 0)], walls
    assert loop.waits[1] == 35.0, (
        "the wait is from now (12:00:15) to the next occurrence (12:00:50)"
    )


@TIMERS
async def test_a_clock_stepped_back_while_waiting_does_not_fire_before_the_target(
    monkeypatch, timer_cls
):
    # The first wait is 10 s on the monotonic clock; the wall clock has only moved 5 s when it ends.
    timer, walls, loop = _run_timer(monkeypatch, timer_cls, firings=2, steps_after_waits=[-5.0])

    await _go(timer)

    assert walls == [_at(0, 40), _at(0, 50)], walls
    assert len(loop.waits) >= 3, "the remainder of the first wait was waited out"


@TIMERS
async def test_a_clock_stepped_forward_does_not_catch_up(monkeypatch, timer_cls):
    timer, walls, _ = _run_timer(monkeypatch, timer_cls, firings=2, steps_in_firings=[3600.0])

    await _go(timer)

    assert walls[0] == _at(0, 40)
    assert walls[1].hour == 13 and walls[1].second % 10 == 0, walls
    assert walls[1] - walls[0] < timedelta(hours=1, seconds=11), "one occurrence, not a run of them"


@TIMERS
async def test_a_timer_stopped_before_it_waits_fires_nothing(monkeypatch, timer_cls):
    timer, walls, _ = _run_timer(monkeypatch, timer_cls, firings=3)
    timer._stop_event.set()

    await _go(timer)

    assert walls == []


@TIMERS
async def test_a_stop_that_ends_the_wait_fires_nothing_and_ends_the_loop(monkeypatch, timer_cls):
    timer, walls, loop = _run_timer(monkeypatch, timer_cls, firings=3, stopped_during_the_wait=True)

    await _go(timer)

    assert walls == [] and len(loop.waits) == 1


async def test_the_last_fired_occurrence_is_recorded_for_the_distributed_timer_too(monkeypatch):
    timer, walls, _ = _run_timer(monkeypatch, DistributedCronTimer, firings=1)

    await _go(timer)

    assert timer._last_fired == walls[0].timestamp()

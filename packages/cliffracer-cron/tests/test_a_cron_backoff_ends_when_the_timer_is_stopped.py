"""A cron loop's error backoff ends when the timer is stopped, on a plain and a distributed cron.

A handler that stops its own timer leaves the loop to end when it next sees the timer is not
running. A firing that then fails outside the handler sends the loop into its backoff, which
waits on the stop event, so the loop ends at once instead of after `error_backoff`. The clock is a
`FakeClock` that nothing advances past the firing, so a backoff that waited out its time would
never end.
"""

import asyncio

import pytest
from cliffracer_cron.cron import CronTimer
from cliffracer_cron.distributed import DistributedCronTimer

from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit

#: The outer bound on a wait that only a defect can make long: on a clock nothing advances, the
#: loop either ends at once or never.
NEVER = 2.0


def _build(kind: str, clock: FakeClock, eager: bool) -> CronTimer:
    if kind == "cron":
        return CronTimer("* * * * *", eager=eager, error_backoff=30.0, clock=clock)
    return DistributedCronTimer("* * * * *", eager=eager, error_backoff=30.0, clock=clock)


async def _loop(timer: CronTimer, clock: FakeClock) -> asyncio.Task[None]:
    async def firing(*args, **kwargs) -> None:
        # The timer as `stop()` from its own run leaves it, then a failure outside the handler.
        timer.is_running = False
        timer._stop_event.set()
        raise RuntimeError("the firing failed outside its handler")

    timer.method_name = "run"
    if isinstance(timer, DistributedCronTimer):
        timer._execute_distributed = firing  # type: ignore[method-assign]
    else:
        timer._execute_method = firing  # type: ignore[method-assign]
    timer.is_running = True
    timer._stop_event = asyncio.Event()
    task = asyncio.create_task(timer._timer_loop())
    clock.watch(task)
    return task


@pytest.mark.parametrize("kind", ["cron", "distributed"])
async def test_an_eager_firing_that_stops_its_timer_and_fails_ends_the_loop_at_once(kind):
    clock = FakeClock()
    timer = _build(kind, clock, eager=True)
    task = await _loop(timer, clock)

    await asyncio.wait_for(task, NEVER)

    assert timer.error_count == 1


@pytest.mark.parametrize("kind", ["cron", "distributed"])
async def test_a_scheduled_firing_that_stops_its_timer_and_fails_ends_the_loop_at_once(kind):
    clock = FakeClock()
    timer = _build(kind, clock, eager=False)
    task = await _loop(timer, clock)
    await clock.advance(60.0)  # to the first occurrence, and no further

    await asyncio.wait_for(task, NEVER)

    assert timer.error_count == 1

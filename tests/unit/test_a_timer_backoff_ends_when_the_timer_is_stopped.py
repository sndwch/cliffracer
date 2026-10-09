"""A timer's error backoff ends when the timer is stopped, as its wait for the next firing does.

A stop from outside the timer's run cancels a timer in its backoff at once, since nothing is in
flight. A handler that stops its own timer does not cancel it: `stop()` sets the stop event and
returns, and the loop ends when it next sees the timer is not running. A firing that then fails
outside the handler sends the loop into its backoff, which waits on the stop event, so the loop
ends at once instead of after `error_backoff`. The clock is a `FakeClock` that nothing advances
past the firing, so a backoff that waited out its time would never end.
"""

import asyncio

import pytest

from cliffracer.core.timer import Timer
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit

#: The outer bound on a wait that only a defect can make long: on a clock nothing advances, the
#: loop either ends at once or never.
NEVER = 2.0


def _stops_itself_then_fails(timer: Timer):
    """A firing that leaves the timer as `stop()` from its own run does, then fails outside it."""

    async def firing() -> None:
        timer.is_running = False
        timer._stop_event.set()
        raise RuntimeError("the firing failed outside its handler")

    return firing


async def _loop(timer: Timer, clock: FakeClock) -> asyncio.Task[None]:
    timer.method_name = "run"
    timer._execute_method = _stops_itself_then_fails(timer)  # type: ignore[method-assign]
    timer.is_running = True
    timer._stop_event = asyncio.Event()
    task = asyncio.create_task(timer._timer_loop())
    clock.watch(task)
    return task


async def test_an_eager_firing_that_stops_its_timer_and_fails_ends_the_loop_at_once():
    clock = FakeClock()
    timer = Timer(interval=30.0, eager=True, error_backoff=30.0, clock=clock)
    task = await _loop(timer, clock)

    await asyncio.wait_for(task, NEVER)

    assert timer.error_count == 1


async def test_a_scheduled_firing_that_stops_its_timer_and_fails_ends_the_loop_at_once():
    clock = FakeClock()
    timer = Timer(interval=30.0, error_backoff=30.0, clock=clock)
    task = await _loop(timer, clock)
    await clock.advance(30.0)  # to the first firing, and no further

    await asyncio.wait_for(task, NEVER)

    assert timer.error_count == 1


async def test_CONTROL_a_backoff_with_no_stop_waits_out_its_time_on_the_clock():
    clock = FakeClock()
    timer = Timer(interval=30.0, eager=True, error_backoff=30.0, clock=clock)
    timer.method_name = "run"

    async def fails() -> None:
        raise RuntimeError("the firing failed outside its handler")

    timer._execute_method = fails  # type: ignore[method-assign]
    timer.is_running = True
    timer._stop_event = asyncio.Event()
    task = asyncio.create_task(timer._timer_loop())
    clock.watch(task)
    await clock.advance(29.0)

    assert not task.done() and timer.error_count == 1, "the backoff did not hold the loop"
    await clock.advance(1.0)  # the backoff ends, and the next firing (rebased to +30 s) is due
    await clock.advance(30.0)
    assert timer.error_count == 2
    task.cancel()  # not a stop: this row holds with or without a backoff that ends on one
    await asyncio.gather(task, return_exceptions=True)

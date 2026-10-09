"""A timer schedules on the monotonic clock, so a step of the wall clock does not move it.

A loop that computed its next firing from the wall time would, after a backward step (an NTP
correction, a VM resume, `date -s`), wait the interval plus the size of the step, and sit idle for
as long as the step was large. The timer reads `monotonic()` from its clock, which a wall step
leaves alone.
"""

import pytest

from cliffracer.core.timer import Timer
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit


class Service:
    def __init__(self) -> None:
        self.fired = 0

    async def tick(self) -> None:
        self.fired += 1


async def _firings_before_and_after(wall_step: float) -> tuple[int, int]:
    """Firings of a 0.1 s timer in 0.5 s, then in the 1.0 s after the wall clock is stepped."""
    clock = FakeClock()
    service = Service()
    t = Timer(interval=0.1, clock=clock)
    t.method_name = "tick"
    await t.start(service)
    clock.watch(t.task)
    try:
        await clock.advance(0.5)
        before = service.fired
        clock.step_wall(wall_step)
        await clock.advance(1.0)
    finally:
        await t.stop()
    return before, service.fired - before


async def test_a_backward_step_of_the_wall_clock_does_not_stall_the_timer():
    assert await _firings_before_and_after(-3600.0) == (5, 10)


async def test_CONTROL_with_no_step_the_timer_fires_at_its_interval():
    assert await _firings_before_and_after(0.0) == (5, 10)

"""A cron timer reads the wall time and waits through the clock it is given, and keeps it in a clone.

A cron loop computes each occurrence from `clock.now(tz)` and waits for it on the clock's
monotonic time, so a `FakeClock` moved forward runs each occurrence once, at its wall time.
"""

from datetime import UTC, datetime

import pytest
from cliffracer_cron import CronTimer
from cliffracer_cron.distributed import DistributedCronTimer

from cliffracer.core.clock import REAL_CLOCK
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit


class Ticks:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.at: list[datetime] = []

    async def tick(self) -> None:
        self.at.append(self.clock.now(UTC))


@pytest.mark.parametrize("timer_cls", [CronTimer, DistributedCronTimer])
def test_a_cron_timer_keeps_the_clock_it_is_given_in_a_clone(timer_cls):
    clock = FakeClock()
    assert timer_cls("* * * * *").clock is REAL_CLOCK
    t = timer_cls("* * * * *", clock=clock)
    assert t.clock is clock
    assert t.clone().clock is clock


async def test_a_cron_timer_on_a_fake_clock_runs_each_occurrence_at_its_wall_time():
    clock = FakeClock(start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    service = Ticks(clock)
    t = CronTimer("* * * * * */10", clock=clock)  # every ten seconds (croniter: seconds last)
    t.method_name = "tick"
    await t.start(service)
    clock.watch(t.task)
    try:
        await clock.advance(35)
    finally:
        await t.stop()

    assert [moment.second for moment in service.at] == [10, 20, 30]

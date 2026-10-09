"""The clock a timer schedules by.

A `Timer` and the cron timers read time only through a `Clock`: what time it is, on the monotonic
and on the wall time base, and how to wait. The default is `REAL_CLOCK`. A test passes a clock that
it advances, `cliffracer.testing.FakeClock`, so a schedule is tested without waiting for it.

Two time bases, as a cron loop needs both. An interval timer schedules on `monotonic()`, which a
wall clock step does not move. A cron loop computes its targets in wall time (`now(tz)`) and waits
on the monotonic base, and its wait checks the wall time again when it wakes.

Only what a timer schedules reads this clock: its waits, its backoffs and its execution timing. A
stop's grace and the timestamps a distributed lease shares with other replicas stay on real time.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, tzinfo
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """What a timer reads time through."""

    def monotonic(self) -> float:
        """Seconds on a clock that only moves forward, for intervals and durations."""
        ...

    def now(self, tz: tzinfo) -> datetime:
        """The current wall time in `tz`, for cron targets."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait `seconds`."""
        ...

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:
        """Wait until `event` is set or `timeout` seconds pass; True when the event was set."""
        ...


class RealClock:
    """The system clock and the event loop's own waits."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self, tz: tzinfo) -> datetime:
        return datetime.now(tz)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def __repr__(self) -> str:
        return "RealClock()"


#: The clock a timer uses when it is given none.
REAL_CLOCK = RealClock()

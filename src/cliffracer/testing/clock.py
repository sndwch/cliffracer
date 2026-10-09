"""A clock a test moves, so a timer's schedule is tested without waiting for it."""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SPINS_BEFORE_SLEEPING = 100


def _micros(seconds: float) -> int:
    return round(seconds * 1_000_000)


@dataclass
class _Sleeper:
    deadline: int
    seq: int
    future: asyncio.Future[None] = field(repr=False)
    task: asyncio.Task[object] | None


class FakeClock:
    """A `cliffracer.core.clock.Clock` whose time moves only when the test moves it.

    Pass it to a timer (`Timer(..., clock=clock)`, or `ServiceTestHarness.start_timers(clock=)`),
    then move time:

    - `advance(seconds)` moves the monotonic and the wall time forward together. Each wait that
      ends inside the step is woken in deadline order, and after each wake `advance` waits, in
      real time bounded by `settle_within`, until every task that waits on this clock is waiting
      on it again or has finished. So a timer at interval 0.05 has fired five times when
      `advance(0.26)` returns, and a handler that is still running (awaiting something that is
      not this clock) holds `advance` until it returns.
    - `step_wall(seconds)` moves only the wall time, as a wall clock that is set does. The
      monotonic time and every pending wait are unchanged.

    `advance` knows a task once it has waited on this clock, or once it is passed to `watch`. A
    timer started directly is watched with `clock.watch(timer.task)`, so the first `advance` waits
    for it to reach its first wait. Time is kept in whole microseconds, so a cron wait that ends
    at its target wakes at exactly the target.
    """

    def __init__(
        self, *, start: datetime = datetime(2026, 1, 1, tzinfo=UTC), settle_within: float = 5.0
    ) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be an aware datetime: the wall time is an instant")
        self._monotonic = 0
        self._wall = (start - _EPOCH) // timedelta(microseconds=1)
        self._sleepers: list[_Sleeper] = []
        self._order = itertools.count()
        self._tasks: set[asyncio.Task[object]] = set()
        self.settle_within = settle_within

    def monotonic(self) -> float:
        return self._monotonic / 1_000_000

    def now(self, tz: tzinfo) -> datetime:
        return (_EPOCH + timedelta(microseconds=self._wall)).astimezone(tz)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        sleeper = self._sleep_until(self._monotonic + _micros(seconds))
        try:
            await sleeper.future
        finally:
            self._forget(sleeper)

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:
        if event.is_set():
            return True
        if timeout <= 0:
            return False
        sleeper = self._sleep_until(self._monotonic + _micros(timeout))
        set_ = asyncio.ensure_future(event.wait())
        either: set[asyncio.Future[Any]] = {sleeper.future, set_}
        try:
            await asyncio.wait(either, return_when=asyncio.FIRST_COMPLETED)
        finally:
            set_.cancel()
            self._forget(sleeper)
        return event.is_set()

    def watch(self, task: asyncio.Task[object] | None) -> None:
        """Make `advance` wait for `task` to wait on this clock or finish, before it moves time."""
        if task is not None:
            self._tasks.add(task)

    async def advance(self, seconds: float) -> None:
        """Move time forward by `seconds`, waking each wait that ends on the way, in order."""
        if seconds < 0:
            raise ValueError("a clock does not go backwards; use step_wall for a wall clock step")
        target = self._monotonic + _micros(seconds)
        await self._settle()
        while True:
            pending = [s for s in self._sleepers if not s.future.done()]
            due = min((s.deadline for s in pending), default=None)
            if due is None or due > target:
                break
            self._move_to(due)
            for sleeper in pending:
                if sleeper.deadline == due:
                    self._forget(sleeper)
                    sleeper.future.set_result(None)
            await self._settle()
        self._move_to(target)
        await self._settle()

    def step_wall(self, seconds: float) -> None:
        """Set the wall clock `seconds` forward (or back, when negative); nothing wakes."""
        self._wall += _micros(seconds)

    def _sleep_until(self, deadline: int) -> _Sleeper:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        sleeper = _Sleeper(
            deadline, next(self._order), asyncio.get_running_loop().create_future(), task
        )
        self._sleepers.append(sleeper)
        return sleeper

    def _forget(self, sleeper: _Sleeper) -> None:
        if sleeper in self._sleepers:
            self._sleepers.remove(sleeper)

    def _move_to(self, monotonic: int) -> None:
        self._wall += monotonic - self._monotonic
        self._monotonic = monotonic

    def _unsettled(self) -> list[asyncio.Task[object]]:
        self._tasks = {t for t in self._tasks if not t.done()}
        asleep = {s.task for s in self._sleepers if not s.future.done()}
        return [t for t in self._tasks if t not in asleep]

    async def _settle(self) -> None:
        give_up = time.monotonic() + self.settle_within
        spins = 0
        while unsettled := self._unsettled():
            if time.monotonic() > give_up:
                names = ", ".join(sorted(t.get_name() for t in unsettled))
                raise AssertionError(
                    f"FakeClock: {names} did not wait on the clock again or finish within "
                    f"{self.settle_within}s of real time"
                )
            await asyncio.sleep(0 if spins < _SPINS_BEFORE_SLEEPING else 0.001)
            spins += 1

    def __repr__(self) -> str:
        return f"FakeClock(monotonic={self.monotonic()}, wall={self.now(UTC).isoformat()})"

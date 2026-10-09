"""A cron loop that woke after more missed occurrences than it counts says it stopped counting.

When the loop wakes after occurrences beyond the one it waited for (a wall clock stepped forward, or
a starved loop), it names them in one warning, counting at most 1000 by date. Past that it says the
later ones were not counted, so a reader does not take 1000 for all of them. The clock and `asyncio`
are the module's own, replaced.
"""

import asyncio
import importlib
from datetime import UTC, datetime
from typing import Any

import pytest
from cliffracer_cron import CronTimer
from loguru import logger

pytestmark = pytest.mark.unit

START = datetime(2026, 1, 2, 12, 0, 30, tzinfo=UTC)
EVERY_MINUTE = "* * * * *"
CRON_MODULE = importlib.import_module("cliffracer_cron.cron")
#: A timer reads time through its clock; the real clock reads this module's `datetime` and waits
#: through its `asyncio`, so a stand-in for either goes here.
CLOCK_MODULE = importlib.import_module("cliffracer.core.clock")


class _Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now
        self.reads = 0
        self.max_reads: int | None = None

    def advance(self, seconds: float) -> None:
        self.now = datetime.fromtimestamp(self.now.timestamp() + seconds, tz=self.now.tzinfo)


def _datetime_reading(clock: _Clock) -> type[datetime]:
    class _Datetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            clock.reads += 1
            if clock.max_reads is not None and clock.reads > clock.max_reads:
                raise RuntimeError("the loop read the clock without waiting")
            return clock.now if tz is None else clock.now.astimezone(tz)

    return _Datetime


class _LoopAsyncio:
    def __init__(self, clock: _Clock, real_asyncio: Any) -> None:
        self._clock = clock
        self._real = real_asyncio
        self.waits: list[float] = []
        self.sleeps: list[float] = []
        self.after_wait: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def wait_for(self, awaitable: Any, timeout: float) -> None:
        awaitable.close()
        self.waits.append(timeout)
        self._clock.advance(timeout)
        if self.after_wait:
            self._clock.advance(self.after_wait.pop(0))
        await self._real.sleep(0)
        raise TimeoutError

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._clock.advance(seconds)
        await self._real.sleep(0)


def _install(monkeypatch, now: datetime = START):
    clock = _Clock(now)
    # A loop that reads the clock without waiting fails by name instead of spinning a test out.
    clock.max_reads = 10_000
    loop = _LoopAsyncio(clock, CLOCK_MODULE.asyncio)
    monkeypatch.setattr(CRON_MODULE, "datetime", _datetime_reading(clock))
    monkeypatch.setattr(CLOCK_MODULE, "datetime", _datetime_reading(clock))
    monkeypatch.setattr(CLOCK_MODULE, "asyncio", loop)
    return clock, loop


def _drive(t: CronTimer, firings) -> None:
    t.method_name = "tick"
    t.is_running = True
    remaining = list(firings)
    t.fired = []  # type: ignore[attr-defined]

    async def execute():
        t.fired.append("fire")  # type: ignore[attr-defined]
        step = remaining.pop(0)
        if not remaining:
            t.is_running = False
        step()

    t._execute_method = execute  # type: ignore[method-assign]


async def _run(t) -> None:
    try:
        await asyncio.wait_for(t._timer_loop(), timeout=5.0)
    except TimeoutError:
        pytest.fail(f"the loop had not finished after 5s; fired {t.fired!r}", pytrace=False)


def _records(level: str):
    said: list[str] = []
    handler = logger.add(lambda m: said.append(m.record["message"]), level=level)
    return said, handler


def _noop() -> None:
    return None


def _warned_after(monkeypatch, minutes_late: int):
    _, loop = _install(monkeypatch)
    loop.after_wait = [minutes_late * 60.0]
    t = CronTimer(EVERY_MINUTE)
    _drive(t, [_noop])
    said, handler = _records("WARNING")
    return said, handler, t


async def _report(monkeypatch, minutes_late: int) -> str:
    said, handler, t = _warned_after(monkeypatch, minutes_late)
    try:
        await _run(t)
    finally:
        logger.remove(handler)
    assert len(said) == 1, said
    return said[0]


async def test_a_wake_past_more_occurrences_than_it_counts_says_it_stopped_counting(monkeypatch):
    """2000 minutes late: the first 1000 are named by date, and the rest are said to be uncounted."""
    said = await _report(monkeypatch, 2000)

    assert (
        "and 1000 more (2026-01-02T12:02:00+00:00 to 2026-01-03T04:41:00+00:00), and later ones not counted:"
        in said
    )


async def test_a_wake_past_exactly_as_many_as_it_counts_counted_them_all(monkeypatch):
    """1000 minutes late misses 1000 occurrences after the one waited for (one at the instant it
    wakes is missed too): all are counted."""
    said = await _report(monkeypatch, 1000)

    assert "and 1000 more (" in said
    assert "not counted" not in said


async def test_CONTROL_a_wake_one_occurrence_late_names_it(monkeypatch):
    said = await _report(monkeypatch, 1)

    assert "and 1 more (2026-01-02T12:02:00+00:00 to 2026-01-02T12:02:00+00:00):" in said

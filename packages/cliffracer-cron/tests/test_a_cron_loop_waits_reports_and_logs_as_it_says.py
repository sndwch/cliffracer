"""What a cron timer's loop waits for, reports and logs.

A bare `@cron` is refused. A job does not run at start unless `eager`. A remainder of under a second
is waited for, not spun on. A schedule that yields one past instant waits for the next future one.
An occurrence at the instant the loop wakes is reported as not run, and a forward step past more
than 1000 occurrences reports the first 1000. A firing that raises is logged as an error, eager or
scheduled. The clock and `asyncio` are the module's own, replaced; the clock refuses to be read
10,000 times without a wait, so a loop that spins fails by name.
"""

import asyncio
import importlib
from datetime import UTC, datetime
from typing import Any

import pytest
from cliffracer_cron import CronTimer, cron
from loguru import logger

from cliffracer.core.exceptions import ConfigurationError

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


def _boom() -> None:
    raise RuntimeError("boom in the firing")


# ---- the decorator ------------------------------------------------------------------------------


def test_a_bare_cron_decorator_is_refused():
    def job(self):
        return None

    with pytest.raises(ConfigurationError, match="decorator factory"):
        cron(job)


async def test_a_cron_job_does_not_run_at_start_unless_asked(monkeypatch):
    @cron(EVERY_MINUTE)
    async def tick(self):
        return None

    (t,) = tick._cliffracer_timers
    assert t.eager is False
    _, loop = _install(monkeypatch)
    _drive(t, [_noop])

    await _run(t)

    assert loop.waits == [30.0]
    assert t.fired == ["fire"]


# ---- the wait -----------------------------------------------------------------------------------


async def test_a_sub_second_remainder_is_waited_for_not_spun_on(monkeypatch):
    """At 12:00:59.5 the next occurrence is half a second away: the loop waits 0.5 s."""
    clock, loop = _install(monkeypatch, now=datetime(2026, 1, 2, 12, 0, 59, 500000, tzinfo=UTC))
    clock.max_reads = 1000  # a loop that re-reads the clock instead of waiting is cut off
    t = CronTimer(EVERY_MINUTE)
    _drive(t, [_noop])

    await _run(t)

    assert t.error_count == 0
    assert loop.waits == [0.5]
    assert t.fired == ["fire"]


async def test_a_schedule_that_yields_one_past_instant_waits_for_the_next_future_one(monkeypatch):
    """A non-future instant from the schedule is skipped once, not treated as a stuck schedule."""
    real_croniter = CRON_MODULE.croniter
    injected = {"done": False}

    class _OnePastInstant:
        def __init__(self, expression, start):
            self._real = real_croniter(expression, start)
            self._start = start

        def get_next(self, kind):
            if not injected["done"]:
                injected["done"] = True
                return datetime.fromtimestamp(self._start.timestamp() - 60, tz=self._start.tzinfo)
            return self._real.get_next(kind)

    _, loop = _install(monkeypatch)
    t = CronTimer(EVERY_MINUTE)
    monkeypatch.setattr(CRON_MODULE, "croniter", _OnePastInstant)
    _drive(t, [_noop])

    await _run(t)

    assert t.error_count == 0
    assert loop.waits == [30.0]
    assert t.fired == ["fire"]


# ---- what a forward step reports ----------------------------------------------------------------


async def test_an_occurrence_at_the_instant_the_loop_wakes_is_reported_as_not_run(monkeypatch):
    """Waited for 12:01, woke at exactly 12:02: 12:02 is passed over and said so."""
    _, loop = _install(monkeypatch)
    loop.after_wait = [60.0]
    t = CronTimer(EVERY_MINUTE)
    _drive(t, [_noop])
    said, handler = _records("WARNING")
    try:
        await _run(t)
    finally:
        logger.remove(handler)

    assert len(said) == 1, said
    assert "and 1 more (2026-01-02T12:02:00+00:00 to 2026-01-02T12:02:00+00:00)" in said[0]


async def test_a_forward_step_past_more_than_1000_occurrences_reports_the_first_1000(monkeypatch):
    """Woke 2000 minutes after the target: the report stops counting at 1000 occurrences."""
    _, loop = _install(monkeypatch)
    loop.after_wait = [2000 * 60.0]
    t = CronTimer(EVERY_MINUTE)
    _drive(t, [_noop])
    said, handler = _records("WARNING")
    try:
        await _run(t)
    finally:
        logger.remove(handler)

    assert len(said) == 1, said
    assert "and 1000 more (2026-01-02T12:02:00+00:00 to 2026-01-03T04:41:00+00:00)" in said[0]


# ---- errors are logged ---------------------------------------------------------------------------


@pytest.mark.parametrize("eager", [True, False], ids=["eager", "scheduled"])
async def test_a_firing_that_raises_is_logged_as_an_error(monkeypatch, eager):
    _install(monkeypatch)
    t = CronTimer(EVERY_MINUTE, eager=eager, error_backoff=5.0)
    _drive(t, [_boom] if not eager else [_boom, _noop])
    said, handler = _records("ERROR")
    try:
        await _run(t)
    finally:
        logger.remove(handler)

    assert t.error_count == 1
    assert len([m for m in said if "tick" in m and "boom in the firing" in m]) == 1, said

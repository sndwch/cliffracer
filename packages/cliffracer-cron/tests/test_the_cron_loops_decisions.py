"""What the two cron loops decide, on a clock they cannot outrun.

`CronTimer` and `DistributedCronTimer` override `Timer._timer_loop`, so the base
timer's loop tests do not reach them. Each override reads `datetime.now(tz)` and
waits with `asyncio.wait_for`, for the next occurrence and the error backoff alike, through names
in a module:
the wait for the next occurrence is `CronTimer`'s and both loops use it, so for the
distributed timer these tests replace the names in its module and in `cron`'s. A wait is
recorded and advances a fake clock instead of passing, and `now` reads that
clock, so each test asserts exactly how long the loop chose to wait.

A loop that stopped reading the module's `datetime` would compute its waits from
the real time of day, and one that stopped routing through the module's
`asyncio` would record nothing. Either way the exact waits asserted below go
red. The CONTROL at the top runs each loop on a `FakeClock` with nothing stood in,
and shows it does not fire 1 µs before its minute and fires once at it: the recorded
waits are what the loop would otherwise wait.

Fixed facts these tests pin:
- The first wait is to the next cron slot from now.
- After a firing that overran, the next wait is to the next slot. Nothing
  catches up and nothing re-bases: `max_drift` is not used by either loop.
- An error before a firing is counted, backed off for `error_backoff`, and the
  next wait is recomputed from the clock after the backoff.
- An eager firing that raises is handled as any firing is, on both loops:
  logged, counted, backed off, and the loop goes on to schedule.
- Starting a running distributed timer again warns and does nothing, even if
  its KvExtension has gone.
"""

import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from cliffracer_cron import CronTimer, DistributedCronTimer
from loguru import logger

from cliffracer.core.exceptions import ConfigurationError
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit

START = datetime(2026, 1, 2, 12, 0, 30, tzinfo=UTC)
EVERY_MINUTE = "* * * * *"


class _Clock:
    def __init__(self, now: datetime = START) -> None:
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


# By module path: the package exports its `cron` decorator under the same name as
# the `cron` module, so `import cliffracer_cron.cron as ...` binds the decorator.
CRON_MODULE = importlib.import_module("cliffracer_cron.cron")
#: A timer reads time through its clock; the real clock reads this module's `datetime` and waits
#: through its `asyncio`, so a stand-in for either goes here.
CLOCK_MODULE = importlib.import_module("cliffracer.core.clock")
MODULES = {
    CronTimer: CRON_MODULE,
    DistributedCronTimer: importlib.import_module("cliffracer_cron.distributed"),
}


def _install(monkeypatch, timer_cls, *, now: datetime = START):
    module = MODULES[timer_cls]
    clock = _Clock(now)
    loop = _LoopAsyncio(clock, CLOCK_MODULE.asyncio)
    # Both loops read the time and wait through the timer's clock, which reads the clock module's
    # names; the timer's own module reads `datetime` too, to check its expression when it is built.
    for patched in {module, CRON_MODULE, CLOCK_MODULE}:
        monkeypatch.setattr(patched, "datetime", _datetime_reading(clock))
    monkeypatch.setattr(CLOCK_MODULE, "asyncio", loop)
    return clock, loop


def _timer(timer_cls, firings, *, expression=EVERY_MINUTE, **kwargs):
    """A timer whose loop runs exactly `len(firings)` firings, then stops.

    Each firing is a callable taking nothing. For the distributed timer the
    firing replaces `_execute_distributed`, which records its target.
    """
    t = timer_cls(expression, **kwargs)
    t.method_name = "tick"
    t.is_running = True
    remaining = list(firings)
    t.fired: list[Any] = []  # type: ignore[attr-defined]

    def fire(record: Any) -> None:
        t.fired.append(record)  # type: ignore[attr-defined]
        step = remaining.pop(0)
        if not remaining:
            t.is_running = False
        step()

    if timer_cls is DistributedCronTimer:

        async def execute_distributed(target_time, eager=False):
            fire("eager" if eager else target_time.isoformat())

        t._execute_distributed = execute_distributed  # type: ignore[method-assign]
    else:

        async def execute():
            fire("fire")

        t._execute_method = execute  # type: ignore[method-assign]
    return t


TIMERS = pytest.mark.parametrize(
    "timer_cls", [CronTimer, DistributedCronTimer], ids=["cron", "distributed"]
)


def _noop() -> None:
    return None


async def _run(t) -> None:
    """The loop, bounded in real time. Every wait here is recorded, not real, so
    a loop that stopped routing through its module's `asyncio` would really
    wait for the next minute; the bound turns that into a failure, not a hang.
    So does a loop that never fires and so spins on the recorded waits: they
    yield to the event loop, which is what lets the bound stop it."""
    try:
        await asyncio.wait_for(t._timer_loop(), timeout=2.0)
    except TimeoutError:
        pytest.fail(
            f"the loop had not finished after 2s of real time; it fired {len(t.fired)} time(s): "
            f"{t.fired!r}",
            pytrace=False,
        )


@pytest.mark.timeout(10)
async def test_CONTROL_a_stand_in_wait_yields_so_the_bound_can_stop_a_loop_that_never_fires():
    """`_run` bounds a loop with `asyncio.wait_for`, which can only stop a task at a
    suspension point. A loop whose scheduled firing never happens has no other
    `await`, so if the stand-in waits do not yield it holds the event loop for
    good and the bound never fires: the test then fails at the suite's per-test
    ceiling, minutes later, with a stack inside croniter."""
    stand_in = _LoopAsyncio(_Clock(START), asyncio)

    async def a_loop_that_never_fires() -> None:
        while True:
            try:
                await stand_in.wait_for(asyncio.sleep(0), timeout=60.0)
            except TimeoutError:
                pass
            await stand_in.sleep(1.0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(a_loop_that_never_fires(), timeout=0.2)


@TIMERS
async def test_CONTROL_on_a_clock_alone_the_loop_waits_for_its_minute(timer_cls):
    """Nothing stood in but a `FakeClock`: the loop has not fired 1 µs before 12:01 and fires
    once, for 12:01, at 12:01, so the rows above that stand in for the clock module's `asyncio`
    are not what makes the loop wait."""
    clock = FakeClock(start=START)
    t = _timer(timer_cls, [_noop], clock=clock)
    task = asyncio.create_task(t._timer_loop())
    clock.watch(task)
    minute = datetime(2026, 1, 2, 12, 1, tzinfo=UTC)

    await clock.advance((minute - START).total_seconds() - 1e-6)
    assert t.fired == [] and not task.done()
    await clock.advance(1e-6)
    assert t.fired == ["fire" if timer_cls is CronTimer else minute.isoformat()]
    await task


@TIMERS
async def test_each_wait_is_to_the_next_cron_slot(monkeypatch, timer_cls):
    _, loop = _install(monkeypatch, timer_cls)
    t = _timer(timer_cls, [_noop, _noop])

    await _run(t)

    assert loop.waits == [30.0, 60.0]
    assert loop.sleeps == []
    if timer_cls is DistributedCronTimer:
        assert t.fired == ["2026-01-02T12:01:00+00:00", "2026-01-02T12:02:00+00:00"]


@TIMERS
@pytest.mark.parametrize(
    ("now", "expression", "expected_wait", "expected_target"),
    [
        (
            datetime(2026, 3, 8, 1, 59, 30, tzinfo=ZoneInfo("America/Chicago")),
            "0 3 * * *",
            30.0,
            "2026-03-08T03:00:00-05:00",
        ),
        (
            datetime(2026, 11, 1, 1, 59, 30, tzinfo=ZoneInfo("America/Chicago")),
            EVERY_MINUTE,
            30.0,
            "2026-11-01T01:00:00-06:00",
        ),
        (
            datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/Chicago")),
            "30 1 * * *",
            3600.0,
            "2026-11-01T01:30:00-06:00",
        ),
    ],
    ids=["spring-forward", "fall-back-next-minute", "fall-back-repeated-slot"],
)
async def test_a_store_schedule_waits_for_the_real_elapsed_time_across_dst(
    monkeypatch, timer_cls, now, expression, expected_wait, expected_target
):
    """A store's local opening and reconciliation jobs wait until their actual instants.

    The repeated 01:30 slot is a second cron occurrence, but it remains an hour
    away. It cannot become an immediate duplicate firing or a busy loop.
    """
    _, loop = _install(monkeypatch, timer_cls, now=now)
    t = _timer(timer_cls, [_noop], expression=expression, tz="America/Chicago")

    await _run(t)

    assert loop.waits == [expected_wait]
    if timer_cls is DistributedCronTimer:
        assert t.fired == [expected_target]


@TIMERS
async def test_an_eager_timer_fires_before_its_first_wait(monkeypatch, timer_cls):
    _, loop = _install(monkeypatch, timer_cls)
    t = _timer(timer_cls, [_noop, _noop], eager=True)
    real_fired = t.fired

    order: list[str] = []
    original_wait_for = loop.wait_for

    async def wait_for(awaitable, timeout):
        order.append(f"wait after {len(real_fired)} firing(s)")
        return await original_wait_for(awaitable, timeout)

    loop.wait_for = wait_for  # type: ignore[method-assign]

    await _run(t)

    assert order == ["wait after 1 firing(s)"]
    assert loop.waits == [30.0]
    if timer_cls is DistributedCronTimer:
        assert t.fired == ["eager", "2026-01-02T12:01:00+00:00"]


@TIMERS
async def test_an_overrun_neither_catches_up_nor_rebases(monkeypatch, timer_cls):
    """A firing that runs 45 seconds past 12:01 is followed by a wait to 12:02, whatever
    `max_drift` says. The base Timer would re-base; the cron loops do not use it."""
    clock, loop = _install(monkeypatch, timer_cls)
    t = _timer(timer_cls, [lambda: clock.advance(45.0), _noop], max_drift=0.5)

    await _run(t)

    assert loop.waits == [30.0, 15.0]


def _fails_once(real, message):
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(message)
        return real(*args, **kwargs)

    return wrapper


async def test_a_cron_loop_error_backs_off_and_reschedules_from_the_clock(monkeypatch):
    """`_next_fire` runs inside the loop's try and outside `_execute_method`'s."""
    _, loop = _install(monkeypatch, CronTimer)
    t = _timer(CronTimer, [_noop], error_backoff=5.0)
    t._next_fire = _fails_once(t._next_fire, "croniter failed")  # type: ignore[method-assign]

    await _run(t)

    assert t.error_count == 1
    assert loop.events == ["wait 5", "wait 25"]
    assert t.fired == ["fire"]


class _Bucket:
    """Enough of a KV bucket for one uncontended distributed firing."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str):
        import nats.js.errors

        raise nats.js.errors.KeyNotFoundError()

    async def create(self, key: str, value: bytes) -> int:
        self.store[key] = value
        return 1

    async def put(self, key: str, value: bytes) -> int:
        return 2

    async def update(self, key: str, value: bytes, last: int) -> int:
        return 3

    async def delete(self, key: str, last: int | None = None) -> None:
        self.store.pop(key, None)


async def test_a_distributed_loop_error_backs_off_and_reschedules_from_the_clock(monkeypatch):
    """`_execute_distributed` awaits `_get_raw_bucket` before its own try, so an
    unavailable bucket raises into the loop."""
    _, loop = _install(monkeypatch, DistributedCronTimer)
    t = DistributedCronTimer(EVERY_MINUTE, error_backoff=5.0)
    t.method_name = "tick"
    t.is_running = True
    bucket = _Bucket()
    calls = {"n": 0}

    async def get_raw_bucket():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConfigurationError("no KvExtension")
        return bucket

    async def execute_method():
        t.is_running = False

    t._get_raw_bucket = get_raw_bucket  # type: ignore[method-assign]
    t._execute_method = execute_method  # type: ignore[method-assign]

    await _run(t)

    assert t.error_count == 1
    assert loop.events == ["wait 30", "wait 5", "wait 55"]
    assert list(bucket.store) == ["cron.cliffracer.tick.1767355320"]


async def test_a_distributed_eager_error_is_handled_like_a_loop_error(monkeypatch):
    _, loop = _install(monkeypatch, DistributedCronTimer)
    t = _timer(DistributedCronTimer, [_noop], eager=True, error_backoff=5.0)
    scheduled = t._execute_distributed

    async def execute_distributed(target_time, eager=False):
        if eager:
            raise RuntimeError("bucket unavailable at start")
        await scheduled(target_time, eager=eager)

    t._execute_distributed = execute_distributed  # type: ignore[method-assign]
    errors: list[str] = []
    sink = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
    try:
        await _run(t)
    finally:
        logger.remove(sink)

    assert [e for e in errors if "bucket unavailable at start" in e] == [
        "Error in distributed cron loop for tick: bucket unavailable at start"
    ]
    assert t.error_count == 1
    assert loop.events == ["wait 5", "wait 25"]
    assert t.fired == ["2026-01-02T12:01:00+00:00"]


class _KvService:
    def __init__(self, kv: Any) -> None:
        self.kv = kv


async def _parked_loop() -> None:
    await asyncio.Event().wait()


async def test_starting_a_running_distributed_timer_again_warns_even_without_its_kv():
    t = DistributedCronTimer(EVERY_MINUTE)
    t.method_name = "tick"
    t._timer_loop = _parked_loop  # type: ignore[method-assign]
    service = _KvService(kv=SimpleNamespace(get_bucket=AsyncMock()))
    await t.start(service)
    first_task = t.task
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        service.kv = None
        await t.start(service)
    finally:
        logger.remove(sink)
        await t.stop()

    assert t.task is first_task
    assert "Timer tick already running" in warnings


async def test_CONTROL_starting_a_distributed_timer_without_kv_is_refused():
    t = DistributedCronTimer(EVERY_MINUTE)
    t.method_name = "tick"
    t._timer_loop = _parked_loop  # type: ignore[method-assign]

    with pytest.raises(ConfigurationError, match="no KvExtension"):
        await t.start(_KvService(kv=None))
    assert t.is_running is False


def _raise_before_the_method_runs() -> None:
    raise RuntimeError("raised before _execute_method's own try")


async def test_a_cron_eager_error_is_handled_like_a_loop_error(monkeypatch):
    _, loop = _install(monkeypatch, CronTimer)
    t = _timer(CronTimer, [_raise_before_the_method_runs, _noop], eager=True, error_backoff=5.0)

    await _run(t)

    assert t.error_count == 1
    assert loop.events == ["wait 5", "wait 25"]
    assert t.fired == ["fire", "fire"]


class _LookupRaises:
    """A service whose timer method cannot be read: `_execute_method` reads it
    with getattr before its own try, so the exception reaches the loop."""

    @property
    def tick(self):
        raise RuntimeError("lookup raised")


@pytest.mark.parametrize("eager", [True, False], ids=["eager", "CONTROL-not-eager"])
async def test_a_started_cron_timer_survives_a_firing_that_raises(eager):
    """The real task, on a clock. An eager CronTimer used to end with the exception while
    `is_running` stayed True and nothing was logged or counted.

    The schedule is yearly and the clock starts in June, so the eager firing (or, not eager,
    nothing) is all that happens until the clock is moved to New Year, when the scheduled
    firing raises too. The task survives both and counts each."""
    start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start=start)
    t = CronTimer("0 0 1 1 *", eager=eager, error_backoff=0.01, clock=clock)
    t.method_name = "tick"
    await t.start(_LookupRaises())
    clock.watch(t.task)
    try:
        await clock.advance(0.01)  # eager: the eager firing has raised and its backoff has passed
        assert not t.task.done(), t.task
        assert t.error_count == (1 if eager else 0)
        await clock.advance((datetime(2027, 1, 1, tzinfo=UTC) - start).total_seconds())
        assert not t.task.done(), t.task
        assert t.error_count == (2 if eager else 1)
    finally:
        await t.stop()


# ---- the wall clock stepped during a wait ---------------------------------------------------------
#
# The wait runs on the monotonic clock and its target is a wall-clock time. A wall clock stepped back
# during the wait used to fire the occurrence early and then, recomputing from the earlier clock, fire
# the same occurrence again; one stepped forward skipped the occurrences it passed with no trace. The
# loop now confirms the wall clock reached the target before it fires, and logs the occurrences a
# forward step passed over.


def _step_after_each_wait(loop, clock, steps: list[float]) -> None:
    """After a wait returns, the wall clock moves by the next step: a clock set while it slept."""
    real_wait_for = loop.wait_for

    async def wait_for(awaitable, timeout):
        try:
            await real_wait_for(awaitable, timeout)
        finally:
            if steps:
                clock.advance(steps.pop(0))

    loop.wait_for = wait_for


async def _warnings_during(run) -> list[str]:
    said: list[str] = []
    handler = logger.add(lambda m: said.append(m.record["message"]), level="WARNING")
    try:
        await run()
    finally:
        logger.remove(handler)
    return said


async def test_CONTROL_with_no_step_it_fires_once_at_the_target(monkeypatch):
    clock, loop = _install(monkeypatch, CronTimer)
    seen: list[datetime] = []
    timer = _timer(CronTimer, [lambda: seen.append(clock.now)])

    said = await _warnings_during(lambda: _run(timer))

    assert loop.waits == [30.0]
    assert seen == [datetime(2026, 1, 2, 12, 1, 0, tzinfo=UTC)]
    assert said == []


async def test_a_clock_stepped_back_during_the_wait_is_waited_out_not_fired_early(monkeypatch):
    clock, loop = _install(monkeypatch, CronTimer)
    _step_after_each_wait(loop, clock, [-20.0])
    seen: list[datetime] = []
    timer = _timer(CronTimer, [lambda: seen.append(clock.now)])

    await _run(timer)

    # 12:00:30 -> wait 30 -> the clock reads 12:00:40 after the step back, short of 12:01:00.
    assert loop.waits == [30.0, 20.0], loop.waits
    assert seen == [datetime(2026, 1, 2, 12, 1, 0, tzinfo=UTC)], seen
    assert timer.fired == ["fire"]


async def test_a_step_back_does_not_fire_the_same_occurrence_twice(monkeypatch):
    """Two firings are asked for: the second must be the NEXT occurrence, not the same one again."""
    clock, loop = _install(monkeypatch, CronTimer)
    _step_after_each_wait(loop, clock, [-20.0, 0.0])
    seen: list[datetime] = []
    timer = _timer(CronTimer, [lambda: seen.append(clock.now), lambda: seen.append(clock.now)])

    await _run(timer)

    assert seen == [
        datetime(2026, 1, 2, 12, 1, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 12, 2, 0, tzinfo=UTC),
    ], seen


@TIMERS
async def test_a_clock_stepped_forward_past_occurrences_says_which_were_not_run(
    monkeypatch, timer_cls
):
    clock, loop = _install(monkeypatch, timer_cls)
    _step_after_each_wait(loop, clock, [150.0])  # woke at 12:03:30; target 12:01:00
    timer = _timer(timer_cls, [lambda: None])

    said = await _warnings_during(lambda: _run(timer))

    assert len(said) == 1, said
    assert "2 more" in said[0] and "this replica does not run those" in said[0], said[0]
    assert "2026-01-02T12:02:00" in said[0] and "2026-01-02T12:03:00" in said[0], said[0]
    assert len(timer.fired) == 1, "the occurrence it waited for is still fired, once"


@TIMERS
async def test_a_wake_a_little_after_the_target_is_not_reported(monkeypatch, timer_cls):
    clock, loop = _install(monkeypatch, timer_cls)
    _step_after_each_wait(loop, clock, [0.5])
    timer = _timer(timer_cls, [lambda: None])

    said = await _warnings_during(lambda: _run(timer))

    assert said == []

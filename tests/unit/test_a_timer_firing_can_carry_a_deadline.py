"""A timer declared with `deadline=` bounds each firing by it, and the calls the firing makes.

Unset, a firing has no deadline, as before. Set, the firing runs inside it, hooks included: it is
the current deadline (`set_by="timer"`), so a call the firing makes waits at most what is left, and
a firing still running at it is cancelled and counted as an error, `DeadlineExceeded`. The deadline
is on the event loop's clock, as a request's is, so these rows give a firing a short real deadline
and a handler that would run far longer, and assert what happened, never how long it took.
"""

import asyncio
import math

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig
from cliffracer.core.deadline import current, outbound_timeout
from cliffracer.core.timer import Timer, timer

pytestmark = pytest.mark.unit

#: A deadline short enough to pass quickly, and a handler wait far longer than it.
DEADLINE = 0.05
LONG = 5.0
#: The outer bound on a wait that only a defect can make long.
NEVER = 10.0


class Host:
    """A host for a timer, with no container: the firing calls the method directly."""

    def __init__(self) -> None:
        self.seen: list = []
        self.ran = 0

    async def run(self) -> None:
        self.ran += 1
        self.seen.append(current())
        await asyncio.sleep(LONG)

    async def quick(self) -> None:
        self.seen.append(current())
        self.seen.append(outbound_timeout(30.0, "a nested call"))

    async def swallows(self) -> str:
        try:
            await asyncio.sleep(LONG)
        except asyncio.CancelledError:
            return "returned anyway"
        return "finished"


def _timer(host: object, method: str, **kwargs) -> Timer:
    t = Timer(interval=60.0, **kwargs)
    t.method_name = method
    t.service_instance = host
    return t


async def _fire(t: Timer) -> None:
    await asyncio.wait_for(t._execute_method(), NEVER)


# --- the declaration ------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, True, "1"])
def test_a_deadline_that_is_not_a_positive_finite_number_is_refused_when_declared(bad):
    with pytest.raises(ConfigurationError, match="Timer deadline must be a finite number"):
        Timer(interval=1.0, deadline=bad)

    async def tick(self) -> None: ...

    with pytest.raises(ConfigurationError, match="Timer deadline must be a finite number"):
        timer(interval=1.0, deadline=bad)(tick)


def test_a_clone_keeps_the_deadline():
    assert Timer(interval=1.0, deadline=2.5).clone().deadline == 2.5
    assert Timer(interval=1.0).clone().deadline is None


# --- a firing under its deadline ------------------------------------------------------------


async def test_CONTROL_a_firing_with_no_deadline_has_none_and_runs_to_its_end():
    host = Host()
    t = _timer(host, "quick")

    await _fire(t)

    assert host.seen == [None, 30.0]
    assert t.error_count == 0


async def test_a_firing_runs_with_its_deadline_current_and_a_nested_call_gets_what_is_left():
    host = Host()
    t = _timer(host, "quick", deadline=2.0)

    await _fire(t)

    deadline, nested = host.seen
    assert (deadline.budget, deadline.set_by) == (2.0, "timer")
    assert 0 < nested <= 2.0
    assert current() is None, "the deadline outlived its firing"


async def test_a_firing_still_running_at_its_deadline_is_cancelled_and_counted_an_error():
    host = Host()
    t = _timer(host, "run", deadline=DEADLINE)

    await _fire(t)

    assert host.ran == 1
    assert (t.error_count, t.last_error_type) == (1, "DeadlineExceeded")
    assert t.last_error is not None
    assert t.last_error.startswith(
        f"run exceeded its deadline of {DEADLINE:.3f}s (set on its timer)"
    )
    assert t.refusal_count == 0


async def test_a_firing_that_suppresses_the_cancellation_and_returns_is_still_cut_off():
    t = _timer(Host(), "swallows", deadline=DEADLINE)

    await _fire(t)

    assert (t.error_count, t.last_error_type) == (1, "DeadlineExceeded")


class Ticking(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="ticking", health_port=0))
        self.seen: list = []

    async def tick(self) -> None:
        self.seen.append(current())
        await asyncio.sleep(LONG)


async def test_a_firing_through_the_services_hooks_is_bounded_by_its_deadline():
    service = Ticking()
    t = _timer(service, "tick", deadline=DEADLINE)

    await _fire(t)

    assert [d.set_by for d in service.seen] == ["timer"]
    assert (t.error_count, t.last_error_type) == (1, "DeadlineExceeded")


async def test_an_eager_firing_gets_the_same_deadline():
    host = Host()
    t = Timer(interval=600.0, eager=True, deadline=DEADLINE)
    t.method_name = "run"
    await t.start(host)
    try:

        async def cut_off() -> None:
            while t.error_count == 0:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(cut_off(), NEVER)
    finally:
        await t.stop()

    assert (host.ran, t.last_error_type) == (1, "DeadlineExceeded")
    assert host.seen[0].budget == DEADLINE


async def test_a_firing_that_keeps_running_past_its_deadline_is_named_at_twice_its_budget():
    """One that catches the cancellation and carries on holds the timer, and is named."""
    from loguru import logger

    release = asyncio.Event()

    class Stubborn:
        async def run(self) -> None:
            try:
                await asyncio.sleep(LONG)
            except asyncio.CancelledError:
                await release.wait()  # it does not stop, and is not cancelled again

    loop = asyncio.get_running_loop()
    lines: list[tuple[float, str]] = []
    sink = logger.add(lambda message: lines.append((loop.time(), str(message))), level="WARNING")
    t = _timer(Stubborn(), "run", deadline=DEADLINE)
    began = loop.time()
    firing = asyncio.create_task(t._execute_method())
    try:

        async def named() -> None:
            while not any("still running" in line for _, line in lines):
                await asyncio.sleep(0.005)

        try:
            await asyncio.wait_for(named(), NEVER)
        except TimeoutError:
            raise AssertionError(
                f"no warning named the firing still running {NEVER}s after it began"
            ) from None
        assert not firing.done(), "the firing ended before it was named"
    finally:
        release.set()
        await asyncio.wait_for(firing, NEVER)
        logger.remove(sink)

    ((at, line),) = [(at, line) for at, line in lines if "still running" in line]
    assert f"run is still running {DEADLINE:.3f}s after being cancelled at its deadline" in line
    # The warning is set for the deadline plus the budget again, on the loop's clock, so it cannot
    # come before twice the budget from the firing's start. A lower bound only: it may come later.
    assert at - began >= 2 * DEADLINE, f"the warning came {at - began:.3f}s after the firing began"
    assert (t.error_count, t.last_error_type) == (1, "DeadlineExceeded")

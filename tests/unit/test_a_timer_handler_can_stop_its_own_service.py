"""A timer handler that stops its own service finishes the stop instead of waiting on itself.

`Timer.stop` runs inside the timer's own task when the handler calls `service.stop()`: a watchdog, a
one-shot job, a task that retires the service when its work is done. It waited for that task for the
grace, and then cancelled it and awaited it, a cancel cycle that ended in `RecursionError` with the
stop never returning, no `on_shutdown`, no extension stop and the connection still open. It now
marks the timer stopped and returns, and the loop ends when the handler does.
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core import dial
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit


@pytest.fixture
def broker(monkeypatch):
    """A connection that answers every call, so a service starts and stops without a broker."""
    made: list[AsyncMock] = []

    async def connect(*_args, **_kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False

        async def subscribe(*_a, **_k):
            return AsyncMock()

        async def close():
            nc.is_closed, nc.is_connected = True, False

        nc.subscribe, nc.close = subscribe, close
        made.append(nc)
        return nc

    monkeypatch.setattr(dial, "connect", connect)
    return made


def _loop_errors() -> list[BaseException | None]:
    """The exceptions the event loop reports, which is where a cancel cycle's RecursionError lands."""
    seen: list[BaseException | None] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: seen.append(context.get("exception"))
    )
    return seen


class Retiring(CliffracerService):
    def __init__(self, *, eager: bool, grace: float = 1.0):
        super().__init__(ServiceConfig(name="retiring", health_port=0, shutdown_timeout=grace))
        self.finished = asyncio.Event()
        self.shutdown_ran = False

    async def on_shutdown(self):
        self.shutdown_ran = True


class OnInterval(Retiring):
    @timer(interval=0.05)
    async def watchdog(self):
        await self.stop()
        self.finished.set()


class Eager(Retiring):
    @timer(interval=30, eager=True)
    async def watchdog(self):
        await self.stop()
        self.finished.set()


@pytest.fixture
def clock(monkeypatch):
    """A fake clock on the timers `OnInterval` and `Eager` declare, which each service clones."""
    clock = FakeClock()
    for service_class in (OnInterval, Eager):
        for declared in service_class.watchdog._cliffracer_timers:
            monkeypatch.setattr(declared, "clock", clock)
    return clock


async def _started(svc: CliffracerService, clock: FakeClock) -> None:
    await svc.start()
    for started in svc.container.registry.timers:
        clock.watch(started.task)


def _on(clock: FakeClock, instance) -> None:
    instance.clock = clock


@pytest.mark.parametrize("service_class", [OnInterval, Eager])
async def test_a_handler_that_stops_its_service_returns_and_the_service_is_stopped(
    broker, clock, service_class
):
    loop_errors = _loop_errors()
    svc = service_class(eager=service_class is Eager)
    started = time.monotonic()
    await _started(svc, clock)

    await clock.advance(0.05)
    await asyncio.wait_for(svc.finished.wait(), timeout=5)

    # Upper bound. CI p99 0.0567 s (run 4712: eric-7, CPython 3.12.15, n=40, p99 = max); 53x p99.
    assert time.monotonic() - started < 3, "the stop waited for its own grace"
    assert svc.container.is_stopped and svc.shutdown_ran
    assert broker[0].is_closed
    assert loop_errors == []


async def test_the_timer_does_not_fire_again_and_its_task_ends(broker, clock):
    loop_errors = _loop_errors()
    svc = OnInterval(eager=False)
    await _started(svc, clock)
    await clock.advance(0.05)
    await asyncio.wait_for(svc.finished.wait(), timeout=5)
    (instance,) = svc.container.registry.timers

    await clock.advance(0.3)  # six more intervals: a timer still running would fire in each

    assert instance.execution_count == 1
    assert not instance.is_running
    assert instance.task is not None and instance.task.done()
    assert loop_errors == []


async def test_a_timers_own_stop_with_a_grace_does_not_wait_for_the_grace(broker):
    loop_errors = _loop_errors()

    class Svc(CliffracerService):
        @timer(interval=0.05)
        async def tick(self):
            started = time.monotonic()
            await instance.stop(grace=30)
            waited.append(time.monotonic() - started)

    waited: list[float] = []
    clock = FakeClock()
    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    (instance,) = svc.container.registry.timers
    _on(clock, instance)
    await instance.start(svc)
    clock.watch(instance.task)

    await clock.advance(0.4)

    # Upper bound. CI p99 0.00013 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 7716x p99.
    assert len(waited) == 1 and waited[0] < 1.0
    assert not instance.is_running and loop_errors == []


async def test_CONTROL_a_stop_from_outside_still_gives_a_run_in_flight_its_grace(broker):
    loop_errors = _loop_errors()
    finished: list[float] = []

    class Svc(CliffracerService):
        @timer(interval=0.05)
        async def tick(self):
            await asyncio.sleep(0.4)
            finished.append(time.monotonic())

    clock = FakeClock()
    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    (instance,) = svc.container.registry.timers
    _on(clock, instance)
    await instance.start(svc)
    clock.watch(instance.task)
    # The first interval ends and the first run starts; `advance` returns only once that run is
    # over, so it is left running while the stop comes from outside.
    advancing = asyncio.create_task(clock.advance(0.05))
    await asyncio.wait_for(_until(lambda: instance._executing), timeout=5)

    await instance.stop(grace=2.0)
    await advancing

    assert len(finished) == 1, "the run was cut off instead of finishing within its grace"
    assert instance.task is not None and instance.task.done()
    assert loop_errors == []


async def test_a_task_a_finished_run_left_behind_is_not_mistaken_for_the_run_in_flight(broker):
    """Only the run in flight is the caller's own: a stale run's task waits for the current one."""
    loop_errors = _loop_errors()
    runs: list[int] = []
    stop_took: list[float] = []

    class Svc(CliffracerService):
        @timer(interval=0.05)
        async def tick(self):
            runs.append(1)
            if len(runs) == 1:

                async def later():
                    await asyncio.sleep(0.25)  # run 1 is over by now, and run 2 is in flight
                    started = time.monotonic()
                    await instance.stop(grace=5)
                    stop_took.append(time.monotonic() - started)

                self.detached = asyncio.create_task(later())
                return
            await asyncio.sleep(1.0)

    clock = FakeClock()
    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    (instance,) = svc.container.registry.timers
    _on(clock, instance)
    await instance.start(svc)
    clock.watch(instance.task)

    # Run 1 at 0.05 and run 2 at 0.1; `advance` returns when the stop has ended the timer.
    await clock.advance(0.1)
    await asyncio.wait_for(_until(lambda: stop_took), timeout=6)

    # Lower bound: run 2 sleeps 1.0 s; a stop that did not wait for it returns near 0. Load can only
    # lengthen it.
    assert stop_took[0] > 0.5, "the stop returned at once instead of waiting for the run in flight"
    assert loop_errors == []


async def test_a_task_a_handler_left_behind_stops_an_idle_timer_the_ordinary_way(broker):
    """After the run is over the timer is idle, and a stop from a task the run created waits for it."""
    loop_errors = _loop_errors()
    outcome: list[bool] = []

    class Svc(CliffracerService):
        @timer(interval=30, eager=True)
        async def tick(self):
            async def later():
                await asyncio.sleep(0.2)  # the run is over, the timer is idle until the next one
                await instance.stop(grace=5)
                outcome.append(instance.task is not None and instance.task.done())

            self.detached = asyncio.create_task(later())

    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    (instance,) = svc.container.registry.timers
    await instance.start(svc)

    await asyncio.wait_for(_until(lambda: outcome), timeout=6)

    assert outcome == [True], "the stop returned before the idle timer's task had ended"
    assert loop_errors == []


async def test_a_finished_run_leaves_nothing_marked_in_the_timers_task(broker):
    from cliffracer.core.timer import _RUNNING

    class Svc(CliffracerService):
        ran = asyncio.Event()

        @timer(interval=30, eager=True)
        async def tick(self):
            assert _RUNNING.get() is not None, "a run is not marked while it is in flight"
            self.ran.set()

    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    (instance,) = svc.container.registry.timers
    await instance.start(svc)
    await asyncio.wait_for(svc.ran.wait(), timeout=5)
    await asyncio.sleep(0.05)  # the run has returned and the loop is waiting for the next one

    assert instance.task is not None
    assert instance.task.get_context().get(_RUNNING) is None
    await instance.stop()


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.02)

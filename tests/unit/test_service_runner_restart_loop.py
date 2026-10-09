"""Start-attempt accounting and the paths by which the runner's loop ends."""

import asyncio
import threading
import time

import pytest
from loguru import logger

from cliffracer.core import ServiceConfig
from cliffracer.core.container import BrokerConnectionState
from cliffracer.runners.orchestrator import ServiceRunner
from cliffracer.testing import wait_until

pytestmark = pytest.mark.unit

# The runner builds a fresh instance on every iteration, so a per-instance
# counter would reset with it. These count on the class.


class AlwaysCrashesOnStart:
    """Self-configuring service whose start() always raises."""

    start_calls = 0

    def __init__(self):
        self.config = ServiceConfig(name="flapper", health_port=0, restart_delay=0.01)

    async def start(self):
        type(self).start_calls += 1
        raise RuntimeError("start fails")

    async def stop(self):
        pass


class UnconstructableService:
    """Service whose constructor cannot be satisfied by one positional config."""

    construction_attempts = 0

    def __new__(cls, *args, **kwargs):
        # Counted here because the TypeError comes from binding __init__, which
        # means the __init__ body never runs to count it.
        cls.construction_attempts += 1
        return super().__new__(cls)

    def __init__(self, config, required_extra):
        self.config = config


class CrashesAndIsNotRestarted:
    """Self-configuring service that crashes on start and is not restarted."""

    start_calls = 0

    def __init__(self):
        self.config = ServiceConfig(name="crash_once", health_port=0, auto_restart=False)

    async def start(self):
        type(self).start_calls += 1
        raise RuntimeError("boom")

    async def stop(self):
        pass


class StopsWhenTheBrokerCloses:
    """Self-configuring service, not restarted, whose broker closes after start."""

    def __init__(self):
        self.config = ServiceConfig(name="until_broker_closes", health_port=0, auto_restart=False)
        self.broker_state = BrokerConnectionState.CONNECTED
        self.stopped = False

    async def start(self):
        self.broker_state = BrokerConnectionState.CLOSED

    async def stop(self):
        self.stopped = True


class RunsUntilShutdown:
    """Self-configuring service that runs until the runner is shut down."""

    def __init__(self):
        self.config = ServiceConfig(name="until_shutdown", health_port=0)
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True


async def test_every_start_attempt_is_logged_with_its_own_number():
    """A flapping service logs a rising attempt number, so the flapping is visible.

    The attempt number is the only per-iteration reading an operator gets, so
    it is read here out of the emitted log line rather than off the counter.
    """
    AlwaysCrashesOnStart.start_calls = 0
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    runner = ServiceRunner(AlwaysCrashesOnStart)
    runner._running = True
    task = asyncio.create_task(runner._run_service())
    try:
        await wait_until(
            lambda: AlwaysCrashesOnStart.start_calls >= 4,
            within=5,
            reason="the flapping service records four start attempts",
        )
    finally:
        runner._running = False
        runner._shutdown_event.set()
        await asyncio.wait_for(task, timeout=5.0)
        logger.remove(sink)

    numbers = [int(m.split("attempt #")[1].rstrip(")")) for m in messages if "attempt #" in m]
    assert len(numbers) >= 4
    assert numbers == list(range(1, len(numbers) + 1))
    assert runner._start_attempts == len(numbers)
    assert runner._successful_starts == 0


async def test_a_constructor_argument_mismatch_is_attempted_once():
    """Waiting does not change a signature, so the runner stops instead of retrying."""
    UnconstructableService.construction_attempts = 0
    cfg = ServiceConfig(name="unconstructable", restart_delay=0.01)
    runner = ServiceRunner(UnconstructableService, config=cfg)
    runner._running = True
    await asyncio.wait_for(runner._run_service(), timeout=5.0)
    assert UnconstructableService.construction_attempts == 1
    assert runner._start_attempts == 0


async def test_run_returns_when_construction_fails_permanently():
    """The runner's entry point returns rather than outliving the service loop."""
    UnconstructableService.construction_attempts = 0
    cfg = ServiceConfig(name="unconstructable", restart_delay=0.01)
    runner = ServiceRunner(UnconstructableService, config=cfg)
    await asyncio.wait_for(runner.run(), timeout=5.0)
    assert UnconstructableService.construction_attempts == 1


async def test_run_returns_when_a_crash_is_not_restarted():
    """A service that crashed and is not to be restarted ends the runner."""
    CrashesAndIsNotRestarted.start_calls = 0
    runner = ServiceRunner(CrashesAndIsNotRestarted)
    await asyncio.wait_for(runner.run(), timeout=5.0)
    assert CrashesAndIsNotRestarted.start_calls == 1


async def test_run_returns_when_a_service_that_is_not_restarted_finishes():
    """A closed broker ends a service that is not restarted, and the runner with it."""
    runner = ServiceRunner(StopsWhenTheBrokerCloses)
    await asyncio.wait_for(runner.run(), timeout=15.0)
    assert runner.service is not None
    assert runner.service.stopped is True
    assert runner._successful_starts == 1


async def test_run_returns_after_shutdown_stops_the_service():
    """The shutdown path still waits for the service to be stopped before returning."""
    runner = ServiceRunner(RunsUntilShutdown)
    task = asyncio.create_task(runner.run())
    await wait_until(
        lambda: runner._successful_starts == 1,
        within=5,
        reason="the service runner records its first successful start",
    )
    runner._shutdown_event.set()
    await asyncio.wait_for(task, timeout=15.0)
    assert runner.service is not None
    assert runner.service.stopped is True


#: How soon a running service is stopped after a stop is asked for. It sits between the two
#: measured figures: a runner that polled once a second stopped about 1.0 s after the request, one
#: woken by it in about 0.06 s. Load cannot push the woken path past it, and the polling path,
#: asked to stop as it starts its wait, cannot duck under it.
STOPPED_WITHIN = 0.5


async def test_a_running_service_is_stopped_as_soon_as_a_stop_is_asked():
    """The runner's steady-state wait ends on the shutdown event, not on its next tick. The event
    is set as soon as the start is seen, when the runner has just begun that wait."""
    runner = ServiceRunner(RunsUntilShutdown)
    task = asyncio.create_task(runner.run())
    await wait_until(
        lambda: runner._successful_starts == 1,
        within=5,
        reason="the service runner records its first successful start",
    )

    asked = time.monotonic()
    runner._shutdown_event.set()
    await asyncio.wait_for(task, timeout=5.0)
    stopped_after = time.monotonic() - asked

    assert stopped_after < STOPPED_WITHIN, f"stopped {stopped_after:.2f} s after it was asked"
    assert runner.service is not None
    assert runner.service.stopped is True


class SetsTheStopAsItsSteadyStateBegins:
    """Sets the runner's shutdown event two callbacks after its start returns: after the start
    task's completion is queued and before the runner resumes, so the runner enters its steady
    state with `_running` still set and the event already set."""

    event: asyncio.Event | None = None

    def __init__(self):
        self.config = ServiceConfig(name="stop_as_steady_state_begins", health_port=0)
        self.stopped = False

    async def start(self):
        loop = asyncio.get_running_loop()
        loop.call_soon(loop.call_soon, type(self).event.set)

    async def stop(self):
        self.stopped = True


def test_a_stop_set_as_the_steady_state_begins_still_ends_the_run():
    """A set event's `wait()` returns without yielding, so a steady-state loop that waited on it
    while `_running` stayed set would spin and starve the monitor that clears `_running`; the loop
    ends on the event itself. The runner runs on its own loop in a thread, so such a spin fails
    this test when the join times out instead of hanging the suite: no `wait_for` on the spinning
    loop could fire."""
    runner = ServiceRunner(SetsTheStopAsItsSteadyStateBegins)
    SetsTheStopAsItsSteadyStateBegins.event = runner._shutdown_event
    ran = threading.Thread(target=asyncio.run, args=(runner.run(),), daemon=True)

    ran.start()
    ran.join(timeout=5.0)

    assert not ran.is_alive(), "the runner did not end after a stop set as its steady state began"
    assert runner.service is not None
    assert runner.service.stopped is True


async def test_CONTROL_a_closed_broker_is_still_seen_on_the_next_tick():
    """Woken by a stop, the wait still times out once a second to read the broker's state, so a
    broker that closes ends a service that is not restarted within about a second."""
    runner = ServiceRunner(StopsWhenTheBrokerCloses)
    started = time.monotonic()

    await asyncio.wait_for(runner.run(), timeout=5.0)
    ended_after = time.monotonic() - started

    assert ended_after < 3.0, f"ended {ended_after:.2f} s after it started"
    assert runner.service is not None
    assert runner.service.stopped is True


# --- the restart backoff ------------------------------------------------------
#
# Between crashes the runner waits `asyncio.wait_for(shutdown_event.wait(),
# timeout=backoff)`. These tests replace the `asyncio` the runner module sees
# with one whose `wait_for` records the timeout and returns at once, so the
# delays the runner ASKS for are read without waiting them out. The exact
# delays each test records are what show the runner routes its wait through
# that name: a wait routed elsewhere records nothing and those tests fail. The
# last CONTROL pins the other half, that the loop really waits between crashes
# when no stand-in is installed.


class _RecordedWaits:
    """The `asyncio` the runner module sees, with `wait_for` recording its timeout.

    After `stop_after` waits it sets the runner's shutdown event and returns
    instead of timing out, which is the runner's own shutdown-during-backoff path.
    """

    def __init__(self, runner: ServiceRunner, stop_after: int):
        self.runner = runner
        self.stop_after = stop_after
        self.timeouts: list[float] = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        self.timeouts.append(timeout)
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        if len(self.timeouts) >= self.stop_after:
            self.runner._running = False
            self.runner._shutdown_event.set()
            return None
        raise TimeoutError


def _scripted(outcomes: list[str], restart_delay: float):
    """A service class whose successive start() calls follow *outcomes*.

    "crash" raises from start(). "run" starts, then reports its broker closed,
    which ends that run cleanly after the runner's one-second health tick.
    """
    script = list(outcomes)

    class Scripted:
        def __init__(self):
            self.config = ServiceConfig(name="scripted", health_port=0, restart_delay=restart_delay)
            self.broker_state = BrokerConnectionState.CONNECTED

        async def start(self):
            outcome = script.pop(0) if script else "crash"
            if outcome == "crash":
                raise RuntimeError("start fails")
            self.broker_state = BrokerConnectionState.CLOSED

        async def stop(self):
            pass

    return Scripted


async def _delays(monkeypatch, service_class, waits: int) -> list[float]:
    import cliffracer.runners.orchestrator as orchestrator

    runner = ServiceRunner(service_class)
    recorded = _RecordedWaits(runner, stop_after=waits)
    monkeypatch.setattr(orchestrator, "asyncio", recorded)
    runner._running = True
    await asyncio.wait_for(runner._run_service(), timeout=10.0)
    return recorded.timeouts


async def test_the_backoff_starts_at_restart_delay_and_doubles_per_crash(monkeypatch):
    assert await _delays(monkeypatch, _scripted([], restart_delay=0.5), waits=3) == [
        0.5,
        1.0,
        2.0,
    ]


async def test_the_backoff_returns_to_restart_delay_after_a_successful_start(monkeypatch):
    """Two crashes grow it; a start that succeeds resets it, so the next crash
    waits restart_delay again rather than the four seconds it had reached."""
    service = _scripted(["crash", "crash", "run", "crash"], restart_delay=1.0)

    assert await _delays(monkeypatch, service, waits=3) == [1.0, 2.0, 1.0]


async def test_the_backoff_is_capped_at_sixty_seconds(monkeypatch):
    assert await _delays(monkeypatch, _scripted([], restart_delay=40.0), waits=3) == [
        40.0,
        60,
        60,
    ]


async def test_the_backoff_starts_at_one_second_when_construction_itself_fails(monkeypatch):
    """No instance means no restart_delay to read."""

    class ConstructorRaises:
        def __init__(self):
            raise RuntimeError("cannot build")

    assert await _delays(monkeypatch, ConstructorRaises, waits=2) == [1.0, 2.0]


async def test_CONTROL_without_the_stand_in_the_runner_really_waits():
    """Between crashes the loop really waits, when no stand-in is installed.

    A crash with a long restart_delay leaves the runner at one attempt with the
    loop still running, until the shutdown the wait is listening for arrives.
    Whether the wait goes through the runner module's `asyncio` is shown by the
    exact delays the tests above record, not by this one.
    """
    service = _scripted([], restart_delay=30.0)
    runner = ServiceRunner(service)
    runner._running = True
    task = asyncio.create_task(runner._run_service())

    await wait_until(
        lambda: runner._start_attempts == 1,
        within=5,
        reason="the service runner records its first start attempt",
    )
    await asyncio.sleep(0.2)

    assert runner._start_attempts == 1
    assert not task.done()
    runner._running = False
    runner._shutdown_event.set()
    await asyncio.wait_for(task, timeout=5.0)

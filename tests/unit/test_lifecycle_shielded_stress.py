"""Empirical adversarial stress test suite for shielded abortive cleanup.

Specifically stress-tests shielded abortive cleanup under cancellation:
- Continuous rapid cancellation bombardment during slow abortive cleanup.
- Mutual exclusion of concurrent start() and stop() callers during cancelled abortive teardown.
- Non-masking of primary startup exceptions under simultaneous cleanup failures and cancellations.
- Cancellation bombardment during interleaved start and stop transitions.
- Rapid restart cycles alternating between cancelled abortive teardowns and clean startups.
- Immediate re-start contention following external cancellation of an in-flight startup.
"""

import asyncio
import collections
import random
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit


class InstrumentedLifecycleService(ServicePhases, CliffracerService):
    """Service instrumented to record exact counts of lifecycle events."""

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.connect_count = 0
        self.disconnect_count = 0
        self.raw_disconnect_count = 0
        self._is_connected = False
        self.on_startup_count = 0
        self.on_shutdown_count = 0
        self.setup_extensions_count = 0
        self.stop_extensions_count = 0
        self.start_extensions_count = 0
        self.setup_subscriptions_count = 0
        self.stop_timers_count = 0
        # How many event-loop turns every hook gives up. 0 keeps a hook from yielding at all.
        self.ticks = 0

    async def _tick(self) -> None:
        for _ in range(self.ticks):
            await asyncio.sleep(0)

    @property
    def _lock(self) -> asyncio.Lock:
        return self.container.lifecycle.lock

    @property
    def _stopped(self) -> bool:
        return self.container.lifecycle.is_stopped

    @property
    def _starting(self) -> bool:
        return self.container.lifecycle.is_starting

    @property
    def _startup_succeeded(self) -> bool:
        return self.container.lifecycle._startup_succeeded

    @property
    def _start_task(self) -> Any:
        return self.container.lifecycle._start_task

    async def _setup_extensions(self) -> None:
        self.setup_extensions_count += 1
        await self._tick()
        await self.container._setup_extensions()

    async def connect(self) -> None:
        await self._tick()
        self.connect_count += 1
        self._is_connected = True

    async def disconnect(self) -> None:
        await self._tick()
        self.raw_disconnect_count += 1
        if self._is_connected:
            self._is_connected = False
            self.disconnect_count += 1

    async def on_startup(self) -> None:
        await self._tick()
        self.on_startup_count += 1

    async def on_shutdown(self) -> None:
        self.on_shutdown_count += 1

    async def _start_extensions(self) -> None:
        self.start_extensions_count += 1

    async def _stop_extensions(self) -> None:
        self.stop_extensions_count += 1

    async def _setup_subscriptions(self) -> None:
        await self._tick()
        self.setup_subscriptions_count += 1

    async def _stop_timers(self) -> None:
        self.stop_timers_count += 1


def assert_lifecycle_state(
    svc: CliffracerService,
    *,
    running: bool | None = None,
    stopped: bool | None = None,
    starting: bool | None = None,
    startup_succeeded: bool | None = None,
) -> None:
    if running is not None:
        assert svc.container.lifecycle.is_running == running
    if stopped is not None:
        assert svc.container.lifecycle.is_stopped == stopped
    if starting is not None:
        assert svc.container.lifecycle.is_starting == starting
    if startup_succeeded is not None:
        assert svc.container.lifecycle._startup_succeeded == startup_succeeded


@pytest.mark.asyncio
async def test_continuous_rapid_cancellation_during_slow_abortive_cleanup():
    """Verify that hammering start() with 50 continuous cancellations during
    a slow abortive disconnect NEVER releases self._lock or exits prematurely.
    """
    cfg = ServiceConfig(name="continuous_cancel_svc", health_port=0)
    svc = CliffracerService(cfg)

    in_disconnect = asyncio.Event()
    allow_disconnect = asyncio.Event()
    disconnect_finished = asyncio.Event()

    async def slow_disconnect() -> None:
        in_disconnect.set()
        await allow_disconnect.wait()
        disconnect_finished.set()

    svc.connect = AsyncMock()  # type: ignore[method-assign]
    svc.disconnect = slow_disconnect  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Startup exploded"))  # type: ignore[method-assign]

    start_task = asyncio.create_task(svc.start())

    # Wait until on_startup fails and abortive teardown pauses in disconnect()
    await asyncio.wait_for(in_disconnect.wait(), timeout=2.0)

    # Invariant: self._lock must be held right now
    assert svc.container.lifecycle.lock.locked(), "Lock must be held while disconnect is in flight"

    # Bombard start_task with 50 cancellations over 100ms
    premature_exit_detected = False
    lock_released_early = False

    for _ in range(50):
        start_task.cancel()
        await asyncio.sleep(0.002)
        if start_task.done():
            premature_exit_detected = True
            break
        if not svc.container.lifecycle.lock.locked():
            lock_released_early = True
            break

    assert not premature_exit_detected, (
        "start() task exited prematurely during continuous cancellation bombardment!"
    )
    assert not lock_released_early, (
        "self._lock was released while disconnect() was still executing!"
    )

    # Now allow disconnect to complete
    allow_disconnect.set()
    await asyncio.wait_for(disconnect_finished.wait(), timeout=2.0)

    # Await start_task — it should now complete and re-raise RuntimeError
    with pytest.raises(RuntimeError, match="Startup exploded"):
        await start_task

    # Lock must be released and state cleanly settled
    assert not svc.container.lifecycle.lock.locked(), (
        "Lock must be released after start() terminates"
    )
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)


@pytest.mark.asyncio
async def test_lock_contention_blocked_during_slow_abortive_cleanup_under_cancellation():
    """Verify that concurrent start() and stop() callers cannot acquire self._lock
    or enter critical sections while a cancelled abortive teardown is in flight.
    """
    cfg = ServiceConfig(name="lock_contention_svc", health_port=0)
    svc = CliffracerService(cfg)

    in_disconnect = asyncio.Event()
    allow_disconnect = asyncio.Event()
    disconnect_entries = 0

    async def slow_disconnect() -> None:
        nonlocal disconnect_entries
        disconnect_entries += 1
        in_disconnect.set()
        await allow_disconnect.wait()

    svc.connect = AsyncMock()  # type: ignore[method-assign]
    svc.disconnect = slow_disconnect  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Primary failure"))  # type: ignore[method-assign]

    primary_start_task = asyncio.create_task(svc.start())
    await asyncio.wait_for(in_disconnect.wait(), timeout=2.0)

    # While primary_start_task is stuck in disconnect and being cancelled:
    canceller_active = True

    async def continuous_canceller():
        while canceller_active:
            primary_start_task.cancel()
            await asyncio.sleep(0.005)

    canceller_task = asyncio.create_task(continuous_canceller())

    # Spawn 10 concurrent start() and 10 concurrent stop() calls
    async def competing_start(idx: int):
        try:
            await svc.start()
        except Exception:
            pass

    async def competing_stop(idx: int):
        try:
            await svc.stop()
        except Exception:
            pass

    competitors = [asyncio.create_task(competing_start(i)) for i in range(10)] + [
        asyncio.create_task(competing_stop(i)) for i in range(10)
    ]

    # Yield several loop iterations
    await asyncio.sleep(0.05)

    # Invariant: no competitor got past the lock. "Not done" is not enough: a competitor
    # that walked straight in would also block, in this test's own slow_disconnect. So
    # count the entries into the critical section: the primary's one disconnect, and the
    # one connect it made, are all that may have happened.
    assert disconnect_entries == 1, (
        f"{disconnect_entries} callers entered disconnect(); only the primary may, "
        "while it holds the lock"
    )
    assert svc.connect.await_count == 1, (
        f"{svc.connect.await_count} callers entered connect(); only the primary may"
    )
    for comp in competitors:
        assert not comp.done(), "Competitor finished while primary disconnect was blocked!"

    # Release disconnect and stop canceller
    canceller_active = False
    await canceller_task
    allow_disconnect.set()

    # Primary task should complete with RuntimeError
    with pytest.raises(RuntimeError, match="Primary failure"):
        await primary_start_task

    # All competitors should now drain cleanly
    await asyncio.gather(*competitors, return_exceptions=True)

    # Final state invariants:
    assert not svc.container.lifecycle.lock.locked()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)


@pytest.mark.asyncio
async def test_abortive_cleanup_exception_masking_under_continuous_cancellation():
    """Verify that exceptions in intermediate cleanup stages (timers, extensions, disconnect)
    do NOT mask the root startup exception, even under continuous cancellation.
    """
    cfg = ServiceConfig(name="masking_test_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    in_disconnect = asyncio.Event()
    allow_disconnect = asyncio.Event()

    async def faulty_timers():
        raise ValueError("Timer failure")

    async def faulty_extensions():
        raise KeyError("Extension failure")

    async def slow_faulty_disconnect():
        in_disconnect.set()
        await allow_disconnect.wait()
        raise OSError("Disconnect failure")

    svc._stop_timers = faulty_timers  # type: ignore[method-assign]
    svc._stop_extensions = faulty_extensions  # type: ignore[method-assign]
    svc.disconnect = slow_faulty_disconnect  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Primary root failure"))  # type: ignore[method-assign]

    start_task = asyncio.create_task(svc.start())
    await asyncio.wait_for(in_disconnect.wait(), timeout=2.0)

    # Cancel repeatedly while awaiting disconnect
    for _ in range(10):
        start_task.cancel()
        await asyncio.sleep(0.002)

    allow_disconnect.set()

    # Invariant: Must re-raise the PRIMARY exception (RuntimeError "Primary root failure"),
    # not the ValueError, KeyError, OSError, or CancelledError!
    with pytest.raises(RuntimeError, match="Primary root failure"):
        await start_task

    assert_lifecycle_state(svc, running=False, stopped=False, starting=False)
    # Verify defensive stop retry brings service to stopped state:
    with pytest.raises(ValueError, match="Timer failure"):
        await svc.stop()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
    assert not svc.container.lifecycle.lock.locked()


BOMBARDMENT_SEED = 20261001
BOMBARDMENT_STEPS = 300
BOMBARDMENT_TICKS = 3


@pytest.mark.asyncio
async def test_cancellation_bombardment_during_interleaved_start_stop():
    """Random start, stop and cancel calls land on a service whose every hook takes several turns.

    Each step of the driver is one event-loop turn that starts a task, stops one or cancels a
    live one, so the calls overlap: a start arrives while a stop is in flight, a stop arrives in
    the middle of a startup, and a cancel arrives in the middle of either. Nothing is timed, so
    one seed is one interleaving, and the test reads that it reached them rather than that
    nothing went wrong: refused starts, cancelled calls and startups that ran again after a stop
    all have to appear in the outcomes. Then the lock, the stop requests and the state are read
    BEFORE the test's own stop(), and the connects are matched against the disconnects.

    What it cannot reach: a stop cancels the startup it interrupts, and the cancel lands at the
    hook's next turn, so the startup never gets as far as asking whether it was interrupted.
    That question is answered only by a startup that does not yield to the cancel, or a stop from
    inside the startup, and the test below and
    `test_start_says_so_when_the_service_was_stopped_while_starting.py` read those.
    """
    cfg = ServiceConfig(name="interleaved_bombardment_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)
    svc.ticks = BOMBARDMENT_TICKS
    rng = random.Random(BOMBARDMENT_SEED)

    await svc.start()
    assert svc.setup_subscriptions_count == 1, "the clean first start did not finish its startup"

    tasks: list[asyncio.Task[None]] = []
    for _ in range(BOMBARDMENT_STEPS):
        live = [t for t in tasks if not t.done()]
        roll = rng.random()
        if roll < 0.3:
            tasks.append(asyncio.create_task(svc.start()))
        elif roll < 0.5:
            tasks.append(asyncio.create_task(svc.stop()))
        elif roll < 0.6 and live:
            rng.choice(live).cancel()
        await asyncio.sleep(0)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    outcomes = collections.Counter(type(res).__name__ for res in results)
    where = f"(seed {BOMBARDMENT_SEED}, outcomes {dict(outcomes)}, startups {svc.on_startup_count})"

    assert set(outcomes) <= {"NoneType", "CancelledError", "ServiceLifecycleError"}, where
    # The interleavings the name promises. A bombardment that never refused a start, never
    # cancelled a call, or never started the service again after a stop has tested a quieter
    # service than it claims.
    assert outcomes["ServiceLifecycleError"] > 0, f"no start was refused during a stop {where}"
    assert outcomes["CancelledError"] > 0, f"no call was cancelled {where}"
    assert svc.setup_subscriptions_count > 1, f"no startup completed after a stop {where}"

    # What the bombardment left, before the test's own stop() tidies it
    lifecycle = svc.container.lifecycle
    assert lifecycle._stop_requests == 0, f"a stop request was never released {where}"
    assert not lifecycle.lock.locked(), where
    assert not lifecycle.is_starting, where

    await svc.stop()

    assert not svc.container.lifecycle.lock.locked()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
    assert svc.connect_count >= 1, where
    assert svc.connect_count == svc.disconnect_count, where


class AbsorbsTheCancelOfItsSetup(InstrumentedLifecycleService):
    """A startup whose first stage takes a cancel and carries on, as a stage that catches it would."""

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.in_setup = asyncio.Event()
        self.cancels_absorbed = 0

    async def _setup_extensions(self) -> None:
        self.setup_extensions_count += 1
        self.in_setup.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancels_absorbed += 1


@pytest.mark.asyncio
async def test_a_stop_ends_a_startup_whose_stage_absorbed_the_cancel():
    """A stop that arrives during a startup cancels it. A stage that takes the cancel and returns
    leaves the startup to see for itself that a stop was asked for: it ends there, and no later
    stage runs for a service that is being stopped."""
    svc = AbsorbsTheCancelOfItsSetup(ServiceConfig(name="absorbed_cancel_svc", health_port=0))

    start_task = asyncio.create_task(svc.start())
    await asyncio.wait_for(svc.in_setup.wait(), timeout=2.0)
    await asyncio.wait_for(svc.stop(), timeout=2.0)
    await asyncio.wait_for(start_task, timeout=2.0)

    where = f"(connects {svc.connect_count}, subscriptions {svc.setup_subscriptions_count})"
    assert svc.cancels_absorbed == 1, "the stop's cancel never reached the stage"
    assert svc.connect_count == 0, f"the startup went on to connect after the stop {where}"
    assert svc.setup_subscriptions_count == 0, where
    assert svc.on_startup_count == 0, where
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
    assert svc.container.lifecycle._stop_requests == 0
    assert not svc.container.lifecycle.lock.locked()


@pytest.mark.asyncio
async def test_rapid_restart_loop_after_cancelled_abortive_cleanup():
    """Perform 25 rapid cycles alternating between a cancelled abortive startup
    and an immediate clean startup. Each cycle must add exactly the lifecycle events of one
    abortive and one clean run, and leave no task or subscription behind.
    """
    cfg = ServiceConfig(name="restart_loop_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    def counts() -> dict[str, int]:
        return {
            name: getattr(svc, name)
            for name in (
                "connect_count",
                "disconnect_count",
                "raw_disconnect_count",
                "on_startup_count",
                "on_shutdown_count",
                "setup_extensions_count",
                "start_extensions_count",
                "stop_extensions_count",
                "setup_subscriptions_count",
                "stop_timers_count",
            )
        }

    # What ONE cycle adds, and so what every cycle must: two starts (each connects and sets up its
    # extensions), one disconnect that is counted (the abortive one is the paused stand-in), one
    # subscription setup, extensions started and the user's shutdown hook run once (the clean run
    # only), and two teardowns (the abortive cleanup and the clean stop) each stopping extensions
    # and timers. A cycle that connected twice, leaked an extension or stopped timers more often
    # than it was started would differ, and so would a cycle that drifted from the one before it.
    per_cycle = {
        "connect_count": 2,
        "disconnect_count": 1,
        "raw_disconnect_count": 1,
        "on_startup_count": 0,  # replaced by a mock in both halves of the cycle
        "on_shutdown_count": 1,
        "setup_extensions_count": 2,
        "start_extensions_count": 1,
        "stop_extensions_count": 2,
        "setup_subscriptions_count": 1,
        "stop_timers_count": 2,
    }

    for cycle in range(25):
        before = counts()
        # 1. Abortive startup with immediate cancellation
        in_cleanup = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def pause_disconnect(_in_cleanup=in_cleanup, _allow_cleanup=allow_cleanup) -> None:
            _in_cleanup.set()
            await _allow_cleanup.wait()

        svc.disconnect = pause_disconnect  # type: ignore[method-assign]
        svc.on_startup = AsyncMock(side_effect=RuntimeError(f"Abort cycle {cycle}"))  # type: ignore[method-assign]

        start_task = asyncio.create_task(svc.start())
        await asyncio.wait_for(in_cleanup.wait(), timeout=2.0)

        # Cancel while in cleanup
        start_task.cancel()
        allow_cleanup.set()

        with pytest.raises(RuntimeError, match=f"Abort cycle {cycle}"):
            await start_task

        assert not svc.container.lifecycle.lock.locked()
        assert_lifecycle_state(svc, running=False, stopped=True, starting=False)

        # 2. Immediate clean startup
        clean_method = InstrumentedLifecycleService.disconnect.__get__(
            svc, InstrumentedLifecycleService
        )
        svc.disconnect = clean_method  # type: ignore[method-assign]
        svc.on_startup = AsyncMock(return_value=None)  # type: ignore[method-assign]

        await svc.start()
        assert_lifecycle_state(
            svc, running=True, stopped=False, starting=False, startup_succeeded=True
        )

        await svc.stop()
        assert_lifecycle_state(svc, running=False, stopped=True, starting=False)

        after = counts()
        delta = {name: after[name] - before[name] for name in after}
        assert delta == per_cycle, f"cycle {cycle}: {delta}"
        # Nothing carried over into the next cycle
        assert len(svc.container.lifecycle.active_tasks) == 0, f"cycle {cycle}"
        assert len(svc.container._subscriptions) == 0, f"cycle {cycle}"

    assert not svc.container.lifecycle.lock.locked()


@pytest.mark.asyncio
async def test_external_task_cancels_start_task_and_immediately_awaits_start():
    """Task A starts the service. Task B cancels Task A and immediately calls svc.start().

    Because Task A holds self._lock until its shielded teardown completes, Task B
    MUST NOT start until Task A has completely finished tearing down.
    """
    cfg = ServiceConfig(name="handover_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    in_disconnect = asyncio.Event()
    allow_disconnect = asyncio.Event()
    disconnect_finished = asyncio.Event()

    async def blocking_disconnect():
        in_disconnect.set()
        await allow_disconnect.wait()
        disconnect_finished.set()

    svc.disconnect = blocking_disconnect  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Task A failure"))  # type: ignore[method-assign]

    task_a = asyncio.create_task(svc.start())
    await asyncio.wait_for(in_disconnect.wait(), timeout=2.0)

    # Task B cancels Task A and calls start()
    task_a.cancel()

    # Reset disconnect for Task B's eventual clean run
    async def clean_disconnect():
        svc.disconnect_count += 1
        svc._is_connected = False

    # Patch on_startup for Task B to succeed
    svc.on_startup = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # While task_a is still tearing down, start() must block on the lifecycle lock.
    task_b = asyncio.create_task(svc.start())
    await asyncio.sleep(0.02)

    # Task B must still be waiting for the lock
    assert not task_b.done(), "Task B must not complete while Task A holds lock"
    assert not disconnect_finished.is_set()

    # Now release Task A's disconnect
    svc.disconnect = clean_disconnect  # type: ignore[method-assign]
    allow_disconnect.set()

    # Task A finishes with RuntimeError
    with pytest.raises(RuntimeError, match="Task A failure"):
        await task_a

    # Task B now acquires lock and completes clean startup
    await asyncio.wait_for(task_b, timeout=2.0)

    # Task A's teardown had finished by the time Task B's start() returned.
    assert disconnect_finished.is_set()
    assert_lifecycle_state(svc, running=True, stopped=False, starting=False, startup_succeeded=True)

    await svc.stop()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)

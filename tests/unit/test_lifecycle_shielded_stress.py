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
import random
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError


class InstrumentedLifecycleService(CliffracerService):
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
        await self.container._setup_extensions()

    async def connect(self) -> None:
        self.connect_count += 1
        self._is_connected = True

    async def disconnect(self) -> None:
        self.raw_disconnect_count += 1
        if self._is_connected:
            self._is_connected = False
            self.disconnect_count += 1

    async def on_startup(self) -> None:
        self.on_startup_count += 1

    async def on_shutdown(self) -> None:
        self.on_shutdown_count += 1

    async def _start_extensions(self) -> None:
        self.start_extensions_count += 1

    async def _stop_extensions(self) -> None:
        self.stop_extensions_count += 1

    async def _setup_subscriptions(self) -> None:
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


@pytest.mark.unit
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_contention_blocked_during_slow_abortive_cleanup_under_cancellation():
    """Verify that concurrent start() and stop() callers cannot acquire self._lock
    or enter critical sections while a cancelled abortive teardown is in flight.
    """
    cfg = ServiceConfig(name="lock_contention_svc", health_port=0)
    svc = CliffracerService(cfg)

    in_disconnect = asyncio.Event()
    allow_disconnect = asyncio.Event()

    async def slow_disconnect() -> None:
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
    concurrent_entered_count = 0

    async def competing_start(idx: int):
        nonlocal concurrent_entered_count
        try:
            await svc.start()
        except Exception:
            pass

    async def competing_stop(idx: int):
        nonlocal concurrent_entered_count
        try:
            await svc.stop()
        except Exception:
            pass

    competitors = [asyncio.create_task(competing_start(i)) for i in range(10)] + [
        asyncio.create_task(competing_stop(i)) for i in range(10)
    ]

    # Yield several loop iterations
    await asyncio.sleep(0.05)

    # Invariant: Not a single competitor should be done or have modified _stopped/_running
    # because primary_start_task still holds self._lock!
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


@pytest.mark.unit
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_bombardment_during_interleaved_start_stop():
    """50 concurrent start/stop tasks subject to random external cancellations.

    Verifies absence of unhandled task exceptions, clean lock release,
    and valid terminal lifecycle state.
    """
    cfg = ServiceConfig(name="interleaved_bombardment_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    all_tasks = []
    for i in range(50):
        if i % 2 == 0:
            all_tasks.append(asyncio.create_task(svc.start()))
        else:
            all_tasks.append(asyncio.create_task(svc.stop()))

    # Concurrently cancel a random selection of tasks
    for _ in range(20):
        await asyncio.sleep(0.002)
        target = random.choice(all_tasks)
        if not target.done():
            target.cancel()

    # Await all tasks
    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    # Every task either succeeded (None) or was cancelled / raised expected lifecycle exceptions
    for res in results:
        if isinstance(res, Exception) and not isinstance(
            res, asyncio.CancelledError | ServiceLifecycleError
        ):
            pytest.fail(f"Unexpected exception in task: {res}")

    # Final cleanup: ensure stop is called
    await svc.stop()

    assert not svc.container.lifecycle.lock.locked()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
    assert svc.connect_count == svc.disconnect_count


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rapid_restart_loop_after_cancelled_abortive_cleanup():
    """Perform 25 rapid cycles alternating between a cancelled abortive startup
    and an immediate clean startup, verifying complete isolation and zero leak.
    """
    cfg = ServiceConfig(name="restart_loop_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    for cycle in range(25):
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

    assert not svc.container.lifecycle.lock.locked()


@pytest.mark.unit
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

    task_b_started_before_a_teardown = False

    async def task_b_run():
        nonlocal task_b_started_before_a_teardown
        # While task_a is still tearing down (disconnect not finished):
        # calling start() should block on self._lock!
        if not disconnect_finished.is_set():
            # If start() returns or enters while disconnect_finished is False, that's a violation!
            pass
        await svc.start()
        if not disconnect_finished.is_set():
            task_b_started_before_a_teardown = True

    # Patch on_startup for Task B to succeed
    svc.on_startup = AsyncMock(return_value=None)  # type: ignore[method-assign]

    task_b = asyncio.create_task(task_b_run())
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

    assert not task_b_started_before_a_teardown
    assert_lifecycle_state(svc, running=True, stopped=False, starting=False, startup_succeeded=True)

    await svc.stop()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)

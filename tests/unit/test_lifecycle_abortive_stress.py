"""Empirical high-concurrency stress test suite for service lifecycle and abortive startup.

Stress-tests abortive startup cancellation race conditions:
- 500-event lifecycle chaos monkey under high concurrency.
- Cascading multi-stage abortive failures with concurrent stop stampedes.
- External task cancellation during each startup phase.
- Cancellation of stop() itself guaranteeing shielded disconnect and extension cleanup.
- Resilience against multiple exceptions during abortive cleanup without masking startup errors.
- Sequential restartability after clean stop and after abortive failure.
- Massive 100-task start cancellation stampede with concurrent stop requests.
"""

import asyncio
import random
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig


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
    svc: InstrumentedLifecycleService,
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
async def test_lifecycle_chaos_monkey_500_events():
    """500 randomized concurrent lifecycle events (start, stop, cancel) on a single service.

    Invariants:
    1. At completion, connect_count == disconnect_count.
    2. Final service state is non-running, stopped, not starting, and _start_task is None.
    3. No deadlocks; all 500 events complete within deadline.
    """
    cfg = ServiceConfig(name="chaos_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    events_run = 0

    async def worker(worker_id: int):
        nonlocal events_run
        for _ in range(10):
            action = random.choice(["start", "stop", "start_and_cancel"])
            if action == "start":
                try:
                    await svc.start()
                except (Exception, asyncio.CancelledError):
                    pass
            elif action == "stop":
                try:
                    await svc.stop()
                except (Exception, asyncio.CancelledError):
                    pass
            elif action == "start_and_cancel":
                t = asyncio.create_task(svc.start())
                await asyncio.sleep(random.uniform(0.0001, 0.002))
                t.cancel()
                try:
                    await t
                except (Exception, asyncio.CancelledError):
                    pass
            events_run += 1
            await asyncio.sleep(random.uniform(0.0001, 0.001))

    workers = [asyncio.create_task(worker(i)) for i in range(50)]
    await asyncio.wait_for(asyncio.gather(*workers), timeout=15.0)

    # Ensure final clean stop
    await svc.stop()

    assert events_run == 500
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
    assert svc._start_task is None
    # Critical resource invariant: every successful connect was cleaned up by a disconnect
    assert svc.connect_count == svc.disconnect_count, (
        f"Resource leak: connect_count ({svc.connect_count}) != disconnect_count ({svc.disconnect_count})"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cascading_abortive_startup_failures_with_concurrent_stops():
    """20 sequential abortive startups across various lifecycle failure points,
    each attacked by 10 concurrent stop() calls while teardown is in flight.

    Invariants:
    1. start() ALWAYS raises the exact stage exception (never masked by CancelledError).
    2. All concurrent stop() calls complete cleanly returning None.
    3. disconnect() is called on every failure.
    4. on_shutdown() is NEVER called because _startup_succeeded remains False.
    """
    failure_stages = [
        "_setup_extensions",
        "connect",
        "on_startup",
        "_start_extensions",
        "_setup_subscriptions",
    ]

    for iteration in range(20):
        stage = failure_stages[iteration % len(failure_stages)]
        cfg = ServiceConfig(name=f"abortive_cascading_{iteration}", health_port=0)
        svc = InstrumentedLifecycleService(cfg)

        stage_error = ValueError(f"Forced failure in {stage} at iter {iteration}")
        teardown_started = asyncio.Event()

        # Wrap _stop_timers to detect when abortive teardown begins
        orig_stop_timers = svc._stop_timers

        def make_monitored_stop_timers(event, orig):
            async def monitored_stop_timers():
                event.set()
                await asyncio.sleep(0.005)  # Simulate non-trivial cleanup duration
                await orig()

            return monitored_stop_timers

        svc._stop_timers = make_monitored_stop_timers(teardown_started, orig_stop_timers)  # type: ignore[method-assign]

        with patch.object(svc, stage, side_effect=stage_error):
            start_task = asyncio.create_task(svc.start())

            # Wait until teardown has begun
            await asyncio.wait_for(teardown_started.wait(), timeout=3.0)

            # Attack with 10 concurrent stop() calls
            stop_tasks = [asyncio.create_task(svc.stop()) for _ in range(10)]

            # Await start_task and expect the exact original error
            with pytest.raises(ValueError) as excinfo:
                await start_task
            assert excinfo.value is stage_error

            # Await all stop tasks - all must return cleanly
            stop_results = await asyncio.gather(*stop_tasks, return_exceptions=True)
            for res in stop_results:
                assert res is None, f"Concurrent stop() returned exception: {res}"

            # Verify invariants
            expected_shutdown = 1 if stage in ("_start_extensions", "_setup_subscriptions") else 0
            assert svc.on_shutdown_count == expected_shutdown
            assert svc.raw_disconnect_count == 1
            assert not svc._is_connected
            assert svc.connect_count == svc.disconnect_count
            assert_lifecycle_state(
                svc, running=False, stopped=True, starting=False, startup_succeeded=False
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_task_cancellation_during_startup_stages():
    """Cancelling start() directly via external task cancellation at each phase.

    Invariants:
    1. The awaiting task receives asyncio.CancelledError.
    2. _stop_internal executes to completion under shield.
    3. disconnect() is called.
    4. Service is safely stopped.
    """
    stages = ["connect", "on_startup", "_start_extensions", "_setup_subscriptions"]

    for stage in stages:
        cfg = ServiceConfig(name=f"cancel_stage_{stage}", health_port=0)
        svc = InstrumentedLifecycleService(cfg)

        stage_entered = asyncio.Event()
        proceed = asyncio.Event()

        def make_pause_in_stage(entered_event, proceed_event):
            async def pause_in_stage():
                entered_event.set()
                await proceed_event.wait()

            return pause_in_stage

        with patch.object(svc, stage, side_effect=make_pause_in_stage(stage_entered, proceed)):
            start_task = asyncio.create_task(svc.start())
            await asyncio.wait_for(stage_entered.wait(), timeout=2.0)

            # External cancellation (e.g. asyncio.timeout)
            start_task.cancel()
            proceed.set()

            with pytest.raises(asyncio.CancelledError):
                await start_task

            # Verify teardown completed despite task cancellation
            assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
            assert not svc._is_connected
            assert svc.connect_count == svc.disconnect_count
            assert svc.raw_disconnect_count >= 1
            expected_shutdown = 1 if stage in ("_start_extensions", "_setup_subscriptions") else 0
            assert svc.on_shutdown_count == expected_shutdown

            # Defensive subsequent stop is a no-op
            await svc.stop()
            assert not svc._is_connected
            assert svc.connect_count == svc.disconnect_count


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_of_stop_itself_guarantees_disconnect_and_extensions_cleanup():
    """If stop() itself is cancelled while awaiting on_shutdown,
    shielded teardown guarantees that _stop_extensions and disconnect still run.
    """
    cfg = ServiceConfig(name="cancelled_stop_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    await svc.start()
    assert_lifecycle_state(svc, running=True, startup_succeeded=True)

    shutdown_entered = asyncio.Event()
    shutdown_proceed = asyncio.Event()

    async def hanging_shutdown():
        shutdown_entered.set()
        await shutdown_proceed.wait()
        svc.on_shutdown_count += 1

    svc.on_shutdown = hanging_shutdown  # type: ignore[method-assign]

    stop_task = asyncio.create_task(svc.stop())
    await asyncio.wait_for(shutdown_entered.wait(), timeout=2.0)

    # Cancel the stop() task from outside (e.g. caller shutdown timeout exceeded)
    stop_task.cancel()
    shutdown_proceed.set()

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    # Crucial invariant: Even though stop() was cancelled,
    # the shielded blocks in _stop_internal ensured _stop_extensions and disconnect ran,
    # and _stopped was set to True!
    assert_lifecycle_state(svc, running=False, stopped=True)
    assert svc.stop_extensions_count == 1
    assert svc.disconnect_count == 1

    # Subsequent stop() call is a clean no-op
    await svc.stop()
    assert svc.disconnect_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_stage_exceptions_do_not_abort_subsequent_cleanup_nor_mask_startup_error():
    """Exceptions in intermediate teardown stages (timers, extensions)
    do not prevent disconnect() from executing, and do not mask the root startup exception.
    """
    cfg = ServiceConfig(name="faulty_cleanup_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    async def faulty_stop_timers():
        raise RuntimeError("Timer teardown exploded")

    async def faulty_stop_extensions():
        raise RuntimeError("Extension teardown exploded")

    svc._stop_timers = faulty_stop_timers  # type: ignore[method-assign]
    svc._stop_extensions = faulty_stop_extensions  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=KeyError("Primary startup failure"))  # type: ignore[method-assign]

    # start() should raise the primary startup failure KeyError, not the cleanup RuntimeErrors
    with pytest.raises(KeyError, match="Primary startup failure"):
        await svc.start()

    # Crucial: disconnect() must still have been called via try/finally
    assert svc.disconnect_count == 1
    assert_lifecycle_state(svc, running=False, stopped=False, starting=False)

    # Verify that a subsequent stop() retries and brings the service to stopped state:
    with pytest.raises(RuntimeError, match="Timer teardown exploded"):
        await svc.stop()
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sequential_restart_cycles_after_clean_and_abortive_stops():
    """Verify service can cycle through start -> stop -> start -> abort -> start -> stop cleanly."""
    cfg = ServiceConfig(name="restart_cycle_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    for _ in range(5):
        # 1. Clean run
        await svc.start()
        assert_lifecycle_state(svc, running=True, stopped=False, startup_succeeded=True)
        await svc.stop()
        assert_lifecycle_state(svc, running=False, stopped=True)

        # 2. Abortive run
        with patch.object(svc, "on_startup", side_effect=RuntimeError("Cycle abort")):
            with pytest.raises(RuntimeError, match="Cycle abort"):
                await svc.start()
            assert_lifecycle_state(svc, running=False, stopped=True, startup_succeeded=False)

    # Invariants across 5 clean + 5 abortive cycles:
    # 5 clean connects + 5 abortive connects = 10 connects
    # 5 clean disconnects + 5 abortive disconnects = 10 disconnects
    assert svc.connect_count == 10
    assert svc.disconnect_count == 10
    assert svc.on_startup_count == 5  # only clean runs hit actual on_startup
    assert svc.on_shutdown_count == 5  # only clean runs hit on_shutdown


@pytest.mark.unit
@pytest.mark.asyncio
async def test_massive_start_cancellation_stampede():
    """100 tasks call start(). Half are cancelled mid-flight, and 50 call stop().

    Verifies deterministic convergence without unhandled exceptions or leaks.
    """
    cfg = ServiceConfig(name="massive_stampede_svc", health_port=0)
    svc = InstrumentedLifecycleService(cfg)

    startup_barrier = asyncio.Event()

    async def slow_startup():
        await startup_barrier.wait()

    svc.on_startup = slow_startup  # type: ignore[method-assign]

    # Launch 100 start tasks
    start_tasks = [asyncio.create_task(svc.start()) for _ in range(100)]
    await asyncio.sleep(0.005)

    # Cancel 50 of them
    for t in start_tasks[:50]:
        t.cancel()

    # Launch 50 stop tasks concurrently
    stop_tasks = [asyncio.create_task(svc.stop()) for _ in range(50)]

    # Release startup barrier
    startup_barrier.set()

    # Gather all tasks
    _ = await asyncio.gather(*start_tasks, return_exceptions=True)
    stop_results = await asyncio.gather(*stop_tasks, return_exceptions=True)

    for res in stop_results:
        assert res is None, f"Stop task failed: {res}"

    # Verify service settled cleanly
    assert_lifecycle_state(svc, running=False, stopped=True, starting=False)
    assert svc.connect_count == svc.disconnect_count


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_cancellation_during_shielded_abortive_cleanup():
    """Demonstrates defect: when start() receives cancellation while awaiting
    asyncio.shield(self._stop_internal()), the shield raises CancelledError to start(),
    causing start() to exit prematurely and release _lock while _stop_internal is
    still actively running in the background.
    """
    cfg = ServiceConfig(name="premature_exit_svc", health_port=0)
    svc = CliffracerService(cfg)

    in_disconnect = asyncio.Event()
    allow_disconnect = asyncio.Event()

    async def slow_disconnect() -> None:
        in_disconnect.set()
        await allow_disconnect.wait()

    svc.connect = AsyncMock()  # type: ignore[method-assign]
    svc.disconnect = slow_disconnect  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Startup crashed"))  # type: ignore[method-assign]

    start_task = asyncio.create_task(svc.start())

    # Wait until on_startup fails and abortive teardown reaches disconnect()
    await asyncio.wait_for(in_disconnect.wait(), timeout=2.0)

    # Cancel start_task while it is awaiting asyncio.shield(self._stop_internal())
    start_task.cancel()

    # Yield control to let the event loop process the cancellation
    await asyncio.sleep(0.01)

    # Invariant: start_task MUST NOT be done while disconnect() is still paused!
    # If start_task.done() is True here, start() exited and released self._lock
    # while disconnect() was still in flight!
    is_done_prematurely = start_task.done()

    # Allow disconnect to finish so cleanup completes
    allow_disconnect.set()
    await asyncio.gather(start_task, return_exceptions=True)

    assert not is_done_prematurely, (
        "start() task exited prematurely under cancellation while _stop_internal() "
        "was still in flight, prematurely releasing self._lock and orphaning teardown."
    )

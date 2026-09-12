"""Empirical adversarial stress-test suite for service lifecycle and concurrency.

Tests concurrency, lifecycle, pull consumer CPU yield, BatchProcessor WeakSet safety,
and ResilientMethodProxy async coroutine dispatch.
"""

import asyncio
import collections.abc
import inspect
import random
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cliffracer_metrics.batch_processor import BatchProcessor
from cliffracer_resilience.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitBreakerConfig,
    ResilientMethodProxy,
    ResilientRpcProxy,
    RpcCircuitOpenError,
)

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.container import Container
from cliffracer.core.exceptions import ServiceLifecycleError

pytestmark = pytest.mark.unit

# ============================================================================
# Section 1: Concurrent start() and stop() calls under load & cancellation
# ============================================================================


class LifecycleInstrumentedService(CliffracerService):
    """Instrumentation helper tracking calls to lifecycle methods."""

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.setup_extensions_calls = 0
        self.connect_calls = 0
        self.on_startup_calls = 0
        self.start_extensions_calls = 0
        self.setup_subscriptions_calls = 0
        self.on_shutdown_calls = 0
        self.disconnect_calls = 0
        self.stop_extensions_calls = 0
        self.stop_timers_calls = 0

    async def _setup_extensions(self) -> None:
        self.setup_extensions_calls += 1
        await self.container._setup_extensions()

    async def connect(self) -> None:
        self.connect_calls += 1

    async def on_startup(self) -> None:
        self.on_startup_calls += 1

    async def _start_extensions(self) -> None:
        self.start_extensions_calls += 1

    async def _setup_subscriptions(self) -> None:
        self.setup_subscriptions_calls += 1

    async def on_shutdown(self) -> None:
        self.on_shutdown_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def _stop_extensions(self) -> None:
        self.stop_extensions_calls += 1

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

    async def _stop_timers(self) -> None:
        self.stop_timers_calls += 1


@pytest.mark.asyncio
async def test_concurrent_start_stampede():
    """Stress test: 100 concurrent tasks calling start() on the same service.

    Verifies that initialization executes exactly once and all callers complete
    without error, leaving the service in running state.
    """
    cfg = ServiceConfig(name="start_stampede_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)

    # Launch 100 concurrent start calls
    tasks = [asyncio.create_task(svc.start()) for _ in range(100)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Verify none raised exceptions
    for res in results:
        assert not isinstance(res, Exception), f"Unexpected exception in start(): {res}"

    # Critical invariants: initialization hooks executed exactly once
    assert svc.connect_calls == 1
    assert svc.on_startup_calls == 1
    assert svc.setup_subscriptions_calls == 1
    assert svc._running is True
    assert svc._starting is False
    assert svc._startup_succeeded is True

    # Clean shutdown
    await svc.stop()
    assert svc._running is False
    assert svc._stopped is True  # type: ignore[unreachable]
    assert svc.disconnect_calls == 1
    assert svc.on_shutdown_calls == 1


@pytest.mark.asyncio
async def test_concurrent_stop_stampede():
    """Stress test: 100 concurrent tasks calling stop() on a running service.

    Verifies that teardown executes cleanly and idempotently with exactly one
    disconnection and on_shutdown invocation.
    """
    cfg = ServiceConfig(name="stop_stampede_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)
    await svc.start()

    assert svc._running is True
    assert svc.on_startup_calls == 1

    # Launch 100 concurrent stop calls
    tasks = [asyncio.create_task(svc.stop()) for _ in range(100)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        assert not isinstance(res, Exception), f"Unexpected exception in stop(): {res}"

    # Critical invariants: teardown hooks executed exactly once
    assert svc._running is False
    assert svc._stopped is True  # type: ignore[unreachable]
    assert svc.on_shutdown_calls == 1
    assert svc.disconnect_calls == 1


@pytest.mark.asyncio
async def test_rapid_cancellation_during_startup_stages():
    """Stress test: cancel start() at multiple distinct async suspension points.

    Verifies that resource cleanup occurs, on_shutdown is not called, and
    defensive stop() is idempotent.
    """
    stages = [
        "connect",
        "on_startup",
        "start_extensions",
        "setup_subscriptions",
    ]

    for target_stage in stages:
        cfg = ServiceConfig(name=f"cancel_at_{target_stage}", health_port=0)
        svc = LifecycleInstrumentedService(cfg)
        reached_stage = asyncio.Event()

        def make_stage_hook(evt: asyncio.Event):
            async def _hook():
                evt.set()
                await asyncio.sleep(10.0)  # Hang indefinitely until cancelled

            return _hook

        stage_fn = make_stage_hook(reached_stage)
        if target_stage == "connect":
            svc.connect = stage_fn  # type: ignore[method-assign]
        elif target_stage == "on_startup":
            svc.on_startup = stage_fn  # type: ignore[method-assign]
        elif target_stage == "start_extensions":
            svc._start_extensions = stage_fn  # type: ignore[method-assign]
        elif target_stage == "setup_subscriptions":
            svc._setup_subscriptions = stage_fn  # type: ignore[method-assign]

        start_task = asyncio.create_task(svc.start())
        # Wait until target stage is reached
        await asyncio.wait_for(reached_stage.wait(), timeout=2.0)

        # Trigger stop() which cancels in-flight start task and waits
        stop_task = asyncio.create_task(svc.stop())
        await asyncio.wait_for(stop_task, timeout=2.0)

        # start_task should have finished via CancelledError
        assert start_task.done()

        # Invariants
        assert svc._running is False
        assert svc._startup_succeeded is False
        expected_shutdown_calls = (
            1 if target_stage in ("start_extensions", "setup_subscriptions") else 0
        )
        assert svc.on_shutdown_calls == expected_shutdown_calls, (
            f"on_shutdown call mismatch after abortive startup at {target_stage}"
        )

        # Multiple defensive stop calls must remain safe no-ops
        for _ in range(5):
            await svc.stop()
        assert svc.on_shutdown_calls == expected_shutdown_calls


@pytest.mark.asyncio
async def test_interleaved_start_stop_high_concurrency_race():
    """Adversarial race test: 50 tasks randomly alternating start() and stop().

    Verifies no deadlock, no unhandled exceptions, and coherent terminal state.
    """
    cfg = ServiceConfig(name="race_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)

    async def worker(action: str):
        for _ in range(5):
            await asyncio.sleep(random.uniform(0.0001, 0.001))
            if action == "start":
                try:
                    await svc.start()
                except (asyncio.CancelledError, ServiceLifecycleError):
                    pass
            else:
                await svc.stop()

    tasks = []
    for i in range(50):
        action = "start" if i % 2 == 0 else "stop"
        tasks.append(asyncio.create_task(worker(action)))

    # Must complete cleanly within 5 seconds (prevent deadlocks)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    # State must be consistent: either running and not stopped, or stopped and not running
    is_running = svc._running
    is_stopped = svc._stopped
    if is_running:
        assert is_stopped is False
    else:
        assert is_stopped is True

    # Final cleanup must bring service to stopped state
    await svc.stop()
    assert svc._running is False
    assert svc._stopped is True


# ============================================================================
# Section 2: Abortive startup cleanup & defensive stop() idempotency
# ============================================================================


@pytest.mark.asyncio
async def test_abortive_startup_at_all_failure_points():
    """Verify that exceptions at any startup step abort cleanly without resource leaks."""

    class StepFailure(RuntimeError):
        pass

    failure_steps = [
        "connect",
        "on_startup",
        "start_extensions",
        "setup_subscriptions",
    ]

    for step in failure_steps:
        cfg = ServiceConfig(name=f"fail_{step}_svc", health_port=0)
        svc = LifecycleInstrumentedService(cfg)

        def make_fail_hook(step_name: str):
            async def _fail():
                raise StepFailure(f"Failure injected at {step_name}")

            return _fail

        fail_fn = make_fail_hook(step)
        if step == "connect":
            svc.connect = fail_fn  # type: ignore[method-assign]
        elif step == "on_startup":
            svc.on_startup = fail_fn  # type: ignore[method-assign]
        elif step == "start_extensions":
            svc._start_extensions = fail_fn  # type: ignore[method-assign]
        elif step == "setup_subscriptions":
            svc._setup_subscriptions = fail_fn  # type: ignore[method-assign]

        with pytest.raises(StepFailure, match=f"Failure injected at {step}"):
            await svc.start()

        # Invariants:
        assert svc._running is False
        assert svc._startup_succeeded is False
        assert svc._stopped is True
        expected_shutdown_calls = 1 if step in ("start_extensions", "setup_subscriptions") else 0
        assert svc.on_shutdown_calls == expected_shutdown_calls

        # Defensive stop in finally block
        await svc.stop()
        assert svc.on_shutdown_calls == expected_shutdown_calls
        assert svc._stopped is True


# ============================================================================
# Section 3: Container._pull_loop CPU yield and 0-message backoff
# ============================================================================


@pytest.mark.asyncio
async def test_pull_loop_cpu_yield_on_zero_messages():
    """Empirically measure CPU time and loop iterations when no messages exist.

    Confirms that Container._pull_loop yields control via sleep(0.05) and does
    not spin in a tight 100% CPU loop.
    """
    cfg = ServiceConfig(name="pull_cpu_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)
    svc._running = True

    container = Container(svc, cfg)

    pull_sub = AsyncMock()
    # Simulate broker returning 0 messages repeatedly via TimeoutError
    pull_sub.fetch = AsyncMock(side_effect=TimeoutError)
    pull_sub.unsubscribe = AsyncMock()

    t_wall_start = time.perf_counter()
    t_cpu_start = time.process_time()

    loop_task = asyncio.create_task(container._pull_loop(pull_sub, "durable_1"))

    # Let the loop run for 0.25 seconds of real wall-clock time
    await asyncio.sleep(0.25)
    svc._running = False
    await loop_task

    t_wall = time.perf_counter() - t_wall_start
    t_cpu = time.process_time() - t_cpu_start

    fetch_count = pull_sub.fetch.await_count

    # Iteration check: at 0.05s sleep per iteration, 0.25s must produce roughly 4-7 iterations
    # If it were a 100% CPU tight spin, fetch_count would be > 20,000
    assert 3 <= fetch_count <= 10, (
        f"Fetch count was {fetch_count} in {t_wall:.3f}s; expected between 3 and 10 iterations"
    )

    # CPU utilization check: CPU time should be tiny compared to wall-clock time
    cpu_utilization = t_cpu / t_wall if t_wall > 0 else 0
    assert cpu_utilization < 0.20, (
        f"CPU utilization {cpu_utilization * 100:.1f}% exceeded 20% limit "
        f"(CPU time: {t_cpu:.4f}s, wall: {t_wall:.4f}s)"
    )


@pytest.mark.asyncio
async def test_pull_loop_immediate_processing_when_messages_present():
    """When messages are returned, _pull_loop processes immediately without sleep(0.05)."""
    cfg = ServiceConfig(name="pull_msg_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)
    svc._running = True

    container = Container(svc, cfg)

    # Mock messages
    msg1, msg2 = AsyncMock(), AsyncMock()
    pull_sub = AsyncMock()

    # First fetch returns 2 messages; second fetch signals termination
    batches = [[msg1, msg2], []]

    async def mock_fetch(*args, **kwargs):
        if batches:
            res = batches.pop(0)
            if not res:
                svc._running = False
            return res
        svc._running = False
        return []

    pull_sub.fetch = AsyncMock(side_effect=mock_fetch)
    pull_sub.unsubscribe = AsyncMock()

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with patch.object(container, "_handle_jetstream_event", new_callable=AsyncMock):
            await container._pull_loop(pull_sub, "durable_work")

            # Sleep(0.05) should NOT have been called after the batch of 2 messages
            sleep_durations = [call.args[0] for call in mock_sleep.await_args_list]
            # Since the second batch returned [] (count=0), sleep(0.05) is called once
            assert sleep_durations == [0.05]


# ============================================================================
# Section 4: BatchProcessor.shutdown WeakSet concurrency safety
# ============================================================================


@pytest.mark.asyncio
async def test_batch_processor_shutdown_concurrent_task_mutation_stress():
    """Stress test: 500 tasks completing concurrently while shutdown() executes.

    Verifies list snapshot prevents 'RuntimeError: Set changed size during iteration'.
    """
    for _ in range(10):
        processor = BatchProcessor(batch_size=10, batch_timeout_ms=1000)

        # Spawn 100 tasks with randomized completion delays
        tasks = []
        for _ in range(100):

            async def mock_task():
                await asyncio.sleep(random.uniform(0.0001, 0.005))

            t = asyncio.create_task(mock_task())
            processor._batch_tasks.add(t)

            def make_done_cb(p: BatchProcessor):
                return lambda task: p._batch_tasks.discard(task)

            t.add_done_callback(make_done_cb(processor))
            tasks.append(t)

        # Concurrently initiate shutdown while tasks are finishing
        shutdown_task = asyncio.create_task(processor.shutdown())

        # Await shutdown without raising RuntimeError
        await asyncio.wait_for(shutdown_task, timeout=2.0)

        assert processor._shutdown is True
        assert len(processor._batches) == 0
        assert len(processor._batch_futures) == 0


@pytest.mark.asyncio
async def test_batch_processor_shutdown_with_faulty_batch_tasks():
    """Verify shutdown() succeeds even if tracked batch tasks raise exceptions."""
    processor = BatchProcessor(batch_size=5, batch_timeout_ms=1000)

    async def failing_task():
        await asyncio.sleep(0.001)
        raise ValueError("Simulated task failure")

    async def cancelled_task():
        await asyncio.sleep(0.001)
        raise asyncio.CancelledError()

    t1 = asyncio.create_task(failing_task())
    t2 = asyncio.create_task(cancelled_task())

    processor._batch_tasks.add(t1)
    processor._batch_tasks.add(t2)
    t1.add_done_callback(lambda t: processor._batch_tasks.discard(t))
    t2.add_done_callback(lambda t: processor._batch_tasks.discard(t))

    # shutdown should absorb exceptions via return_exceptions=True
    await asyncio.wait_for(processor.shutdown(), timeout=2.0)
    assert processor._shutdown is True


# ============================================================================
# Section 5: ResilientMethodProxy.call_async coroutine and dispatch semantics
# ============================================================================


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_coroutine_inspection():
    """Verify call_async returns an awaitable coroutine object when CLOSED."""

    class OrderService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = OrderService(ServiceConfig(name="order_svc"))
    dummy_coro = asyncio.sleep(0)  # genuine awaitable coroutine
    svc.call_rpc_no_wait = MagicMock(return_value=dummy_coro)  # type: ignore[method-assign]

    proxy = svc.inventory.update_stock
    assert proxy.circuit_breaker.state == CLOSED

    coro = proxy.call_async(sku="SKU-100", delta=-1)

    # Must return genuine awaitable coroutine, not None
    assert coro is not None
    assert inspect.iscoroutine(coro) or inspect.isawaitable(coro)
    assert isinstance(coro, collections.abc.Awaitable)

    # Await it and confirm it dispatches
    await coro
    svc.call_rpc_no_wait.assert_called_once_with(
        "inventory_service", "update_stock", namespace=None, sku="SKU-100", delta=-1
    )


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_open_circuit_fast_fail():
    """Verify call_async raises RpcCircuitOpenError fast when OPEN without coroutine leak."""

    class OrderService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = OrderService(ServiceConfig(name="order_svc"))
    svc.call_rpc_no_wait = MagicMock()  # type: ignore[method-assign]

    # Manually trip circuit breaker to OPEN
    svc.inventory.circuit_breaker.trip()
    assert svc.inventory.circuit_breaker.state == OPEN

    # 50 fast-fail calls under load
    for i in range(50):
        with pytest.raises(RpcCircuitOpenError) as exc_info:
            svc.inventory.update_stock.call_async(sku=f"SKU-{i}")
        assert "OPEN" in str(exc_info.value)

    # No RPCs dispatched
    svc.call_rpc_no_wait.assert_not_called()


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_circuit_transitions():
    """Verify call_async behavior across CLOSED -> OPEN -> HALF_OPEN states."""
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05)
    cb = CircuitBreaker("billing-service", config=config)

    class BillingClient(CliffracerService):
        pass

    svc = BillingClient(ServiceConfig(name="billing_client"))
    dispatched_calls = []

    async def mock_call_rpc_no_wait(service, method, namespace=None, **kwargs):
        dispatched_calls.append((service, method, kwargs))

    svc.call_rpc_no_wait = MagicMock(side_effect=mock_call_rpc_no_wait)  # type: ignore[method-assign]

    proxy = ResilientMethodProxy(
        service_instance=svc,
        service_name="billing_service",
        method_name="charge",
        circuit_breaker=cb,
    )

    # 1. State: CLOSED -> call_async works and dispatches
    assert cb.state == CLOSED
    coro1 = proxy.call_async(amount=100)
    await coro1
    assert len(dispatched_calls) == 1

    # 2. Trip to OPEN -> call_async raises fast
    cb.trip()
    assert cb.state == OPEN
    with pytest.raises(RpcCircuitOpenError):
        proxy.call_async(amount=200)
    assert len(dispatched_calls) == 1

    # 3. Wait for cooldown to transition to HALF_OPEN
    await asyncio.sleep(0.06)
    assert cb.state == HALF_OPEN

    # In HALF_OPEN, call_async is allowed as a probe
    coro3 = proxy.call_async(amount=300)
    await coro3
    assert len(dispatched_calls) == 2


@pytest.mark.asyncio
async def test_concurrent_stop_during_abortive_cleanup_causes_resource_leak():
    """Demonstrates that calling stop() while abortive start() is in _stop_internal
    cancels the cleanup task, truncating teardown and leaking disconnect.
    """
    cfg = ServiceConfig(name="leak_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)

    in_timer_stop = asyncio.Event()
    allow_timer_stop = asyncio.Event()

    async def blocking_stop_timers():
        in_timer_stop.set()
        await allow_timer_stop.wait()

    svc.connect = AsyncMock()  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Startup failed"))  # type: ignore[method-assign]
    svc._stop_timers = blocking_stop_timers  # type: ignore[method-assign]
    mock_disconnect = AsyncMock()
    svc.disconnect = mock_disconnect  # type: ignore[method-assign]

    start_task = asyncio.create_task(svc.start())

    # Wait until startup fails and reaches _stop_timers inside _stop_internal
    await asyncio.wait_for(in_timer_stop.wait(), timeout=2.0)

    # Call svc.stop() concurrently
    stop_task = asyncio.create_task(svc.stop())

    # Allow timer stop to proceed
    allow_timer_stop.set()

    results = await asyncio.gather(start_task, stop_task, return_exceptions=True)

    # Note: start_task raised CancelledError instead of RuntimeError("Startup failed")
    # and mock_disconnect was NEVER called!
    assert mock_disconnect.await_count == 1, (
        f"Resource leak reproduced: disconnect was called {mock_disconnect.await_count} times; "
        f"cleanup was aborted by stop() cancellation. Results: {results}"
    )


@pytest.mark.asyncio
async def test_concurrent_stop_during_slow_shutdown():
    """Stress test: 50 concurrent stop() calls arriving while on_shutdown is slow."""
    cfg = ServiceConfig(name="slow_shutdown_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)
    await svc.start()

    in_shutdown = asyncio.Event()
    allow_shutdown = asyncio.Event()

    async def slow_on_shutdown():
        in_shutdown.set()
        await allow_shutdown.wait()
        svc.on_shutdown_calls += 1

    svc.on_shutdown = slow_on_shutdown  # type: ignore[method-assign]

    first_stop = asyncio.create_task(svc.stop())
    await asyncio.wait_for(in_shutdown.wait(), timeout=2.0)

    # Stampede 50 stop() calls while the first is blocked inside on_shutdown
    concurrent_stops = [asyncio.create_task(svc.stop()) for _ in range(50)]

    # Unblock shutdown
    allow_shutdown.set()

    await asyncio.wait_for(first_stop, timeout=2.0)
    await asyncio.wait_for(asyncio.gather(*concurrent_stops), timeout=2.0)

    assert svc._running is False
    assert svc._stopped is True
    assert svc.on_shutdown_calls == 1

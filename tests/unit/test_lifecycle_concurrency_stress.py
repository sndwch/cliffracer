"""Empirical adversarial stress-test suite for service lifecycle and concurrency.

Tests concurrency, lifecycle, pull consumer CPU yield, BatchProcessor WeakSet safety,
and ResilientMethodProxy async coroutine dispatch.
"""

import asyncio
import inspect
import json
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
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit

# ============================================================================
# Section 1: Concurrent start() and stop() calls under load & cancellation
# ============================================================================


class LifecycleInstrumentedService(ServicePhases, CliffracerService):
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

    def released(self) -> tuple[int, int, int]:
        """How often each teardown resource was released: timers, extensions, transport."""
        return (self.stop_timers_calls, self.stop_extensions_calls, self.disconnect_calls)


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
        # What the interrupted startup had acquired is released, once: the transport it connected
        # (not yet connected when the cancel landed in `connect`), the timers and the extensions.
        assert svc.connect_calls == (0 if target_stage == "connect" else 1), target_stage
        assert svc.released() == (1, 1, 1), (
            f"(timers, extensions, transport) released after a cancel in {target_stage}"
        )

        # Multiple defensive stop calls must remain safe no-ops
        for _ in range(5):
            await svc.stop()
        assert svc.on_shutdown_calls == expected_shutdown_calls
        assert svc.released() == (1, 1, 1), (
            f"a defensive stop released a resource again, {target_stage}"
        )


@pytest.mark.asyncio
async def test_interleaved_start_stop_high_concurrency_race():
    """Adversarial race test: 50 tasks alternating start() and stop(), on a service that STARTS.

    The stop workers wait until one start() has completed, so the storm is made of stops
    arriving at a running service and starts arriving behind them. Without that wait,
    start() is refused for as long as any stop is pending, and not one of 125 starts ever
    reached `setup_subscriptions`: the storm ran against a service that never came up.

    Verifies no deadlock, no unhandled exceptions, that a full startup really happened, that
    what each startup acquired is released by the teardown that follows it, and a coherent
    terminal state.
    """
    cfg = ServiceConfig(name="race_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)
    one_start_completed = asyncio.Event()

    async def worker(action: str):
        if action == "stop":
            await asyncio.wait_for(one_start_completed.wait(), timeout=2.0)
        for _ in range(5):
            await asyncio.sleep(random.uniform(0.0001, 0.001))
            if action == "start":
                try:
                    await svc.start()
                except (asyncio.CancelledError, ServiceLifecycleError):
                    pass
                else:
                    one_start_completed.set()
            else:
                await svc.stop()

    tasks = []
    for i in range(50):
        action = "start" if i % 2 == 0 else "stop"
        tasks.append(asyncio.create_task(worker(action)))

    # Must complete cleanly within 5 seconds (prevent deadlocks)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    # The storm brought the service up at least once: the last step of startup ran.
    assert svc.setup_subscriptions_calls >= 1, "no start() ever completed"

    # Every startup that completed `on_startup` was followed by exactly one `on_shutdown`, and
    # every extension that was started was stopped. Stops that arrive when nothing is running
    # add stops, never starts, so the counts only ever move toward the teardown side.
    stopped_so_far = svc._stopped
    await svc.stop()
    assert svc.on_shutdown_calls == svc.on_startup_calls, (
        svc.on_startup_calls,
        svc.on_shutdown_calls,
    )
    assert svc.stop_extensions_calls >= svc.start_extensions_calls
    assert svc.disconnect_calls >= svc.connect_calls
    # Every teardown releases the timers, the extensions and the transport together, so the three
    # counts agree, whatever number of teardowns the storm produced. One skipped, or run twice,
    # makes them differ.
    timers, extensions, transport = svc.released()
    assert transport >= 1, "the storm never tore the service down"
    assert timers == extensions == transport, svc.released()
    assert svc._running is False
    assert svc._stopped is True, stopped_so_far

    # The state a restart must leave is built explicitly: a service that was stopped and is
    # started again is running and no longer stopped. A start() that forgot to clear
    # _stopped reports both.
    await svc.start()
    assert svc._running is True
    assert svc._stopped is False

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
        # No leak: the failed startup released the transport it had connected (none yet when
        # `connect` itself failed), the timers and the extensions, once each.
        assert svc.connect_calls == (0 if step == "connect" else 1), step
        assert svc.released() == (1, 1, 1), (
            f"(timers, extensions, transport) released after a failure in {step}"
        )

        # Defensive stop in finally block
        await svc.stop()
        assert svc.on_shutdown_calls == expected_shutdown_calls
        assert svc.released() == (1, 1, 1), f"a defensive stop released a resource again, {step}"
        assert svc._stopped is True


# ============================================================================
# Section 3: Container._pull_loop CPU yield and 0-message backoff
# ============================================================================


@pytest.mark.asyncio
async def test_pull_loop_sleeps_after_every_empty_fetch_before_the_next():
    """With nothing to fetch the loop alternates a fetch and a sleep of a twentieth of a second.

    Read as the order of the loop's own calls, with no clock: a loop that sleeps less, sleeps
    after only some empty fetches, or does not sleep at all gives a different sequence, and none
    of them can hang the test, because the fetch that ends the loop is counted, not timed.
    """
    cfg = ServiceConfig(name="pull_yield_svc", health_port=0)
    svc = LifecycleInstrumentedService(cfg)
    svc._running = True
    container = Container(svc, cfg)

    empty_fetches = 5
    calls: list[tuple[str, float | None]] = []

    async def empty_fetch(*args, **kwargs):
        calls.append(("fetch", None))
        if sum(1 for kind, _ in calls if kind == "fetch") == empty_fetches:
            svc._running = False
        raise TimeoutError

    async def recorded_sleep(delay, *args, **kwargs):
        calls.append(("sleep", delay))

    pull_sub = AsyncMock()
    pull_sub.fetch = AsyncMock(side_effect=empty_fetch)
    pull_sub.unsubscribe = AsyncMock()

    with patch("asyncio.sleep", new=recorded_sleep):
        await container._pull_loop(pull_sub, "durable_1")

    assert calls == [("fetch", None), ("sleep", 0.05)] * empty_fetches, calls


@pytest.mark.asyncio
async def test_pull_loop_cpu_yield_on_zero_messages():
    """On a real clock, a loop with nothing to fetch makes a handful of fetches and idles the CPU.

    The end-to-end reading of "does not spin": a tight loop makes thousands of fetches in the
    window and uses a whole core. That the yield between fetches is `sleep(0.05)` is read by the
    test above, from the loop's own calls; the counts here only bound what a real clock shows.
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
    # Upper bound. CI p99 0.00775 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 26x p99.
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
        with patch.object(container, "_handle_jetstream_event", new_callable=AsyncMock) as handled:
            await container._pull_loop(pull_sub, "durable_work")

            # Sleep(0.05) should NOT have been called after the batch of 2 messages
            sleep_durations = [call.args[0] for call in mock_sleep.await_args_list]
            # Since the second batch returned [] (count=0), sleep(0.05) is called once
            assert sleep_durations == [0.05]

    # "Processes immediately" means the two fetched messages reached the handler, in order. The
    # sleep pattern alone is unchanged by a loop that fetches messages and drops them.
    assert [call.args[0] for call in handled.await_args_list] == [msg1, msg2], (
        handled.await_args_list
    )


# ============================================================================
# Section 4: BatchProcessor.shutdown WeakSet concurrency safety
# ============================================================================


@pytest.mark.asyncio
async def test_batch_processor_shutdown_concurrent_task_mutation_stress():
    """Stress test: tracked tasks finishing while shutdown() runs, 10 rounds of 100.

    shutdown() must wait for every tracked task, however they interleave with it. The
    `list()` snapshot is not what this reads: `gather(*weakset)` unpacks the set in one
    synchronous expression, so the done-callbacks that discard from it (run via call_soon)
    cannot interleave, and removing `list()` leaves this test green. What the test can see is
    the drain. The batch state `shutdown()` clears is read in
    packages/cliffracer-metrics/tests/test_batch_processor_shutdown_drains_what_it_promises.py,
    where `add_item` fills it first; here nothing does.
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
        unfinished = [t for t in tasks if not t.done()]
        assert not unfinished, f"shutdown() returned with {len(unfinished)} tracked tasks running"


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
    # ...and wait for both tasks to finish first: the flag is set on shutdown()'s first line
    assert t1.done() and t2.done(), "shutdown() returned before its tracked tasks finished"
    assert isinstance(t1.exception(), ValueError)
    assert t2.cancelled()


# ============================================================================
# Section 5: ResilientMethodProxy.call_async coroutine and dispatch semantics
# ============================================================================


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_forwards_its_arguments_to_the_service():
    """With the circuit CLOSED, `call_async` hands the service the target, method, namespace and
    keyword arguments. The service's own `call_async` is a mock here, so what is asserted is the
    forwarding; what the proxy returns is read against the real one in the next test."""

    class OrderService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = OrderService(ServiceConfig(name="order_svc"))
    svc.call_async = MagicMock(return_value=asyncio.sleep(0))  # type: ignore[method-assign]

    proxy = svc.inventory.update_stock
    assert proxy.circuit_breaker.state == CLOSED

    await proxy.call_async(sku="SKU-100", delta=-1)

    svc.call_async.assert_called_once_with(
        "inventory_service", "update_stock", namespace=None, sku="SKU-100", delta=-1
    )


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_returns_the_services_own_coroutine():
    """The real `CliffracerService.call_async` is a coroutine function: what the proxy returns is
    that coroutine, un-awaited, and nothing is sent until it is awaited."""

    class OrderService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = OrderService(ServiceConfig(name="order_svc"))
    svc.nc = AsyncMock()
    proxy = svc.inventory.update_stock
    assert proxy.circuit_breaker.state == CLOSED

    coro = proxy.call_async(sku="SKU-100", delta=-1)

    assert inspect.iscoroutine(coro)
    svc.nc.publish.assert_not_called()

    await coro
    svc.nc.publish.assert_awaited_once()
    subject, body = svc.nc.publish.await_args.args[:2]
    assert subject == "inventory_service.async.update_stock"
    assert json.loads(body)["sku"] == "SKU-100"


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_open_circuit_fast_fail():
    """Verify call_async raises RpcCircuitOpenError fast when OPEN without coroutine leak."""

    class OrderService(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service")

    svc = OrderService(ServiceConfig(name="order_svc"))
    svc.call_async = MagicMock()  # type: ignore[method-assign]

    # Manually trip circuit breaker to OPEN
    svc.inventory.circuit_breaker.trip()
    assert svc.inventory.circuit_breaker.state == OPEN

    # 50 fast-fail calls under load
    for i in range(50):
        with pytest.raises(RpcCircuitOpenError) as exc_info:
            svc.inventory.update_stock.call_async(sku=f"SKU-{i}")
        assert "OPEN" in str(exc_info.value)

    # No RPCs dispatched
    svc.call_async.assert_not_called()


@pytest.mark.asyncio
async def test_resilient_method_proxy_call_async_circuit_transitions():
    """Verify call_async behavior across CLOSED -> OPEN -> HALF_OPEN states."""
    config = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05)
    cb = CircuitBreaker("billing-service", config=config)

    class BillingClient(CliffracerService):
        pass

    svc = BillingClient(ServiceConfig(name="billing_client"))
    dispatched_calls = []

    async def mock_call_async(service, method, namespace=None, **kwargs):
        dispatched_calls.append((service, method, kwargs))

    svc.call_async = MagicMock(side_effect=mock_call_async)  # type: ignore[method-assign]

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
async def test_a_stop_during_abortive_cleanup_waits_and_disconnect_runs_once():
    """A stop() that arrives while a failed start() is mid-cleanup waits for it.

    start() clears `_starting` and `_start_task` before it begins the cleanup, so the
    stop() has nothing to cancel: it queues on the lock the failing start() still holds.
    The cleanup therefore completes and the transport is disconnected exactly once, by
    the start(), not twice. The shield around the cleanup task is exercised by a cancel of
    the start() task itself, in test_lifecycle_abortive_stress.py and
    test_lifecycle_shielded_stress.py, not here.
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

    start_result, stop_result = results
    assert isinstance(start_result, RuntimeError) and "Startup failed" in str(start_result), (
        f"start() ended with {start_result!r}"
    )
    assert stop_result is None, f"stop() ended with {stop_result!r}"
    assert mock_disconnect.await_count == 1, (
        f"disconnect was awaited {mock_disconnect.await_count} times, expected once. "
        f"Results: {results}"
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

    # They wait for the stop in progress: none returns while it is still blocked. A stop() that
    # did not serialise would either return at once or run the teardown a second time.
    await asyncio.sleep(0.05)
    assert not first_stop.done()
    assert not any(t.done() for t in concurrent_stops)

    # Unblock shutdown
    allow_shutdown.set()

    await asyncio.wait_for(first_stop, timeout=2.0)
    await asyncio.wait_for(asyncio.gather(*concurrent_stops), timeout=2.0)

    assert svc._running is False
    assert svc._stopped is True
    assert svc.on_shutdown_calls == 1
    # And the teardown behind it ran once: `on_shutdown_calls == 1` alone is held by a one-shot
    # flag that `stop_internal` clears before the hook, so it does not need the lock.
    assert svc.disconnect_calls == 1

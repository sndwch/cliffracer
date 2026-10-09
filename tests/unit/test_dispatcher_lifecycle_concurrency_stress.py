"""Adversarial stress test suite for MessageDispatcher and LifecycleManager concurrency.

Verifies under high load, adverse timing, and fault conditions:
1. Concurrency bounds on MessageDispatcher (semaphore capacity, queueing, correlation inheritance, zero task leaks).
2. Rapid start/stop/abort stress cycles (reentrant locks, abortive cleanup, overlapping start/stop races).
3. In-flight event and async task draining under forced exception triggers, timeouts, and hook errors during shutdown.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import BaseModel

from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.decorators import listener, rpc
from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.extension import Extension, WorkerContext
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing.messages import MockMessage, TestResponse

pytestmark = pytest.mark.unit

# ==============================================================================
# Models for Typed RPC handlers
# ==============================================================================


class JobResult(BaseModel):
    job_id: int
    status: str


class WorkResult(BaseModel):
    item_id: int
    echo_cid: str | None = None
    ambient_cid: str | None = None
    subtask_cid: str | None = None


# ==============================================================================
# Helper Mock Factories
# ==============================================================================


def make_mock_nc() -> AsyncMock:
    nc = AsyncMock()
    nc.is_connected = True
    nc.is_closed = False
    nc.is_draining = False
    nc.publish = AsyncMock()
    nc.subscribe = AsyncMock(return_value=AsyncMock())
    nc.flush = AsyncMock()
    nc.close = AsyncMock()
    nc.drain = AsyncMock()
    return nc


def make_rpc_message(
    subject: str,
    payload: dict[str, Any] | None = None,
    raw_data: bytes | None = None,
    correlation_id: str | None = None,
) -> MockMessage:
    data = raw_data if raw_data is not None else json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
        headers["correlation_id"] = correlation_id
    return MockMessage(subject=subject, data=data, headers=headers)


async def assert_every_permit_is_free(sem: asyncio.Semaphore | None, permits: int) -> None:
    """Take all `permits` of a semaphore and show there is not one more.

    A request that finishes without returning its permit leaves the semaphore
    one short for good, and no request's reply shows it: concurrency merely
    drops by one. Only counting the permits does.
    """
    assert sem is not None, "the dispatcher never created the semaphore it should be using"
    taken = 0
    try:
        for _ in range(permits):
            await asyncio.wait_for(sem.acquire(), timeout=0.2)
            taken += 1
    except TimeoutError:
        pytest.fail(f"only {taken} of {permits} permits are free: the rest were never returned")
    finally:
        for _ in range(taken):
            sem.release()
    assert taken == permits
    # And no more than that: a semaphore with a permit too many bounds nothing.
    for _ in range(permits):
        await sem.acquire()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sem.acquire(), timeout=0.05)
    finally:
        for _ in range(permits):
            sem.release()


# ==============================================================================
# 1. MessageDispatcher Concurrency Stress, Queueing, & Correlation
# ==============================================================================


@pytest.mark.asyncio
async def test_rpc_concurrency_stress_strict_semaphore_bound() -> None:
    """Simultaneous RPC requests exceeding semaphore capacity are queued without exceeding bound."""
    active_in_flight = 0
    max_observed_concurrency = 0
    concurrency_lock = asyncio.Lock()
    processed_count = 0

    class HeavyWorkService(CliffracerService):
        @rpc
        async def compute(self, job_id: int) -> JobResult:
            nonlocal active_in_flight, max_observed_concurrency, processed_count
            async with concurrency_lock:
                active_in_flight += 1
                if active_in_flight > max_observed_concurrency:
                    max_observed_concurrency = active_in_flight

            # Simulate non-trivial I/O delay
            await asyncio.sleep(0.01)

            async with concurrency_lock:
                active_in_flight -= 1
                processed_count += 1

            return JobResult(job_id=job_id, status="done")

    cfg = ServiceConfig(name="stress_rpc_svc", max_rpc_concurrency=3, health_port=0)
    svc = HeavyWorkService(cfg)
    svc.container.discover_handlers()

    total_requests = 60
    messages = [
        make_rpc_message("stress_rpc_svc.rpc.compute", payload={"job_id": i})
        for i in range(total_requests)
    ]

    # Dispatch 60 simultaneous requests through dispatcher.on_rpc_request
    await asyncio.gather(*[svc.container.dispatcher.on_rpc_request(m) for m in messages])

    # Drain any spawned background tasks in lifecycle
    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    # Invariants verification:
    # 1. Semaphore strictly bounded concurrency to <= 3
    assert max_observed_concurrency <= 3, (
        f"Observed concurrency {max_observed_concurrency} exceeded max 3"
    )
    assert max_observed_concurrency > 1, "Concurrency was unexpectedly serialized"

    # 2. All 60 requests were queued and processed
    assert processed_count == total_requests

    # 3. All 60 requests received successful responses
    for i, m in enumerate(messages):
        resp = TestResponse.from_mock_message(m)
        assert resp.success is True
        assert resp.result == {"job_id": i, "status": "done"}

    # 4. Zero task leaks
    assert len(svc.container.lifecycle.active_tasks) == 0


@pytest.mark.asyncio
async def test_rpc_concurrency_correlation_inheritance_and_no_crosstalk() -> None:
    """Under heavy concurrency load, every request inherits and maintains its own correlation ID."""

    class CorrelationService(CliffracerService):
        @rpc
        async def work(self, item_id: int, correlation_id: str | None = None) -> WorkResult:
            ambient_cid = CorrelationContext.get()
            # Verify explicit correlation parameter matches ambient context
            assert correlation_id == ambient_cid

            # Spawn a sub-task to verify context inheritance
            async def sub_task() -> str | None:
                await asyncio.sleep(0.005)
                return CorrelationContext.get()

            inherited_cid = await asyncio.create_task(sub_task())
            # Simulate randomized jitter to stress concurrency interleaving
            await asyncio.sleep(random.uniform(0.005, 0.015))

            return WorkResult(
                item_id=item_id,
                echo_cid=correlation_id,
                ambient_cid=ambient_cid,
                subtask_cid=inherited_cid,
            )

    cfg = ServiceConfig(name="corr_svc", max_rpc_concurrency=4, health_port=0)
    svc = CorrelationService(cfg)
    svc.container.discover_handlers()

    total_requests = 80
    messages: list[MockMessage] = []
    expected_cids: list[str] = []

    for i in range(total_requests):
        cid = f"req-trace-{i:04d}-{random.randint(1000, 9999)}"
        expected_cids.append(cid)
        messages.append(
            make_rpc_message(
                "corr_svc.rpc.work",
                payload={"item_id": i},
                correlation_id=cid,
            )
        )

    # Fire all 80 requests simultaneously
    await asyncio.gather(*[svc.container.dispatcher.on_rpc_request(m) for m in messages])

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    # Verify zero task leaks
    assert len(svc.container.lifecycle.active_tasks) == 0

    # Verify each response strictly preserved its distinct correlation ID
    for _i, (m, expected_cid) in enumerate(zip(messages, expected_cids, strict=False)):
        resp = TestResponse.from_mock_message(m)
        assert resp.success is True
        assert resp.data.get("correlation_id") == expected_cid
        assert resp.result["echo_cid"] == expected_cid
        assert resp.result["ambient_cid"] == expected_cid
        assert resp.result["subtask_cid"] == expected_cid


@pytest.mark.asyncio
async def test_rpc_concurrency_mixed_payloads_errors_and_zero_semaphore_leaks() -> None:
    """Fault injection under concurrency: malformed JSON, schema errors, and exceptions release permits."""

    class MixedService(CliffracerService):
        @rpc
        async def valid_method(self, x: int) -> int:
            await asyncio.sleep(0.005)
            return x * 2

        @rpc
        async def failing_method(self, x: int) -> int:
            await asyncio.sleep(0.005)
            raise RuntimeError(f"Simulated internal crash on x={x}")

    cfg = ServiceConfig(name="mixed_svc", max_rpc_concurrency=3, health_port=0)
    svc = MixedService(cfg)
    svc.container.discover_handlers()

    batch_size = 15
    messages: list[tuple[str, MockMessage]] = []

    for i in range(batch_size):
        cid_valid = f"valid-{i}"
        messages.append(
            (
                "valid",
                make_rpc_message(
                    "mixed_svc.rpc.valid_method", payload={"x": i}, correlation_id=cid_valid
                ),
            )
        )

        cid_malformed = f"malformed-{i}"
        messages.append(
            (
                "malformed",
                make_rpc_message(
                    "mixed_svc.rpc.valid_method",
                    raw_data=b"{{invalid json",
                    correlation_id=cid_malformed,
                ),
            )
        )

        cid_failing = f"failing-{i}"
        messages.append(
            (
                "failing",
                make_rpc_message(
                    "mixed_svc.rpc.failing_method", payload={"x": i}, correlation_id=cid_failing
                ),
            )
        )

        cid_unknown = f"unknown-{i}"
        messages.append(
            (
                "unknown",
                make_rpc_message(
                    "mixed_svc.rpc.nonexistent", payload={"x": i}, correlation_id=cid_unknown
                ),
            )
        )

    # Total 60 mixed requests across 4 categories
    random.shuffle(messages)
    await asyncio.gather(*[svc.container.dispatcher.on_rpc_request(m) for _, m in messages])

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    # Invariants verification:
    # 1. Zero task leaks
    assert len(svc.container.lifecycle.active_tasks) == 0

    # 2. Every category handled properly with structured error envelopes
    for kind, m in messages:
        resp = TestResponse.from_mock_message(m)
        if kind == "valid":
            assert resp.success is True
        elif kind == "malformed":
            assert resp.success is False
            assert "validation failed" in str(resp.error)
        elif kind == "failing":
            assert resp.success is False
            assert "Internal server error" in str(resp.error)
            assert not (isinstance(resp.data, dict) and "traceback" in resp.data)
        elif kind == "unknown":
            assert resp.success is False
            assert "Unknown method" in str(resp.error)

    # 3. Critical: Semaphore was not leaked. Every permit is back, and a new request runs.
    await assert_every_permit_is_free(svc.container.dispatcher._get_rpc_semaphore(), 3)
    test_msg = make_rpc_message("mixed_svc.rpc.valid_method", payload={"x": 99})
    await svc.container.dispatcher.on_rpc_request(test_msg)
    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=2.0)
    resp_after = TestResponse.from_mock_message(test_msg)
    assert resp_after.success is True
    assert resp_after.result == 198

    # 4. Verify expose_internal_errors=True opt-in exposes raw crash message and traceback
    cfg_opt = ServiceConfig(
        name="mixed_opt", max_rpc_concurrency=3, health_port=0, expose_internal_errors=True
    )
    svc_opt = MixedService(cfg_opt)
    svc_opt.container.discover_handlers()
    opt_msg = make_rpc_message("mixed_opt.rpc.failing_method", payload={"x": 42})
    await svc_opt.container.dispatcher.on_rpc_request(opt_msg)
    if svc_opt.container.lifecycle.active_tasks:
        await svc_opt.container.lifecycle.drain_active_tasks(timeout=2.0)
    resp_opt = TestResponse.from_mock_message(opt_msg)
    assert resp_opt.success is False
    assert "Simulated internal crash on x=42" in str(resp_opt.error)
    assert isinstance(resp_opt.data, dict) and "traceback" in resp_opt.data


@pytest.mark.asyncio
async def test_rpc_concurrency_client_cancellation_during_semaphore_wait() -> None:
    """When callers cancel while awaiting the semaphore, permits and state remain uncorrupted."""
    concurrency_entered = 0
    entered_event = asyncio.Event()

    class BlockedService(CliffracerService):
        @rpc
        async def slow_call(self) -> str:
            nonlocal concurrency_entered
            concurrency_entered += 1
            if concurrency_entered >= 2:
                entered_event.set()
            # Block until cancelled or finished
            await asyncio.sleep(0.5)
            return "ok"

        @rpc
        async def quick(self) -> str:
            return "ready"

    cfg = ServiceConfig(name="cancel_svc", max_rpc_concurrency=2, health_port=0)
    svc = BlockedService(cfg)
    svc.container.discover_handlers()

    # Launch 2 requests to saturate the semaphore
    sat_msgs = [make_rpc_message("cancel_svc.rpc.slow_call") for _ in range(2)]
    for m in sat_msgs:
        await svc.container.dispatcher.on_rpc_request(m)

    await entered_event.wait()
    sat_tasks = list(svc.container.lifecycle.active_tasks)

    # Now launch 10 queued requests. Each callback returns at once; each request's task waits for
    # a permit on its own, so those tasks are the ones a cancellation reaches while they wait.
    queued_msgs = [make_rpc_message("cancel_svc.rpc.slow_call") for _ in range(10)]
    for m in queued_msgs:
        await asyncio.wait_for(svc.container.dispatcher.on_rpc_request(m), timeout=1.0)
    queued_tasks = [t for t in svc.container.lifecycle.active_tasks if t not in sat_tasks]
    assert len(queued_tasks) == 10

    await asyncio.sleep(0.01)

    # Cancel 5 of the queued tasks while they are waiting on semaphore
    for t in queued_tasks[:5]:
        t.cancel()

    # Let the remaining 5 queued tasks continue, and cancel the saturating tasks
    for t in sat_tasks:
        t.cancel()

    await asyncio.gather(*sat_tasks, *queued_tasks[:5], return_exceptions=True)

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=2.0)

    assert len(svc.container.lifecycle.active_tasks) == 0

    # The semaphore is intact: every permit is back, and a request to THIS
    # service's own dispatcher runs at once.
    await assert_every_permit_is_free(svc.container.dispatcher._get_rpc_semaphore(), 2)
    quick_msg = make_rpc_message("cancel_svc.rpc.quick")
    await svc.container.dispatcher.on_rpc_request(quick_msg)
    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=2.0)
    quick_reply = TestResponse.from_mock_message(quick_msg)
    assert quick_reply.success is True
    assert quick_reply.result == "ready"
    assert len(svc.container.lifecycle.active_tasks) == 0


# ==============================================================================
# 2. Rapid Start / Stop / Abort Stress Cycles
# ==============================================================================


@pytest.mark.asyncio
async def test_rapid_serialized_start_stop_cycles() -> None:
    """Rapid succession of start() and stop() cycles cleanly initializes and reaps resources."""
    start_count = 0
    stop_count = 0

    class CyclicService(CliffracerService):
        @rpc
        async def ping(self) -> str:
            return "pong"

        async def on_startup(self) -> None:
            nonlocal start_count
            start_count += 1

        async def on_shutdown(self) -> None:
            nonlocal stop_count
            stop_count += 1

    cfg = ServiceConfig(name="cyclic_svc", health_port=0, health_listener=False)
    svc = CyclicService(cfg)

    mock_nc = make_mock_nc()

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    cycles = 25
    for _i in range(cycles):
        await svc.start()
        assert bool(svc._running) is True
        assert bool(svc.container.lifecycle.is_running) is True
        assert bool(svc.container.lifecycle.is_starting) is False
        assert bool(svc.container.lifecycle.is_stopped) is False

        # Verify RPC functions while running
        msg = make_rpc_message("cyclic_svc.rpc.ping")
        await svc.container.dispatcher.handle_rpc_request(msg)
        resp = TestResponse.from_mock_message(msg)
        assert resp.success is True
        assert resp.result == "pong"

        await svc.stop()
        assert bool(svc._running) is False
        assert bool(svc.container.lifecycle.is_running) is False
        assert bool(svc.container.lifecycle.is_stopped) is True
        assert len(svc.container.lifecycle.active_tasks) == 0

    assert start_count == cycles
    assert stop_count == cycles


@pytest.mark.asyncio
async def test_rapid_abortive_startup_cycles_and_recovery() -> None:
    """Startup exceptions trigger internal abortive cleanup without corrupting mutex or state."""
    abort_iterations = 20
    should_fail = True
    teardown_cleaned = 0

    class FlakyService(CliffracerService):
        async def on_startup(self) -> None:
            if should_fail:
                raise ConnectionError("Broker handshake rejected")

        async def on_shutdown(self) -> None:
            nonlocal teardown_cleaned
            teardown_cleaned += 1

    cfg = ServiceConfig(name="flaky_svc", health_port=0, health_listener=False)
    svc = FlakyService(cfg)

    mock_nc = make_mock_nc()
    disconnect_calls = 0

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        nonlocal disconnect_calls
        disconnect_calls += 1
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    # Run 20 abortive startups in a row
    for _i in range(abort_iterations):
        with pytest.raises(ConnectionError, match="Broker handshake rejected"):
            await svc.start()

        # Invariants after abortive startup:
        assert bool(svc._running) is False
        assert bool(svc.container.lifecycle.is_running) is False
        assert bool(svc.container.lifecycle.is_starting) is False
        assert len(svc.container.lifecycle.active_tasks) == 0
        assert svc.container.connection.nc is None

    assert disconnect_calls == abort_iterations

    # Now verify recovery: disable failure and start cleanly
    should_fail = False
    await svc.start()
    assert bool(svc._running) is True
    assert bool(svc.container.lifecycle.is_running) is True

    await svc.stop()
    assert bool(svc._running) is False
    assert bool(svc.container.lifecycle.is_stopped) is True


@pytest.mark.asyncio
async def test_concurrent_overlapping_start_and_stop_race() -> None:
    """Calling stop() while start() is in flight cancels startup cleanly without deadlocking."""
    startup_started = asyncio.Event()
    # Never set: startup cannot finish by itself, so it ends only if stop()
    # interrupts it. A stop() that waited the startup out would hang here.
    never_released = asyncio.Event()
    startup_finished = False

    class SlowStartService(CliffracerService):
        async def on_startup(self) -> None:
            nonlocal startup_finished
            startup_started.set()
            await never_released.wait()
            startup_finished = True

    cfg = ServiceConfig(name="slow_start_svc", health_port=0, health_listener=False)
    svc = SlowStartService(cfg)

    mock_nc = make_mock_nc()
    disconnect_calls = 0

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        nonlocal disconnect_calls
        disconnect_calls += 1
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    # Launch start() in background
    start_task = asyncio.create_task(svc.start())

    await asyncio.wait_for(startup_started.wait(), timeout=10)

    # Intervene with stop() while startup is blocked inside on_startup()
    stop_task = asyncio.create_task(svc.stop())

    # The bound only turns a deadlock into a report; it is not a duration claim.
    _, pending = await asyncio.wait({start_task, stop_task}, timeout=10)
    assert not pending, f"start/stop did not finish: {pending}"

    # 1. The startup was interrupted, not waited out and not failed some other way.
    assert start_task.cancelled(), "start() must end cancelled by stop()"
    assert startup_finished is False
    assert stop_task.exception() is None
    mock_nc.subscribe.assert_not_called()
    assert disconnect_calls == 1

    # 2. State must be fully stopped, never running
    assert bool(svc.container.lifecycle.is_running) is False
    assert bool(svc.container.lifecycle.is_starting) is False
    assert bool(svc.container.lifecycle.is_stopped) is True
    assert len(svc.container.lifecycle.active_tasks) == 0

    # 3. Mutex must not remain locked
    assert not svc.container.lifecycle.lock.locked()


@pytest.mark.asyncio
async def test_multiple_concurrent_stop_calls() -> None:
    """Multiple parallel stop() calls are idempotent and serialize under the lifecycle lock."""
    shutdown_calls = 0

    class SharedStopService(CliffracerService):
        async def on_shutdown(self) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1
            await asyncio.sleep(0.01)

    cfg = ServiceConfig(name="multi_stop_svc", health_port=0, health_listener=False)
    svc = SharedStopService(cfg)

    mock_nc = make_mock_nc()

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    await svc.start()
    assert bool(svc._running) is True

    # 20 concurrent coroutines invoking stop() simultaneously
    stops = [asyncio.create_task(svc.stop()) for _ in range(20)]
    await asyncio.gather(*stops)

    # Teardown executed exactly once
    assert shutdown_calls == 1
    assert bool(svc._running) is False
    assert bool(svc.container.lifecycle.is_stopped) is True
    assert len(svc.container.lifecycle.active_tasks) == 0


@pytest.mark.asyncio
async def test_start_rejected_when_stop_requested() -> None:
    """start() rejects execution if stop has already been requested."""
    cfg = ServiceConfig(name="reject_start_svc", health_port=0, health_listener=False)
    svc = CliffracerService(cfg)

    # Simulate pending stop request
    svc.container.lifecycle._stop_requests = 1

    with pytest.raises(ServiceLifecycleError, match="cannot start: stop has been requested"):
        await svc.start()

    assert bool(svc._running) is False
    svc.container.lifecycle._stop_requests = 0


# ==============================================================================
# 3. In-Flight Draining Under Forced Exception Triggers
# ==============================================================================


@pytest.mark.asyncio
async def test_in_flight_tasks_drained_cleanly_during_shutdown() -> None:
    """In-flight supervised tasks run to completion during graceful shutdown."""
    completed_tasks = 0
    total_tasks = 20

    cfg = ServiceConfig(
        name="drain_svc", shutdown_timeout=3.0, health_port=0, health_listener=False
    )
    svc = CliffracerService(cfg)

    async def worker(idx: int) -> None:
        nonlocal completed_tasks
        await asyncio.sleep(0.02)
        completed_tasks += 1

    # Spawn 20 tasks via lifecycle supervised spawner
    for i in range(total_tasks):
        svc.container.lifecycle.spawn_supervised_task(worker(i), name=f"worker_{i}")

    assert len(svc.container.lifecycle.active_tasks) == total_tasks

    # Stop service (triggers drain_active_tasks)
    await svc.stop()

    assert completed_tasks == total_tasks
    assert len(svc.container.lifecycle.active_tasks) == 0
    assert bool(svc.container.lifecycle.is_stopped) is True


@pytest.mark.asyncio
async def test_in_flight_tasks_raising_exceptions_drained_without_unretrieved_warnings() -> None:
    """Tasks raising unhandled exceptions during shutdown are drained and exceptions retrieved."""
    total_tasks = 30
    crashed_count = 0

    cfg = ServiceConfig(
        name="crash_drain_svc", shutdown_timeout=3.0, health_port=0, health_listener=False
    )
    svc = CliffracerService(cfg)

    async def faulty_worker(idx: int) -> None:
        nonlocal crashed_count
        await asyncio.sleep(0.01)
        if idx % 2 == 0:
            crashed_count += 1
            raise RuntimeError(f"Forced background worker crash on idx={idx}")

    logged: list[str] = []
    sink_id = logger.add(
        lambda message: logged.append(message.record["message"]), level="ERROR", format="{message}"
    )
    try:
        # Spawn 30 tasks where half raise unhandled exceptions
        for i in range(total_tasks):
            svc.container.lifecycle.spawn_supervised_task(faulty_worker(i), name=f"faulty_{i}")

        assert len(svc.container.lifecycle.active_tasks) == total_tasks

        # stop() should gracefully drain without crashing the shutdown sequence
        await svc.stop()
    finally:
        logger.remove(sink_id)

    assert crashed_count == total_tasks // 2
    # Each crash is reported by the framework, under the task's own name.
    crashed_names = [f"faulty_{i}" for i in range(total_tasks) if i % 2 == 0]
    for name in crashed_names:
        assert any(f"task '{name}':" in m for m in logged), name
    assert len([m for m in logged if "Unhandled exception in background task" in m]) == len(
        crashed_names
    )
    assert len(svc.container.lifecycle.active_tasks) == 0
    assert bool(svc.container.lifecycle.is_stopped) is True


# The workers below loop forever, so a shutdown that waits for them instead of
# cancelling them does not take longer -- it never returns. This bound is what
# turns that into a report, and no host speed can cross it, because the thing on
# the other side of it is unbounded rather than slow. It says nothing about how
# long shutdown should take.
@pytest.mark.timeout(10)
@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_timeout", [0.05, 0.6])
async def test_in_flight_hanging_tasks_cancelled_after_shutdown_timeout(
    shutdown_timeout: float,
) -> None:
    """Tasks that hang indefinitely are cancelled when shutdown_timeout expires."""
    cfg = ServiceConfig(
        name="hang_svc", shutdown_timeout=shutdown_timeout, health_port=0, health_listener=False
    )
    svc = CliffracerService(cfg)

    cancelled_count = 0

    async def infinite_worker(idx: int) -> None:
        nonlocal cancelled_count
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            cancelled_count += 1
            raise

    # Spawn 10 infinite tasks
    for i in range(10):
        svc.container.lifecycle.spawn_supervised_task(infinite_worker(i), name=f"hang_{i}")

    assert len(svc.container.lifecycle.active_tasks) == 10

    # Stop cancels what is left at its deadline and finishes cleanly.
    started = time.monotonic()
    await svc.stop()
    elapsed = time.monotonic() - started

    # Every worker loops forever, so a drain that waited for them instead of
    # cancelling them never reaches this count.
    assert cancelled_count == 10
    assert len(svc.container.lifecycle.active_tasks) == 0
    assert bool(svc.container.lifecycle.is_stopped) is True

    # The deadline is the configured one. Only the floor is asserted: nothing
    # can end the drain before its deadline while the workers hang, and a slow
    # host can only lengthen it, so a floor cannot flake where a ceiling would.
    # Two values keep it from being met by one fixed constant; the larger is
    # what a drain that ignored the setting for a shorter fixed one falls under.
    assert elapsed >= shutdown_timeout * 0.9, (
        f"stop() returned after {elapsed:.3f}s with shutdown_timeout={shutdown_timeout}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_timeout", [0.05, 0.3, 7.0])
async def test_stop_drains_active_tasks_under_the_configured_shutdown_timeout(
    shutdown_timeout: float,
) -> None:
    """The deadline handed to the drain is the config's, whatever its value."""
    cfg = ServiceConfig(
        name="deadline_svc",
        shutdown_timeout=shutdown_timeout,
        health_port=0,
        health_listener=False,
    )
    svc = CliffracerService(cfg)
    lifecycle = svc.container.lifecycle

    deadlines: list[float | None] = []
    real_drain = lifecycle.drain_active_tasks

    async def recording_drain(timeout: float | None = 30.0) -> None:
        deadlines.append(timeout)
        await real_drain(timeout=timeout)

    lifecycle.drain_active_tasks = recording_drain  # type: ignore[method-assign]

    # Not started: this never opens a connection.
    await svc.stop()

    assert deadlines == [shutdown_timeout]


@pytest.mark.asyncio
async def test_shutdown_resilience_when_teardown_hooks_raise() -> None:
    """Failures in timers, subscriptions, or on_shutdown do not abort task draining or disconnect."""
    active_drained = False
    on_shutdown_ran = False

    class BrokenTeardownService(CliffracerService):
        async def on_shutdown(self) -> None:
            nonlocal on_shutdown_ran
            on_shutdown_ran = True
            raise KeyError("Failed during user on_shutdown hook")

    cfg = ServiceConfig(
        name="broken_teardown_svc", shutdown_timeout=2.0, health_port=0, health_listener=False
    )
    svc = BrokenTeardownService(cfg)

    mock_nc = make_mock_nc()
    disconnect_called = False

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        nonlocal disconnect_called
        disconnect_called = True
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]  # type: ignore[method-assign]

    await svc.start()

    # In-flight task
    async def worker() -> None:
        nonlocal active_drained
        await asyncio.sleep(0.01)
        active_drained = True

    svc.container.lifecycle.spawn_supervised_task(worker(), name="in_flight")

    # Make the timers stop and cancel_subscriptions fail, as well as on_shutdown above
    svc.container._stop_timers = AsyncMock(side_effect=RuntimeError("timers wedged"))  # type: ignore[method-assign]
    svc.container.connection.unsubscribe_all = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("NATS unreachable")
    )

    # stop() should execute in-flight drain and disconnect, but raise the first error: the timers'
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    try:
        with pytest.raises(RuntimeError, match="timers wedged"):
            await svc.stop()
    finally:
        logger.remove(sink)

    assert active_drained is True
    assert disconnect_called is True
    assert len(svc.container.lifecycle.active_tasks) == 0
    # Each failure was met and recorded where the later ones would have hidden it: only the first
    # is raised, so the log is the record of the others, and on_shutdown was reached at all.
    assert on_shutdown_ran is True
    for stage in ("stopping timers", "cancelling subscriptions", "Error in on_shutdown"):
        assert any(stage in line for line in lines), (stage, lines)


async def _stop_while_a_task_spawns_a_child(child_body: Any, shutdown_timeout: float) -> Any:
    """Stop a service whose running task spawns a supervised child once the stop is under way.

    Returns the service and whether the child was tracked at the moment it was spawned.
    """
    cfg = ServiceConfig(
        name="dynamic_spawn_svc",
        shutdown_timeout=shutdown_timeout,
        health_port=0,
        health_listener=False,
    )
    svc = CliffracerService(cfg)
    lifecycle = svc.container.lifecycle
    child_tracked: list[bool] = []

    async def parent_worker() -> None:
        while not lifecycle.stop_requested:
            await asyncio.sleep(0)
        child = lifecycle.spawn_supervised_task(child_body(), name="child_worker")
        child_tracked.append(child in lifecycle.active_tasks)

    lifecycle.spawn_supervised_task(parent_worker(), name="parent_worker")

    await asyncio.wait_for(svc.stop(), timeout=5.0)
    return svc, child_tracked


@pytest.mark.asyncio
async def test_a_task_spawned_while_stopping_is_tracked_and_awaited() -> None:
    """A child spawned once the stop is under way is supervised, and stop() waits for it."""
    child_ran = False

    async def child_body() -> None:
        nonlocal child_ran
        await asyncio.sleep(0.05)
        child_ran = True

    svc, child_tracked = await _stop_while_a_task_spawns_a_child(child_body, shutdown_timeout=2.0)

    assert child_tracked == [True]
    # Asserted the moment stop() returns, with no sleep to outlast the child: the
    # drain is what made both of these true.
    assert child_ran is True
    assert len(svc.container.lifecycle.active_tasks) == 0


@pytest.mark.asyncio
async def test_a_task_spawned_while_stopping_is_cancelled_at_the_shutdown_deadline() -> None:
    """A child that outlives shutdown_timeout is cancelled, not left running."""
    child_cancelled = False

    async def child_body() -> None:
        nonlocal child_cancelled
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            child_cancelled = True
            raise

    started = asyncio.get_running_loop().time()
    svc, child_tracked = await _stop_while_a_task_spawns_a_child(child_body, shutdown_timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - started

    assert child_tracked == [True]
    assert child_cancelled is True
    assert len(svc.container.lifecycle.active_tasks) == 0
    # Upper bound. CI p99 0.202 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.2 s,
    # 1285x the overshoot; below 30 s (the child's sleep(30)).
    assert elapsed < 3.0, f"stop() took {elapsed:.1f}s against a 0.2s shutdown_timeout"


# ==============================================================================
# 4. Advanced Concurrency Stress & Fault Resilience
# ==============================================================================


@pytest.mark.asyncio
async def test_event_concurrency_stress_strict_semaphore_bound() -> None:
    """Event callbacks strictly obey max_event_concurrency without leaking tasks."""
    active_events = 0
    max_observed_event_concurrency = 0
    event_lock = asyncio.Lock()
    processed_events = 0

    class EventStressService(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, order_id: int) -> None:
            nonlocal active_events, max_observed_event_concurrency, processed_events
            async with event_lock:
                active_events += 1
                if active_events > max_observed_event_concurrency:
                    max_observed_event_concurrency = active_events

            await asyncio.sleep(0.01)

            async with event_lock:
                active_events -= 1
                processed_events += 1

    cfg = ServiceConfig(
        name="event_stress_svc", max_event_concurrency=3, health_port=0, health_listener=False
    )
    svc = EventStressService(cfg)
    svc.container.discover_handlers()

    cb = svc.container.dispatcher.make_event_callback("orders.created")

    total_events = 50
    messages = [
        make_rpc_message("orders.created", payload={"order_id": i}) for i in range(total_events)
    ]

    await asyncio.gather(*[cb(m) for m in messages])

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    assert max_observed_event_concurrency <= 3
    assert max_observed_event_concurrency > 1
    assert processed_events == total_events
    assert len(svc.container.lifecycle.active_tasks) == 0


@pytest.mark.asyncio
async def test_async_rpc_concurrency_stress_strict_semaphore_bound() -> None:
    """Fire-and-forget async RPC calls obey max_async_rpc_concurrency bound."""
    active_async = 0
    max_observed_async_concurrency = 0
    async_lock = asyncio.Lock()
    processed_async = 0

    class AsyncRpcService(CliffracerService):
        @rpc
        async def background_job(self, job_id: int) -> JobResult:
            nonlocal active_async, max_observed_async_concurrency, processed_async
            async with async_lock:
                active_async += 1
                if active_async > max_observed_async_concurrency:
                    max_observed_async_concurrency = active_async

            await asyncio.sleep(0.01)

            async with async_lock:
                active_async -= 1
                processed_async += 1

            return JobResult(job_id=job_id, status="done")

    cfg = ServiceConfig(
        name="async_rpc_svc",
        max_async_rpc_concurrency=2,
        max_rpc_concurrency=5,
        health_port=0,
        health_listener=False,
    )
    svc = AsyncRpcService(cfg)
    svc.container.discover_handlers()

    total_async = 40
    messages = [
        make_rpc_message("async_rpc_svc.rpc.background_job", payload={"job_id": i})
        for i in range(total_async)
    ]

    await asyncio.gather(*[svc.container.dispatcher.on_async_request(m) for m in messages])

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    # Bound must match max_async_rpc_concurrency (2), not max_rpc_concurrency (5)
    assert max_observed_async_concurrency <= 2
    # And the bound is reached: fire-and-forget calls run in parallel up to it, not one at a time,
    # as the RPC and event versions of this test also assert.
    assert max_observed_async_concurrency > 1
    assert processed_async == total_async
    assert len(svc.container.lifecycle.active_tasks) == 0


@pytest.mark.asyncio
async def test_stop_cancelled_during_drain_ensures_critical_teardown() -> None:
    """If stop() is cancelled while draining active tasks, extensions and transport disconnect still execute."""
    ext_stopped = False
    transport_disconnected = False

    class MonitoredExtension(Extension):
        async def stop(self) -> None:
            nonlocal ext_stopped
            ext_stopped = True

    class MonitoredService(CliffracerService):
        ext = MonitoredExtension()

    cfg = ServiceConfig(
        name="cancel_stop_svc", shutdown_timeout=5.0, health_port=0, health_listener=False
    )
    svc = MonitoredService(cfg)

    mock_nc = make_mock_nc()

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        nonlocal transport_disconnected
        transport_disconnected = True
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    await svc.start()

    # Create a long-running supervised task
    drain_entered = asyncio.Event()

    async def stubborn_task() -> None:
        drain_entered.set()
        await asyncio.sleep(10.0)

    svc.container.lifecycle.spawn_supervised_task(stubborn_task(), name="stubborn")

    # Launch stop() in background
    stop_task = asyncio.create_task(svc.stop())

    await drain_entered.wait()
    await asyncio.sleep(0.01)

    # Cancel stop() while it is draining
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    # Critical invariant: Shielded teardown hooks must have completed despite stop() cancellation!
    assert ext_stopped is True
    assert transport_disconnected is True
    assert svc.container.connection.nc is None


@pytest.mark.asyncio
async def test_a_cancel_during_each_teardown_hook_does_not_abort_it() -> None:
    """The extension stop and the transport disconnect finish although stop() is cancelled while each runs.

    The first cancel, during the drain, is consumed by the preliminary-teardown
    handler; after that nothing re-cancels the task, so the hooks complete
    shielded or not. What the two shields are for is a LATER cancel, delivered
    while a hook is running: without its shield the hook's own await is the one
    cancelled, and the hook never finishes. So the cancels here are timed to land
    inside each hook.
    """
    ext_stop_entered = asyncio.Event()
    release_ext = asyncio.Event()
    ext_stopped = asyncio.Event()
    disconnect_entered = asyncio.Event()
    release_disconnect = asyncio.Event()
    disconnected = asyncio.Event()

    class SlowExtension(Extension):
        async def stop(self) -> None:
            ext_stop_entered.set()
            await release_ext.wait()
            ext_stopped.set()

    class MonitoredService(CliffracerService):
        ext = SlowExtension()

    cfg = ServiceConfig(
        name="two_cancel_svc", shutdown_timeout=5.0, health_port=0, health_listener=False
    )
    svc = MonitoredService(cfg)
    mock_nc = make_mock_nc()

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        disconnect_entered.set()
        await release_disconnect.wait()
        svc.container.connection.nc = None
        disconnected.set()

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]

    await svc.start()

    drain_entered = asyncio.Event()

    async def stubborn_task() -> None:
        drain_entered.set()
        await asyncio.sleep(10.0)

    svc.container.lifecycle.spawn_supervised_task(stubborn_task(), name="stubborn")

    stop_task = asyncio.create_task(svc.stop())
    await drain_entered.wait()
    await asyncio.sleep(0.01)

    stop_task.cancel()  # during the drain
    await asyncio.wait_for(ext_stop_entered.wait(), timeout=2.0)
    stop_task.cancel()  # while the extension is stopping
    await asyncio.wait_for(disconnect_entered.wait(), timeout=2.0)
    stop_task.cancel()  # while the transport is disconnecting

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    release_ext.set()
    release_disconnect.set()
    await asyncio.wait_for(ext_stopped.wait(), timeout=0.5)
    await asyncio.wait_for(disconnected.wait(), timeout=0.5)
    assert svc.container.connection.nc is None


@pytest.mark.asyncio
async def test_fails_closed_extension_rejects_and_releases_semaphore() -> None:
    """Extensions failing closed reject messages and release semaphore permits under concurrency."""
    auth_down = True
    teardown_invocations = 0

    class StrictAuthExtension(Extension):
        fails_closed = True

        async def worker_setup(self, ctx: WorkerContext) -> None:
            if auth_down:
                raise RuntimeError("Auth server unavailable")

        async def worker_teardown(self, ctx: WorkerContext) -> None:
            nonlocal teardown_invocations
            teardown_invocations += 1

    class AuthedService(CliffracerService):
        auth = StrictAuthExtension()

        @rpc
        async def secure_call(self, val: int) -> JobResult:
            return JobResult(job_id=val, status="authorized")

    cfg = ServiceConfig(
        name="authed_svc", max_rpc_concurrency=3, health_port=0, health_listener=False
    )
    svc = AuthedService(cfg)
    svc.container.discover_handlers()

    total_requests = 30
    messages = [
        make_rpc_message("authed_svc.rpc.secure_call", payload={"val": i})
        for i in range(total_requests)
    ]

    await asyncio.gather(*[svc.container.dispatcher.on_rpc_request(m) for m in messages])

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    # All 30 requests must be rejected
    for m in messages:
        resp = TestResponse.from_mock_message(m)
        assert resp.success is False
        assert "extension auth failed" in str(resp.error)
        assert "refused" not in str(resp.error)
        # What this test is about is the semaphore, but it also has 30 replies
        # in hand: the extension's own exception text is gated, and this config
        # leaves expose_internal_errors at its default.
        assert "Auth server unavailable" not in str(resp.error)

    assert teardown_invocations == total_requests
    assert len(svc.container.lifecycle.active_tasks) == 0

    # Critical: Semaphore must not be leaked! Every permit is back; then allow auth to
    # succeed and verify requests pass
    await assert_every_permit_is_free(svc.container.dispatcher._get_rpc_semaphore(), 3)
    auth_down = False
    new_msg = make_rpc_message("authed_svc.rpc.secure_call", payload={"val": 42})
    await svc.container.dispatcher.on_rpc_request(new_msg)
    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=2.0)

    resp_after = TestResponse.from_mock_message(new_msg)
    assert resp_after.success is True
    assert resp_after.result["status"] == "authorized"


@pytest.mark.asyncio
async def test_dlq_publish_failure_under_concurrency_never_deadlocks() -> None:
    """When every dead-letter publish fails, a burst of malformed events still completes.

    This is the event path: the RPC decode-error path answers with an error
    envelope and never dead-letters, so a failing dead-letter publisher is
    invisible to it. The publisher is replaced where the dispatcher reads it
    (`container._publish_dlq`), and the test requires that it was reached.
    """

    class DlqTestService(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, order_id: int) -> None:
            return None

    cfg = ServiceConfig(
        name="dlq_fail_svc", max_event_concurrency=3, health_port=0, health_listener=False
    )
    svc = DlqTestService(cfg)
    svc.container.discover_handlers()

    svc.container._publish_dlq = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("DLQ broker unreachable")
    )
    callback = svc.container.dispatcher.make_event_callback("orders.created")

    total = 25
    messages = [make_rpc_message("orders.created", raw_data=b"invalid{{{") for _ in range(total)]

    await asyncio.wait_for(asyncio.gather(*[callback(m) for m in messages]), timeout=5.0)

    if svc.container.lifecycle.active_tasks:
        await svc.container.lifecycle.drain_active_tasks(timeout=5.0)

    assert svc.container._publish_dlq.await_count == total
    assert len(svc.container.lifecycle.active_tasks) == 0
    await assert_every_permit_is_free(svc.container.dispatcher._get_event_semaphore(), 3)


# The guard above is set by hand with the lifecycle lock free, so `start()` meets the check that
# precedes the lock without the race it exists for. The real ordering is driven here. (The other
# guard, inside the lock, is held by `test_queued_start_rejected_when_stop_cancels_inflight_start`.)


def _stubbed_service(name: str) -> CliffracerService:
    """A service whose connection is stubbed, so start and stop run with no broker."""
    svc = CliffracerService(ServiceConfig(name=name, health_port=0, health_listener=False))
    mock_nc = make_mock_nc()

    async def fake_connect() -> None:
        svc.container.connection.nc = mock_nc

    async def fake_disconnect() -> None:
        svc.container.connection.nc = None

    svc.container.connection.connect = fake_connect  # type: ignore[method-assign]
    svc.container.connection.disconnect = fake_disconnect  # type: ignore[method-assign]
    return svc


@pytest.mark.asyncio
async def test_a_start_called_while_a_stop_holds_the_lock_is_refused_at_once() -> None:
    """The pre-lock guard: without it `start()` queues behind the lock, and finds the stop
    request already withdrawn when it gets in, because `stop()` withdraws it after releasing."""
    svc = _stubbed_service("start_during_stop_svc")
    await svc.start()

    in_disconnect = asyncio.Event()
    release = asyncio.Event()
    real_disconnect = svc.container.connection.disconnect

    async def slow_disconnect() -> None:
        in_disconnect.set()
        await release.wait()
        await real_disconnect()

    svc.container.connection.disconnect = slow_disconnect  # type: ignore[method-assign]

    stop_task = asyncio.create_task(svc.stop())
    try:
        await asyncio.wait_for(in_disconnect.wait(), timeout=10)
        assert svc.container.lifecycle.lock.locked(), "stop() must be holding the lock"

        start_task = asyncio.create_task(svc.start())
        done, _ = await asyncio.wait({start_task}, timeout=2)
        assert done, "start() queued behind a stop that holds the lock instead of being refused"
        with pytest.raises(ServiceLifecycleError, match="cannot start: stop has been requested"):
            start_task.result()
    finally:
        release.set()
        await asyncio.wait_for(stop_task, timeout=10)

    assert bool(svc._running) is False
    assert bool(svc.container.lifecycle.is_stopped) is True

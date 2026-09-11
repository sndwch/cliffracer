"""Adversarial stress and edge-case verification for task supervision, DLQ, and resilience.

Validates:
1. Task supervision, unhandled exception trapping, zero warning emission, and active task accounting.
2. JetStream heartbeat endurance, long-running handler pulsing, and safe broker error suppression.
3. Concurrency semaphore throttling under flood load for events and async RPCs without permit leakage.
4. DLQ subject formatting and raw publishing without namespace prefixing, bypassing outbound hooks.
5. ResilientMethodProxy coroutine return, HALF_OPEN isolation, and OPEN state fast-fail.
"""

import asyncio
import inspect
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cliffracer_resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    ResilientMethodProxy,
    RpcCircuitOpenError,
)
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, validated_listener
from cliffracer.core.container import _JetStreamHeartbeat
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec


class CustomExplosionError(Exception):
    """Custom exception for error trapping tests."""


class OrderPayload(BaseModel):
    order_id: str
    amount: float


# ============================================================================
# Scenario 1: Task Supervision & Unhandled Exceptions
# ============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_supervised_task_exception_matrix():
    """Supervised background tasks trap diverse exceptions with zero 'never retrieved' warnings."""
    config = ServiceConfig(name="stress_svc")
    svc = CliffracerService(config)

    exception_types = [
        RuntimeError("runtime failure"),
        ValueError("invalid value supplied"),
        KeyError("missing_key"),
        ZeroDivisionError("division by zero"),
        CustomExplosionError("domain error"),
    ]

    async def _failing_work(exc: Exception):
        await asyncio.sleep(0.001)
        raise exc

    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")

        tasks = []
        for i in range(50):
            exc = exception_types[i % len(exception_types)]
            task = svc.container._spawn_supervised_task(
                _failing_work(exc), name=f"failing_task_{i}"
            )
            tasks.append(task)

        # Confirm all are tracked in active tasks
        assert len(svc.container._active_tasks) == 50

        # Wait for all tasks to finish
        await asyncio.gather(*tasks, return_exceptions=True)

        # Confirm all tasks were discarded from active tasks
        assert len(svc.container._active_tasks) == 0

        # Verify that every task's exception was recorded
        for i, task in enumerate(tasks):
            expected_exc = exception_types[i % len(exception_types)]
            assert isinstance(task.exception(), type(expected_exc))

        # Check for unretrieved exception warnings
        unretrieved_warnings = [
            w for w in captured_warnings if "Task exception was never retrieved" in str(w.message)
        ]
        assert len(unretrieved_warnings) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_supervised_task_cancellation():
    """Cancelling supervised tasks leaves no dangling references and does not log errors."""
    config = ServiceConfig(name="stress_svc")
    svc = CliffracerService(config)

    async def _long_sleeping_work():
        await asyncio.sleep(10.0)

    with patch.object(svc.container.logger, "error") as mock_log_error:
        tasks = [
            svc.container._spawn_supervised_task(_long_sleeping_work(), name=f"sleep_{i}")
            for i in range(20)
        ]
        assert len(svc.container._active_tasks) == 20

        # Cancel all tasks
        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        # Discarded cleanly
        assert len(svc.container._active_tasks) == 0
        # No error logged for cancelled tasks
        mock_log_error.assert_not_called()


# ============================================================================
# Scenario 2: JetStream Heartbeat & Long-Running Handlers
# ============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_jetstream_heartbeat_endurance():
    """JetStream heartbeat pulses in-progress repeatedly during handler duration > 3 * ack_wait."""
    # ack_wait = 0.1s -> pulse_interval = max(0.05, 0.05) = 0.05s
    config = ServiceConfig(
        name="heartbeat_svc",
        jetstream_enabled=True,
        jetstream_ack_wait=0.1,
        jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
    )

    class LongRunningService(CliffracerService):
        @listener("events.long", durable="long_durable")
        async def on_long(self, subject: str) -> None:
            # Sleep for 0.35s (> 3 * 0.1s)
            await asyncio.sleep(0.35)

    svc = LongRunningService(config)
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc._discover_handlers()

    msg = AsyncMock()
    msg.subject = "events.long"
    msg.data = b"{}"
    msg.headers = None

    await svc.container._handle_jetstream_event(msg, pattern="events.long")

    # In 0.35s with 0.05s interval, pulses should be at least 5-6
    assert msg.in_progress.await_count >= 5
    # Message should be acknowledged once completed
    assert msg.ack.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_jetstream_heartbeat_broker_disconnect_suppression():
    """Heartbeat suppresses connection errors without interrupting the long-running handler."""
    config = ServiceConfig(
        name="heartbeat_svc",
        jetstream_enabled=True,
        jetstream_ack_wait=0.1,
        jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
    )

    class ResilientService(CliffracerService):
        @listener("events.fragile", durable="fragile_durable")
        async def on_fragile(self, subject: str) -> str:
            await asyncio.sleep(0.25)
            return "finished"

    svc = ResilientService(config)
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc._discover_handlers()

    msg = AsyncMock()
    msg.subject = "events.fragile"
    msg.data = b"{}"
    msg.headers = None

    # Simulate fluctuating network errors during in_progress pulses
    call_count = 0

    async def flaky_in_progress():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        elif call_count == 2:
            raise ConnectionResetError("broker connection reset")
        elif call_count == 3:
            raise TimeoutError("in_progress timeout")
        else:
            raise OSError("network down")

    msg.in_progress.side_effect = flaky_in_progress

    # Must not raise an error, handler should complete
    await svc.container._handle_jetstream_event(msg, pattern="events.fragile")

    assert call_count >= 3
    assert msg.ack.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_jetstream_heartbeat_non_jetstream_message():
    """_JetStreamHeartbeat is a safe no-op on non-JetStream messages lacking in_progress."""
    svc = CliffracerService(ServiceConfig(name="noop_svc"))
    plain_msg = object()  # Has no in_progress attribute

    async with _JetStreamHeartbeat(svc.container, plain_msg, interval=0.05) as hb:
        assert hb._task is None
        await asyncio.sleep(0.01)


# ============================================================================
# Scenario 3: Concurrency Semaphore Throttling
# ============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_event_concurrency_flood_limit():
    """max_event_concurrency=3 strictly bounds concurrent event handlers under 50-message flood."""
    active_count = 0
    max_active_observed = 0
    lock = asyncio.Lock()

    class FloodedEventService(CliffracerService):
        @listener("events.flood", fanout=True)
        async def on_flood(self, subject: str) -> None:
            nonlocal active_count, max_active_observed
            async with lock:
                active_count += 1
                if active_count > max_active_observed:
                    max_active_observed = active_count

            await asyncio.sleep(0.02)

            async with lock:
                active_count -= 1

    config = ServiceConfig(name="flood_svc", max_event_concurrency=3)
    svc = FloodedEventService(config)
    svc._discover_handlers()
    svc._running = True

    cb = svc.container._make_event_callback("events.flood")
    msgs = [AsyncMock(subject="events.flood", data=b"{}", headers=None) for _ in range(50)]

    await asyncio.gather(*[cb(m) for m in msgs])

    # Drain any remaining tasks in container
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active_observed == 3
    assert active_count == 0

    # Ensure semaphore permits are fully returned (value == 3)
    sem = svc.container._get_event_semaphore()
    assert sem is not None
    assert sem._value == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_async_rpc_concurrency_flood_limit():
    """max_async_rpc_concurrency=3 strictly bounds concurrent async RPC handlers under flood."""
    active_count = 0
    max_active_observed = 0
    lock = asyncio.Lock()

    class FloodedAsyncRpcService(CliffracerService):
        @rpc
        async def do_async_task(self) -> None:
            nonlocal active_count, max_active_observed
            async with lock:
                active_count += 1
                if active_count > max_active_observed:
                    max_active_observed = active_count

            await asyncio.sleep(0.02)

            async with lock:
                active_count -= 1

    config = ServiceConfig(name="async_flood_svc", max_async_rpc_concurrency=3)
    svc = FloodedAsyncRpcService(config)
    svc._discover_handlers()
    svc._running = True

    msgs = [
        AsyncMock(subject="async_flood_svc.async.do_async_task", data=b"{}", headers=None)
        for _ in range(50)
    ]

    await asyncio.gather(*[svc.container._on_async_request(m) for m in msgs])

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active_observed == 3
    assert active_count == 0

    sem = svc.container._get_async_rpc_semaphore()
    assert sem is not None
    assert sem._value == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_concurrency_semaphore_leak_under_failures_and_cancellations():
    """Concurrency semaphores never leak permits under mixed successes, failures, and cancellations."""
    active_count = 0
    max_active_observed = 0
    lock = asyncio.Lock()

    class MixedOutcomeService(CliffracerService):
        @listener("events.mixed", fanout=True)
        async def on_mixed(self, subject: str, index: int = 0):
            nonlocal active_count, max_active_observed
            async with lock:
                active_count += 1
                max_active_observed = max(max_active_observed, active_count)

            try:
                await asyncio.sleep(0.01)
                if index % 4 == 1:
                    raise ValueError(f"failure on index {index}")
                elif index % 4 == 2:
                    raise RuntimeError(f"error on index {index}")
            finally:
                async with lock:
                    active_count -= 1

    config = ServiceConfig(name="mixed_svc", max_event_concurrency=4)
    svc = MixedOutcomeService(config)
    svc._discover_handlers()
    svc._running = True

    cb = svc.container._make_event_callback("events.mixed")
    msgs = [
        AsyncMock(
            subject="events.mixed",
            data=f'{{"index": {i}}}'.encode(),
            headers=None,
        )
        for i in range(40)
    ]

    # Dispatch tasks
    dispatch_tasks = [asyncio.create_task(cb(m)) for m in msgs]
    await asyncio.gather(*dispatch_tasks)

    # Cancel a subset of active tasks to simulate aborts
    for i, task in enumerate(list(svc.container._active_tasks)):
        if i % 4 == 3:
            task.cancel()

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks), return_exceptions=True)

    assert max_active_observed <= 4
    assert active_count == 0

    # Ensure all permits are restored to 4
    sem = svc.container._get_event_semaphore()
    assert sem is not None
    assert sem._value == 4


# ============================================================================
# Scenario 4: DLQ Namespacing Decoupling
# ============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_dlq_namespacing_malformed_and_validation():
    """Namespaced service DLQ publishes strictly to unnamespaced dlq.<service>."""
    config = ServiceConfig(
        name="billing",
        namespace="corp_prod",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )

    class BillingService(CliffracerService):
        @validated_listener("events.orders", OrderPayload, durable="orders_durable")
        async def on_order(self, message: OrderPayload):
            pass

    svc = BillingService(config)
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc._discover_handlers()

    # 1. Malformed JSON message
    malformed_msg = AsyncMock()
    malformed_msg.subject = "corp_prod.events.orders"
    malformed_msg.data = b"NOT_VALID_JSON{{"
    malformed_msg.headers = None
    malformed_msg.metadata = MagicMock(num_delivered=1)

    await svc.container._handle_jetstream_event(malformed_msg, pattern="corp_prod.events.orders")

    # Assert published to raw dlq.billing on JetStream, NOT corp_prod.dlq.billing
    svc.js.publish.assert_awaited_once()
    published_subject = svc.js.publish.call_args[0][0]
    assert published_subject == "dlq.billing"
    assert "corp_prod" not in published_subject
    malformed_msg.term.assert_awaited_once()

    # 2. Schema validation error (valid JSON, invalid fields)
    svc.js.publish.reset_mock()
    invalid_schema_msg = AsyncMock()
    invalid_schema_msg.subject = "corp_prod.events.orders"
    invalid_schema_msg.data = b'{"unexpected_field": 123}'
    invalid_schema_msg.headers = None
    invalid_schema_msg.metadata = MagicMock(num_delivered=1)

    await svc.container._handle_jetstream_event(
        invalid_schema_msg, pattern="corp_prod.events.orders"
    )

    svc.js.publish.assert_awaited_once()
    assert svc.js.publish.call_args[0][0] == "dlq.billing"
    invalid_schema_msg.term.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_dlq_outbound_hooks_bypassed_strictly():
    """_publish_dlq never triggers application outbound send hooks."""
    hook_called = False

    async def sample_send_hook(ctx, call):
        nonlocal hook_called
        hook_called = True
        return await call()

    config = ServiceConfig(name="catalog", namespace="ecommerce")
    svc = CliffracerService(config)
    svc.container.nc = AsyncMock()
    svc.container._send_hooks = [sample_send_hook]

    await svc.container._publish_dlq(
        "dlq.catalog",
        payload=b'{"poison": true}',
        headers={"Trace-Id": "123"},
    )

    assert hook_called is False
    svc.container.nc.publish.assert_awaited_once()
    call_subject = svc.container.nc.publish.call_args[0][0]
    assert call_subject == "dlq.catalog"


@pytest.mark.unit
def test_adversarial_dlq_stream_coverage_matrix():
    """Stream coverage assertion verifies unnamespaced DLQ against configured streams."""
    # dlq_subject defaults to 'dlq.{service}'
    # When service has namespace 'prod', stream with 'dlq.*' covers it
    svc = CliffracerService(
        ServiceConfig(
            name="inventory",
            namespace="prod",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ_STREAM", subjects=["dlq.*"])],
        )
    )
    svc.container.js = MagicMock()
    svc.container._assert_dlq_covered()  # Must not raise

    # Stream with 'prod.dlq.*' should FAIL because DLQ is not namespaced
    svc_mismatched = CliffracerService(
        ServiceConfig(
            name="inventory",
            namespace="prod",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="PROD_DLQ", subjects=["prod.dlq.*"])],
        )
    )
    svc_mismatched.container.js = MagicMock()
    with pytest.raises(StreamDeclarationError) as excinfo:
        svc_mismatched.container._assert_dlq_covered()
    assert "dlq.inventory" in str(excinfo.value)


# ============================================================================
# Scenario 5: Resilience Proxy Coroutines & Circuit Breaker
# ============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_resilience_proxy_call_async_coroutine():
    """ResilientMethodProxy.call_async returns an awaitable coroutine with zero warnings."""
    mock_service = MagicMock()
    call_executed = False

    async def mock_call_rpc_no_wait(svc_name, method, namespace=None, **kwargs):
        nonlocal call_executed
        call_executed = True
        return None

    mock_service.call_rpc_no_wait = mock_call_rpc_no_wait

    cb = CircuitBreaker("test_breaker", CircuitBreakerConfig())
    proxy = ResilientMethodProxy(
        mock_service,
        service_name="payment_svc",
        method_name="process_charge",
        namespace="prod",
        circuit_breaker=cb,
    )

    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")

        # 1. Inspect returned object
        coro = proxy.call_async(amount=100.0)
        assert inspect.iscoroutine(coro)
        assert asyncio.iscoroutine(coro)

        # 2. Await the coroutine
        await coro
        assert call_executed is True

        # 3. Verify zero unawaited coroutine warnings
        coro_warnings = [
            w for w in captured_warnings if "was never awaited" in str(w.message).lower()
        ]
        assert len(coro_warnings) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_resilience_proxy_half_open_state_and_probe_isolation():
    """Fire-and-forget call_async preserves HALF_OPEN state and does not consume probe quota."""
    mock_service = MagicMock()

    async def mock_call_rpc_no_wait(svc_name, method, namespace=None, **kwargs):
        return None

    mock_service.call_rpc_no_wait = mock_call_rpc_no_wait

    cb_config = CircuitBreakerConfig(
        failure_threshold=1,
        recovery_timeout=0.01,
        half_open_max_calls=1,
    )
    cb = CircuitBreaker("probe_test_breaker", cb_config)

    proxy = ResilientMethodProxy(
        mock_service,
        service_name="order_svc",
        method_name="place_order",
        circuit_breaker=cb,
    )

    # 1. Trip breaker to OPEN
    await cb.record_failure(RuntimeError("outage"))
    assert cb.state == CircuitState.OPEN

    # When OPEN, call_async must raise RpcCircuitOpenError immediately
    with pytest.raises(RpcCircuitOpenError):
        proxy.call_async(order_id="1")

    # 2. Wait for recovery timeout to transition to HALF_OPEN
    await asyncio.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN

    # 3. In HALF_OPEN, call_async multiple times
    for i in range(5):
        coro = proxy.call_async(order_id=str(i))
        assert inspect.iscoroutine(coro)
        await coro

    # Invariant: fire-and-forget calls MUST NOT alter HALF_OPEN state
    assert cb.state == CircuitState.HALF_OPEN
    # Invariant: fire-and-forget calls MUST NOT consume probe quota
    assert cb._half_open_calls == 0

    # 4. A synchronous monitored call CAN now execute as the 1 allowed probe
    async def mock_call_rpc(svc_name, method, namespace=None, **kwargs):
        return "success"

    mock_service.call_rpc = mock_call_rpc

    # Probe call succeeds -> closes breaker
    result = await proxy(order_id="probe_sync")
    assert result == "success"
    assert cb.state == CircuitState.CLOSED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_max_rpc_concurrency_fallback_for_async_rpc():
    """max_async_rpc_concurrency falls back to max_rpc_concurrency when None."""
    active_count = 0
    max_active_observed = 0
    lock = asyncio.Lock()

    class FallbackService(CliffracerService):
        @rpc
        async def work(self) -> None:
            nonlocal active_count, max_active_observed
            async with lock:
                active_count += 1
                max_active_observed = max(max_active_observed, active_count)
            await asyncio.sleep(0.02)
            async with lock:
                active_count -= 1

    cfg = ServiceConfig(name="fallback_svc", max_rpc_concurrency=2)
    assert cfg.max_async_rpc_concurrency is None

    svc = FallbackService(cfg)
    svc._discover_handlers()
    svc._running = True

    msgs = [
        AsyncMock(subject="fallback_svc.async.work", data=b"{}", headers=None) for _ in range(20)
    ]

    await asyncio.gather(*[svc.container._on_async_request(m) for m in msgs])
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active_observed == 2
    assert active_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_dlq_publish_failure_still_terminates_jetstream_msg():
    """When DLQ publishing fails, JetStream message is still safely terminated."""
    config = ServiceConfig(
        name="term_svc",
        namespace="ns",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )

    class TermService(CliffracerService):
        @listener("events.unroutable", durable="unroutable_dur")
        async def on_event(self, subject: str) -> None:
            pass

    svc = TermService(config)
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc._discover_handlers()
    # Simulate DLQ publish failure (e.g. JetStream publish timeout/error)
    svc.js.publish.side_effect = TimeoutError("DLQ broker timeout")

    msg = AsyncMock()
    msg.subject = "ns.events.unroutable"
    msg.data = b"NOT_JSON"
    msg.headers = None
    msg.metadata = MagicMock(num_delivered=1)

    await svc.container._handle_jetstream_event(msg, pattern="ns.events.unroutable")

    # Message must still be terminated to avoid poison loop
    msg.term.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adversarial_jetstream_max_deliver_deadletter_and_term():
    """When delivery count reaches jetstream_max_deliver, message lands on DLQ and terminates."""
    config = ServiceConfig(
        name="retry_svc",
        namespace="ops",
        jetstream_enabled=True,
        jetstream_max_deliver=3,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["ops.events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )

    class FailingWorker(CliffracerService):
        @listener("events.failing", durable="failing_dur")
        async def on_failing(self, subject: str) -> None:
            raise RuntimeError("irrecoverable database failure")

    svc = FailingWorker(config)
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc._discover_handlers()

    # Case A: num_delivered = 2 (< 3) -> should NAK, not DLQ
    msg_retry = AsyncMock()
    msg_retry.subject = "ops.events.failing"
    msg_retry.data = b"{}"
    msg_retry.headers = None
    msg_retry.metadata = MagicMock(num_delivered=2)

    await svc.container._handle_jetstream_event(msg_retry, pattern="ops.events.failing")
    msg_retry.nak.assert_awaited_once()
    msg_retry.term.assert_not_awaited()
    svc.js.publish.assert_not_awaited()

    # Case B: num_delivered = 3 (== max_deliver) -> should DLQ and TERM
    msg_max = AsyncMock()
    msg_max.subject = "ops.events.failing"
    msg_max.data = b"{}"
    msg_max.headers = None
    msg_max.metadata = MagicMock(num_delivered=3)

    await svc.container._handle_jetstream_event(msg_max, pattern="ops.events.failing")
    msg_max.term.assert_awaited_once()
    svc.js.publish.assert_awaited_once()
    # Verify published to raw unnamespaced DLQ subject
    dlq_subj = svc.js.publish.call_args[0][0]
    assert dlq_subj == "dlq.retry_svc"
    assert "ops" not in dlq_subj


@pytest.mark.unit
def test_adversarial_resilience_method_proxy_weakref_gc():
    """ResilientMethodProxy raises RuntimeError if underlying service was garbage collected."""
    import gc

    class DummyService:
        def call_rpc_no_wait(self, *args, **kwargs):
            pass

    dummy = DummyService()
    cb = CircuitBreaker("gc_breaker", CircuitBreakerConfig())
    proxy = ResilientMethodProxy(
        dummy,
        service_name="temp_svc",
        method_name="do_work",
        circuit_breaker=cb,
    )

    del dummy
    gc.collect()

    with pytest.raises(RuntimeError, match="Service instance was garbage collected"):
        proxy.call_async()

"""Tests for structured task supervision and JetStream acknowledgment coordination."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_supervised_task_retrieves_exception_eliminating_warning():
    """Uncaught exception in supervised task is retrieved and logged without event loop warning."""
    config = ServiceConfig(name="test_svc")
    svc = CliffracerService(config)

    async def _failing_coro():
        raise RuntimeError("simulated task explosion")

    task = svc.container._spawn_supervised_task(_failing_coro(), name="fail_task")
    assert task in svc.container._active_tasks

    # Await completion
    await asyncio.gather(task, return_exceptions=True)

    # Task removed from active tasks
    assert len(svc.container._active_tasks) == 0
    # Exception retrieved; does not raise UnretrievedException warning
    assert isinstance(task.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_guarded_ack_suppresses_connection_closed():
    """Transport drop during msg.ack() is caught, logged, and does not crash background task."""
    config = ServiceConfig(
        name="test_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
    )
    svc = CliffracerService(config)
    svc.nc, svc.js = AsyncMock(), AsyncMock()

    msg = AsyncMock()
    msg.subject = "events.work"
    msg.data = b"{}"
    msg.ack.side_effect = ConnectionResetError("connection reset by peer")

    # Invoking safe ack does not raise
    success = await svc.container._safe_ack(msg)
    assert success is False
    assert msg.ack.await_count == 1


@pytest.mark.asyncio
async def test_guarded_nak_term_and_in_progress_guard_transport_errors():
    """Safe broker methods guard against transport failures."""
    svc = CliffracerService(ServiceConfig(name="test_svc"))
    msg = AsyncMock()
    msg.nak.side_effect = OSError("network unreachable")
    msg.term.side_effect = OSError("network unreachable")
    msg.in_progress.side_effect = OSError("network unreachable")

    assert await svc.container._safe_nak(msg) is False
    assert await svc.container._safe_term(msg) is False
    assert await svc.container._safe_in_progress(msg) is False


@pytest.mark.asyncio
async def test_jetstream_in_progress_pulse_during_long_handler():
    """Handlers exceeding ack_wait / 2 pulse msg.in_progress() to prevent redelivery."""
    config = ServiceConfig(
        name="test_svc",
        jetstream_enabled=True,
        jetstream_ack_wait=0.2,  # pulse_interval = max(0.05, 0.1) = 0.1s
        jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
    )

    class SlowWorker(CliffracerService):
        @listener("events.slow", durable="slow_durable")
        async def on_slow(self, subject: str) -> None:
            await asyncio.sleep(0.25)

    svc = SlowWorker(config)
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc._discover_handlers()

    msg = AsyncMock()
    msg.subject = "events.slow"
    msg.data = b"{}"
    msg.headers = None

    # Run handler
    await svc.container._handle_jetstream_event(msg, pattern="events.slow")

    # Assert in_progress was pulsed at least once
    assert msg.in_progress.await_count >= 1
    assert msg.ack.await_count == 1


@pytest.mark.asyncio
async def test_bounded_event_concurrency():
    """max_event_concurrency restricts simultaneous event executions."""
    active = 0
    max_active = 0

    class EventService(CliffracerService):
        @listener("events.work", fanout=True)
        async def on_event(self, subject: str) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    config = ServiceConfig(name="evt_svc", max_event_concurrency=2)
    svc = EventService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    cb = svc.container._make_event_callback("events.work")
    msgs = [AsyncMock(subject="events.work", data=b"{}", headers=None) for _ in range(6)]

    await asyncio.gather(*[cb(m) for m in msgs])
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active == 2


@pytest.mark.asyncio
async def test_bounded_async_rpc_concurrency():
    """max_async_rpc_concurrency restricts simultaneous fire-and-forget RPC executions."""
    active = 0
    max_active = 0

    class AsyncRpcService(CliffracerService):
        @rpc
        async def do_work(self) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    config = ServiceConfig(name="async_svc", max_async_rpc_concurrency=2)
    svc = AsyncRpcService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    msgs = [
        AsyncMock(subject="async_svc.async.do_work", data=b"{}", headers=None) for _ in range(6)
    ]
    await asyncio.gather(*[svc.container._on_async_request(m) for m in msgs])
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active == 2

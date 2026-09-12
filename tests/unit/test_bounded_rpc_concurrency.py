"""Unit tests for bounded RPC concurrency and shutdown deadline."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


def _rpc_msg(subject: str = "svc.rpc.work", data: dict | None = None):
    msg = AsyncMock()
    msg.subject = subject
    msg.reply = "reply.123"
    msg.data = json.dumps(data or {}).encode()
    msg.headers = None
    return msg


@pytest.mark.asyncio
async def test_max_rpc_concurrency_bounds_in_flight_handlers():
    """max_rpc_concurrency limits simultaneous in-flight handler executions."""
    current_active = 0
    max_active = 0

    class WorkerService(CliffracerService):
        @rpc
        async def work(self) -> str:
            nonlocal current_active, max_active
            current_active += 1
            max_active = max(max_active, current_active)
            await asyncio.sleep(0.05)
            current_active -= 1
            return "ok"

    config = ServiceConfig(name="bounded_svc", max_rpc_concurrency=2)
    svc = WorkerService(config)
    svc._discover_handlers()
    svc._running = True

    # Launch 6 concurrent requests
    msgs = [_rpc_msg("bounded_svc.rpc.work") for _ in range(6)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)

    # Wait for all background tasks in container to complete
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active == 2
    for m in msgs:
        assert m.respond.await_count == 1
        reply = json.loads(m.respond.call_args.args[0].decode())
        assert reply["success"] is True
        assert reply["result"] == "ok"


@pytest.mark.asyncio
async def test_unbounded_concurrency_by_default():
    """When max_rpc_concurrency is None, requests run concurrently without limit."""
    current_active = 0
    max_active = 0

    class WorkerService(CliffracerService):
        @rpc
        async def work(self) -> str:
            nonlocal current_active, max_active
            current_active += 1
            max_active = max(max_active, current_active)
            await asyncio.sleep(0.05)
            current_active -= 1
            return "ok"

    config = ServiceConfig(name="unbounded_svc", max_rpc_concurrency=None)
    svc = WorkerService(config)
    svc._discover_handlers()
    svc._running = True

    msgs = [_rpc_msg("unbounded_svc.rpc.work") for _ in range(5)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_active == 5


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_hanging_tasks():
    """Service shutdown terminates hanging in-flight tasks when shutdown_timeout expires."""
    cancelled = False

    class SlowService(CliffracerService):
        @rpc
        async def slow_work(self) -> str:
            nonlocal cancelled
            try:
                await asyncio.sleep(10.0)
                return "completed"
            except asyncio.CancelledError:
                cancelled = True
                raise

    config = ServiceConfig(name="timeout_svc", shutdown_timeout=0.1)
    svc = SlowService(config)
    svc._discover_handlers()
    svc._running = True

    # Start a slow in-flight task
    msg = _rpc_msg("timeout_svc.rpc.slow_work")
    await svc.container._on_rpc_request(msg)
    assert len(svc.container._active_tasks) == 1

    start_time = time.time()
    await svc.stop()
    elapsed = time.time() - start_time

    # Shutdown should have taken ~0.1s, well under 2.0s
    assert elapsed < 2.0
    assert cancelled is True
    assert len(svc.container._active_tasks) == 0


@pytest.mark.asyncio
async def test_shutdown_allows_tasks_to_finish_if_within_deadline():
    """Service shutdown waits for tasks to finish normally if they finish before shutdown_timeout."""
    completed = False

    class QuickService(CliffracerService):
        @rpc
        async def quick_work(self) -> str:
            nonlocal completed
            await asyncio.sleep(0.05)
            completed = True
            return "done"

    config = ServiceConfig(name="quick_svc", shutdown_timeout=1.0)
    svc = QuickService(config)
    svc._discover_handlers()
    svc._running = True

    msg = _rpc_msg("quick_svc.rpc.quick_work")
    await svc.container._on_rpc_request(msg)
    assert len(svc.container._active_tasks) == 1

    await svc.stop()
    assert completed is True
    assert len(svc.container._active_tasks) == 0

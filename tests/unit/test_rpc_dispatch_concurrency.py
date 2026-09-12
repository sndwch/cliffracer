"""Unit tests for concurrent RPC dispatch.

Verifies that incoming RPC requests are dispatched asynchronously using
asyncio.create_task, preventing head-of-line blocking in NATS subscription
callbacks.
"""

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


class _MockMsg:
    """Mock NATS message simulating RPC request-reply envelope."""

    def __init__(self, subject: str, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers: dict[str, str] = {}
        self.response: dict[str, Any] | None = None

    async def respond(self, payload: bytes) -> None:
        self.response = json.loads(payload.decode())


class _ConcurrentService(CliffracerService):
    """Test service with slow and barrier-synchronized RPC methods."""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.active_count = 0
        self.peak_concurrency = 0
        self.barrier: asyncio.Barrier | None = None

    @rpc
    async def slow_work(self, task_id: str, delay: float) -> str:
        self.active_count += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active_count)
        try:
            await asyncio.sleep(delay)
            return f"done_{task_id}"
        finally:
            self.active_count -= 1

    @rpc
    async def barrier_work(self, task_id: str) -> str:
        self.active_count += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active_count)
        try:
            if self.barrier:
                await self.barrier.wait()
            return f"done_{task_id}"
        finally:
            self.active_count -= 1


@pytest.fixture
async def svc():
    service = _ConcurrentService(ServiceConfig(name="concurrency_svc"))
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


def test_active_tasks_property_delegation(svc):
    """svc.container._active_tasks holds active tasks; delegation removed from service."""
    assert isinstance(svc.container._active_tasks, set)
    assert len(svc.container._active_tasks) == 0
    assert not hasattr(svc, "_active_tasks")


@pytest.mark.asyncio
async def test_setup_subscriptions_binds_on_rpc_request():
    """_setup_subscriptions binds cb=self.dispatcher.on_rpc_request for RPC."""
    service = _ConcurrentService(ServiceConfig(name="test_bind_svc"))
    await service.container._setup_extensions()
    service._discover_handlers()
    service.nc = AsyncMock()
    service.container.lifecycle._running = False

    await service.container._setup_subscriptions()

    # Find the RPC subscription call
    rpc_calls = [
        c for c in service.nc.subscribe.call_args_list if c.args[0] == "test_bind_svc.rpc.*"
    ]
    assert len(rpc_calls) == 1
    rpc_call = rpc_calls[0]
    assert rpc_call.kwargs["cb"] == service.container.dispatcher.on_rpc_request
    assert rpc_call.kwargs["queue"] == "test_bind_svc.rpc"


@pytest.mark.asyncio
async def test_rpc_dispatch_concurrency_via_barrier(svc):
    """Multiple concurrent RPC requests execute in parallel, proving no HOL blocking.

    With serial dispatch, 5 calls waiting on an asyncio.Barrier(5) would deadlock
    because the first call would block the subscription loop and subsequent calls
    would never start. With concurrent dispatch via asyncio.create_task, all 5 calls
    enter their handlers simultaneously and satisfy the barrier.
    """
    call_count = 5
    svc.barrier = asyncio.Barrier(call_count)
    msgs = [
        _MockMsg("concurrency_svc.rpc.barrier_work", {"task_id": f"t_{i}"})
        for i in range(call_count)
    ]

    # Dispatch all messages via _on_rpc_request (simulating NATS callback loop)
    for msg in msgs:
        await svc.container._on_rpc_request(msg)

    # All 5 tasks are active simultaneously in _active_tasks
    assert len(svc.container._active_tasks) == call_count

    # Wait for all active tasks to complete
    await asyncio.gather(*list(svc.container._active_tasks))

    # Concurrency checks
    assert svc.peak_concurrency == call_count
    assert len(svc.container._active_tasks) == 0

    for i, msg in enumerate(msgs):
        assert msg.response is not None
        assert msg.response["success"] is True
        assert msg.response["result"] == f"done_t_{i}"


@pytest.mark.asyncio
async def test_rpc_dispatch_concurrency_timing(svc):
    """5 concurrent calls with 0.05s sleep take ~0.05s (< 0.15s), not ~0.25s."""
    call_count = 5
    sleep_delay = 0.05
    msgs = [
        _MockMsg("concurrency_svc.rpc.slow_work", {"task_id": f"t_{i}", "delay": sleep_delay})
        for i in range(call_count)
    ]

    start_time = time.monotonic()

    for msg in msgs:
        await svc.container._on_rpc_request(msg)

    # Await all tasks dispatched to container._active_tasks
    await asyncio.gather(*list(svc.container._active_tasks))

    elapsed = time.monotonic() - start_time

    # Serial execution would take at least call_count * sleep_delay (0.25s)
    # Concurrent execution should take ~0.05s; give generous headroom < 0.20s
    assert elapsed < 0.20, f"Expected concurrent dispatch (< 0.20s), took {elapsed:.3f}s"
    assert svc.peak_concurrency == call_count

    for i, msg in enumerate(msgs):
        assert msg.response is not None
        assert msg.response["success"] is True
        assert msg.response["result"] == f"done_t_{i}"


@pytest.mark.asyncio
async def test_active_tasks_discard_on_done(svc):
    """Completed tasks are automatically discarded from _active_tasks."""
    msg = _MockMsg("concurrency_svc.rpc.slow_work", {"task_id": "single", "delay": 0.01})
    await svc.container._on_rpc_request(msg)

    assert len(svc.container._active_tasks) == 1
    task = next(iter(svc.container._active_tasks))
    await task

    # Give loop a cycle for done_callback
    await asyncio.sleep(0)

    assert len(svc.container._active_tasks) == 0
    assert msg.response is not None
    assert msg.response["success"] is True

"""Unit tests for concurrent RPC dispatch.

Verifies that incoming RPC requests are dispatched asynchronously using
asyncio.create_task, preventing head-of-line blocking in NATS subscription
callbacks.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class _MockMsg:
    """Mock NATS message simulating RPC request-reply envelope."""

    #: Every dispatcher path reads this; a double without one let a
    #: reply be recorded that production would have refused.
    reply: str | None = "_INBOX.test"

    def __init__(self, subject: str, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers: dict[str, str] = {}
        self.response: dict[str, Any] | None = None

    async def respond(self, payload: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
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
    """A fresh service's container has an empty `_active_tasks`, and the service itself carries no
    such attribute: the delegation to the service was removed. That the set tracks tasks is read
    by the next test, which spawns one."""
    assert isinstance(svc.container._active_tasks, frozenset)
    assert len(svc.container._active_tasks) == 0
    assert not hasattr(svc, "_active_tasks")


async def test_a_supervised_task_is_tracked_while_it_runs_and_dropped_when_it_ends(svc):
    """The half the test above does not exercise: `_active_tasks` holds a spawned task for as long
    as it runs and releases it when it finishes."""
    release = asyncio.Event()

    async def work() -> str:
        await release.wait()
        return "done"

    task = svc.container.lifecycle.spawn_supervised_task(work(), name="tracked")

    assert task in svc.container._active_tasks
    assert len(svc.container._active_tasks) == 1

    release.set()
    assert await task == "done"
    await asyncio.sleep(0)  # the done-callback that releases it runs on the next iteration

    assert task not in svc.container._active_tasks
    assert len(svc.container._active_tasks) == 0


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
async def test_slow_calls_occupy_the_handler_together(svc):
    """Five calls into a sleeping handler are inside it at once, not one after another."""
    call_count = 5
    sleep_delay = 0.05
    msgs = [
        _MockMsg("concurrency_svc.rpc.slow_work", {"task_id": f"t_{i}", "delay": sleep_delay})
        for i in range(call_count)
    ]

    for msg in msgs:
        await svc.container._on_rpc_request(msg)

    # Await all tasks dispatched to container._active_tasks
    await asyncio.gather(*list(svc.container._active_tasks))

    # The handler counts how many callers are inside it together, so serial
    # dispatch reads as a peak of one however fast the host runs them.
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

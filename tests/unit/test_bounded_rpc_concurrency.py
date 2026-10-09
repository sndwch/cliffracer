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


def _record_drain_deadlines(svc: CliffracerService) -> list[float | None]:
    """Record the deadline `stop()` hands to the task drain, and still run the drain."""
    lifecycle = svc.container.lifecycle
    deadlines: list[float | None] = []
    real_drain = lifecycle.drain_active_tasks

    async def recording_drain(timeout: float | None = 30.0) -> None:
        deadlines.append(timeout)
        await real_drain(timeout=timeout)

    lifecycle.drain_active_tasks = recording_drain  # type: ignore[method-assign]
    return deadlines


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
@pytest.mark.parametrize("shutdown_timeout", [0.1, 0.5])
async def test_shutdown_timeout_cancels_hanging_tasks(shutdown_timeout):
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

    config = ServiceConfig(name="timeout_svc", shutdown_timeout=shutdown_timeout)
    svc = SlowService(config)
    svc._discover_handlers()
    svc._running = True

    # Start a slow in-flight task
    msg = _rpc_msg("timeout_svc.rpc.slow_work")
    await svc.container._on_rpc_request(msg)
    assert len(svc.container._active_tasks) == 1

    deadlines = _record_drain_deadlines(svc)
    started = time.monotonic()
    await svc.stop()
    elapsed = time.monotonic() - started

    # The drain was given the configured deadline, not some other. This is read
    # from the call rather than timed: a ceiling on `elapsed` would have to
    # allow for a slow host, and a fixed deadline several times too long would
    # sit inside any such allowance.
    assert deadlines == [shutdown_timeout]
    # And it was waited out before the handler was cancelled. A floor cannot
    # flake: a slow host only lengthens it.
    assert elapsed >= shutdown_timeout * 0.9, f"cancelled after {elapsed:.3f}s"

    # The handler sleeps 10s and returns "completed" if it is left alone, so
    # reaching its except branch is what says shutdown cancelled it rather
    # than waiting it out.
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

    deadlines = _record_drain_deadlines(svc)
    await svc.stop()
    assert deadlines == [1.0], "the drain must be given the configured deadline"
    assert completed is True
    assert len(svc.container._active_tasks) == 0

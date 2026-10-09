"""A supervised task that calls `stop()` first (a "shutdown" RPC) is not waited for by that stop.

The drain waits for every supervised task, and the task running the stop is one of them: the stop
waited `shutdown_timeout` for the task that was waiting for the stop, then cancelled it, so the
handler never finished and its caller never got a reply. A stop that is already running was
handled (`test_a_stop_called_from_inside_a_stop_...`); this is the first caller.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cliffracer.core.lifecycle import LifecycleHooks, LifecycleManager
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

NAMES = [
    "setup_extensions",
    "connect",
    "ensure_streams",
    "on_startup",
    "start_extensions",
    "start_health_listener",
    "start_timers",
    "setup_subscriptions",
    "stop_timers",
    "stop_health_listener",
    "cancel_subscriptions",
    "on_shutdown",
    "stop_extensions",
    "disconnect",
]


def _manager(shutdown_timeout: float) -> tuple[LifecycleManager, list[str]]:
    calls: list[str] = []

    def hook(name):
        async def run():
            calls.append(name)

        return run

    hooks = LifecycleHooks(
        discover_handlers=lambda: calls.append("discover_handlers"),
        validate_dlq=lambda: calls.append("validate_dlq"),
        is_jetstream_active=lambda: False,
        **{name: hook(name) for name in NAMES},
    )
    return (
        LifecycleManager(
            ServiceConfig(name="svc", health_listener=False, shutdown_timeout=shutdown_timeout),
            hooks,
        ),
        calls,
    )


def _supervise(manager: LifecycleManager, coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    manager._active_tasks.add(task)
    task.add_done_callback(manager._active_tasks.discard)
    return task


async def test_the_first_caller_finishes_after_stop_returns_instead_of_being_cancelled():
    manager, calls = _manager(shutdown_timeout=5.0)
    await manager.start()
    outcome: list[str] = []

    async def shutdown_handler():
        await manager.stop()
        outcome.append("replied")

    handler = _supervise(manager, shutdown_handler())
    started = time.monotonic()

    await asyncio.wait_for(handler, timeout=2)

    assert outcome == ["replied"], "the handler was cancelled by the stop it was running"
    # Upper bound. CI p99 0.000263 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 3795x
    # p99.
    assert time.monotonic() - started < 1.0, "the stop waited out the shutdown timeout for itself"
    assert "on_shutdown" in calls and "disconnect" in calls, calls
    assert not manager._stoppers, "a finished stop left its caller marked as a stopper"


async def test_another_supervised_task_is_still_drained_and_cancelled_without_touching_the_stopper():
    manager, _ = _manager(shutdown_timeout=0.3)
    await manager.start()
    gate = asyncio.Event()
    cancelled: list[bool] = []

    async def other():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def shutdown_handler():
        await manager.stop()

    straggler = _supervise(manager, other())
    handler = _supervise(manager, shutdown_handler())

    await asyncio.wait({handler, straggler}, timeout=5)

    assert cancelled == [True], "the stop must still cancel work that outlives the timeout"
    assert straggler.cancelled()
    assert not handler.cancelled(), (
        "the cancellation of stragglers reached the task running the stop"
    )


async def test_CONTROL_a_stop_from_outside_any_task_the_service_supervises_is_unchanged():
    """It drains a supervised task for the shutdown timeout, then cancels it."""
    manager, calls = _manager(shutdown_timeout=0.3)
    await manager.start()
    gate = asyncio.Event()
    cancelled: list[bool] = []

    async def other():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    straggler = _supervise(manager, other())
    await asyncio.sleep(0)
    started = time.monotonic()

    await asyncio.wait_for(manager.stop(), timeout=5)

    elapsed = time.monotonic() - started
    assert "disconnect" in calls and manager.is_stopped
    assert cancelled == [True] and straggler.cancelled(), (
        "the outside stop left a straggler running"
    )
    # Lower bound: the 0.3 s drain less slack; a stop that cancelled at once falls under it. Load
    # can only lengthen it.
    assert elapsed >= 0.25, f"the outside stop did not wait for the drain ({elapsed:.2f}s)"

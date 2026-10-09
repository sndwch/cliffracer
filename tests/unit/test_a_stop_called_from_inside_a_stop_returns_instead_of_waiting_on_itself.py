"""`stop()` called while a stop is already running on the same task, or from a task that stop is
waiting for, returns instead of waiting for itself.

The lifecycle lock is an `asyncio.Lock`, which is not reentrant, though the property called it
"the reentrant lifecycle mutex". `stop()` holds it while it runs the teardown hooks, so a hook that
called `stop()` again (the natural "make shutdown idempotent" mistake, and the shape a
closed-connection callback has) reached `async with self.lock` and waited for the lock it was
holding: forever, because nothing times it out. The same cycle ran through a supervised task that
called `stop()` while the outer stop was draining that very task, which broke only when the
shutdown timeout killed the task.

A stop that is already underway is the stop being asked for, so the nested call has nothing to do.
A stop from any OTHER task still waits its turn and finds the work done, as before.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cliffracer.core.lifecycle import LifecycleHooks, LifecycleManager
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

TEARDOWN = [
    "stop_timers",
    "stop_health_listener",
    "cancel_subscriptions",
    "on_shutdown",
    "stop_extensions",
    "disconnect",
]


def _manager(*, nested_in: str | None = None, shutdown_timeout: float = 30.0, block_in=None):
    """A started-able manager over recording hooks. `nested_in` names the teardown hook that
    calls `stop()` again; `block_in` is an event that hook waits for before returning."""
    calls: list[str] = []
    holder: dict[str, LifecycleManager] = {}

    def recording(name):
        async def hook():
            calls.append(name)
            if name == nested_in:
                await holder["manager"].stop()
            if block_in is not None and name == "on_shutdown":
                await block_in.wait()

        return hook

    def sync(name):
        return lambda: calls.append(name)

    hooks = LifecycleHooks(
        setup_extensions=recording("setup_extensions"),
        discover_handlers=sync("discover_handlers"),
        connect=recording("connect"),
        ensure_streams=recording("ensure_streams"),
        validate_dlq=sync("validate_dlq"),
        is_jetstream_active=lambda: False,
        on_startup=recording("on_startup"),
        start_extensions=recording("start_extensions"),
        start_health_listener=recording("start_health_listener"),
        start_timers=recording("start_timers"),
        setup_subscriptions=recording("setup_subscriptions"),
        stop_timers=recording("stop_timers"),
        stop_health_listener=recording("stop_health_listener"),
        cancel_subscriptions=recording("cancel_subscriptions"),
        on_shutdown=recording("on_shutdown"),
        stop_extensions=recording("stop_extensions"),
        disconnect=recording("disconnect"),
    )
    manager = LifecycleManager(
        ServiceConfig(name="svc", health_listener=False, shutdown_timeout=shutdown_timeout), hooks
    )
    holder["manager"] = manager
    return manager, calls


@pytest.mark.parametrize("hook", ["on_shutdown", "stop_extensions", "cancel_subscriptions"])
async def test_a_teardown_hook_that_calls_stop_does_not_wait_on_the_stop_it_is_part_of(hook):
    manager, calls = _manager(nested_in=hook)
    await manager.start()

    await asyncio.wait_for(manager.stop(), timeout=3)

    teardown = [c for c in calls if c in TEARDOWN]
    assert teardown == TEARDOWN, teardown
    assert manager.is_stopped and not manager._running


async def test_the_nested_stop_returns_to_its_caller_and_the_outer_one_finishes_the_work():
    nested: list[str] = []
    manager, calls = _manager()
    original = manager.hooks.on_shutdown

    async def on_shutdown():
        await original()
        started = time.monotonic()
        await manager.stop()
        nested.append(f"returned after {time.monotonic() - started:.2f}s")

    manager.hooks.on_shutdown = on_shutdown
    await manager.start()

    await asyncio.wait_for(manager.stop(), timeout=3)

    # Upper bound. CI p99 0 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); p99 inside the 0
    # s designed window.
    assert nested and float(nested[0].split()[2].rstrip("s")) < 0.5, nested
    assert calls.count("stop_extensions") == 1 and calls.count("disconnect") == 1


async def test_a_supervised_task_that_calls_stop_while_it_is_being_drained_does_not_wait_for_the_timeout():
    manager, _ = _manager(shutdown_timeout=3.0)
    await manager.start()
    inside = asyncio.Event()
    finished: list[float] = []

    async def handler():
        inside.set()
        await asyncio.sleep(0.05)
        await manager.stop()
        finished.append(time.monotonic())

    task = asyncio.create_task(handler())
    manager._active_tasks.add(task)
    await inside.wait()

    started = time.monotonic()
    await asyncio.wait_for(manager.stop(), timeout=10)

    assert finished, "the handler was cancelled by the shutdown timeout instead of finishing"
    # Upper bound. CI p99 0.0519 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.05
    # s, 774x the overshoot.
    assert time.monotonic() - started < 1.5, time.monotonic() - started


async def test_CONTROL_a_stop_from_another_task_waits_for_the_running_one_and_does_nothing_more():
    gate = asyncio.Event()
    manager, calls = _manager(block_in=gate)
    await manager.start()

    first = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.05)
    second = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.05)
    assert not second.done(), "the second stop must wait for the first, as before"

    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=3)

    assert [c for c in calls if c in TEARDOWN] == TEARDOWN


async def test_CONTROL_a_stop_on_a_manager_that_is_not_stopping_runs_the_teardown():
    manager, calls = _manager()
    await manager.start()

    await manager.stop()

    assert [c for c in calls if c in TEARDOWN] == TEARDOWN


async def test_CONTROL_a_manager_that_is_started_again_is_torn_down_again():
    """The mark lives only as long as the teardown: a stop on the same task afterwards runs."""
    manager, calls = _manager()

    await manager.start()
    await manager.stop()
    await manager.start()
    await manager.stop()

    assert calls.count("disconnect") == 2, calls


async def test_CONTROL_one_managers_teardown_does_not_silence_stop_on_another_manager():
    other, other_calls = _manager()
    await other.start()
    manager, _ = _manager()
    original = manager.hooks.on_shutdown

    async def on_shutdown():
        await original()
        await other.stop()

    manager.hooks.on_shutdown = on_shutdown
    await manager.start()

    await asyncio.wait_for(manager.stop(), timeout=3)

    assert [c for c in other_calls if c in TEARDOWN] == TEARDOWN


async def test_CONTROL_a_supervised_task_that_asks_first_still_gets_a_real_stop():
    """The exemption is for a stop that is already running. With none running, the call is the
    stop: the whole teardown runs, `on_shutdown` included, and the task is not drained."""
    manager, calls = _manager(shutdown_timeout=3.0)
    await manager.start()
    asked = asyncio.Event()
    outcome: list[str] = []

    async def handler():
        asked.set()
        await manager.stop()
        outcome.append("returned")

    task = asyncio.create_task(handler())
    manager._active_tasks.add(task)
    task.add_done_callback(manager._active_tasks.discard)
    await asked.wait()
    started = time.monotonic()
    await asyncio.wait_for(task, timeout=2)

    assert outcome == ["returned"], "the handler was cancelled instead of finishing its stop"
    assert [c for c in calls if c in TEARDOWN] == TEARDOWN, calls
    # Upper bound. CI p99 0.000345 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 2901x
    # p99.
    assert time.monotonic() - started < 1.0, "the stop waited for the task that was running it"


async def test_a_task_the_teardown_created_does_not_carry_the_mark_into_a_later_stop():
    """A task created inside the teardown inherits its context for life. After a restart, a stop
    from that task is a real stop, not a nested one: the mark alone must not decide."""
    manager, calls = _manager()
    release = asyncio.Event()
    survivors: list[asyncio.Task] = []
    original = manager.hooks.on_shutdown

    async def on_shutdown():
        await original()

        async def later_stopper():
            await release.wait()
            await manager.stop()

        survivors.append(asyncio.create_task(later_stopper()))

    manager.hooks.on_shutdown = on_shutdown
    await manager.start()
    await manager.stop()
    await manager.start()
    assert manager._running

    release.set()
    await asyncio.wait_for(survivors[0], timeout=3)

    assert calls.count("disconnect") == 2, calls
    assert not manager._running
    assert manager.is_stopped


async def test_a_hook_of_the_abortive_startup_cleanup_that_calls_stop_returns_at_once():
    """The abortive cleanup is a teardown too, in a task of its own, with `start()` holding the
    lock: a stop from inside it must not wait for that lock."""
    calls: list[str] = []
    holder: dict[str, LifecycleManager] = {}

    manager, _ = _manager()

    async def failing_on_startup():
        raise RuntimeError("startup failed")

    async def stop_extensions():
        calls.append("stop_extensions")
        await holder["manager"].stop()

    manager.hooks.on_startup = failing_on_startup
    manager.hooks.stop_extensions = stop_extensions
    holder["manager"] = manager

    with pytest.raises(RuntimeError, match="startup failed"):
        await asyncio.wait_for(manager.start(), timeout=3)

    assert calls == ["stop_extensions"]


async def test_a_context_that_once_ran_a_teardown_waits_for_a_later_teardown_it_is_not_part_of():
    """The mark ends with the teardown that set it: a task created afterwards, from a context that
    once ran one, must wait for somebody else's running stop like any other task."""
    gate = asyncio.Event()
    manager, calls = _manager(block_in=gate)
    await manager.start()
    gate.set()
    await manager.stop()  # this task ran a teardown: its context must not keep the mark
    gate.clear()
    await manager.start()

    blocked = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.05)
    bystander = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.1)
    still_waiting = not bystander.done()

    gate.set()
    await asyncio.wait_for(asyncio.gather(blocked, bystander), timeout=3)

    assert still_waiting, "a stop from outside the running teardown returned without waiting"
    assert calls.count("disconnect") == 2


async def test_a_teardown_marked_for_one_manager_does_not_make_another_managers_stop_a_no_op():
    gate = asyncio.Event()
    other, other_calls = _manager(block_in=gate)
    await other.start()
    blocked = asyncio.create_task(other.stop())  # the other manager's teardown is under way
    await asyncio.sleep(0.05)

    manager, _ = _manager()
    waited: list[bool] = []
    original = manager.hooks.on_shutdown

    async def on_shutdown():
        await original()
        waiter = asyncio.ensure_future(other.stop())
        await asyncio.sleep(0.1)
        waited.append(not waiter.done())
        gate.set()
        await waiter

    manager.hooks.on_shutdown = on_shutdown
    await manager.start()

    await asyncio.wait_for(manager.stop(), timeout=3)
    await blocked

    assert waited == [True], "the other manager's stop returned without waiting for its teardown"
    assert [c for c in other_calls if c in TEARDOWN] == TEARDOWN

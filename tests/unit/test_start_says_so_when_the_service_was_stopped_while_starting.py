"""`start()` does not return normally for a service that was stopped while it was starting.

A stop from inside the startup (an `on_startup` that stops its own service) used to make `start()`
return as if the service were up, while a stop from another task made it raise `CancelledError`:
one end state, reported two ways. Now neither returns. The same-task case raises
`ServiceLifecycleError`; the other-task case is the cancellation that stop inflicted, left alone,
because a caller that cancels `start()` on purpose (a startup timeout) must see its cancellation.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.extension import Extension
from cliffracer.core.lifecycle import LifecycleHooks, LifecycleManager
from cliffracer.runners.orchestrator import ServiceRunner
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit


def _service_class(calls: list[str], *, stop_in: str | None = None, on_startup_waits=None):
    """A service with no broker: its lifecycle calls go to `calls`, and `stop_in` names the step
    that stops the service from inside its own startup."""

    class Recording(Extension):
        async def setup(self, ctx) -> None:
            calls.append("ext.setup")
            if stop_in == "ext.setup":
                await self.service.stop()

        async def start(self) -> None:
            calls.append("ext.start")
            if stop_in == "ext.start":
                await self.service.stop()

        async def stop(self) -> None:
            calls.append("ext.stop")

    class Svc(ServicePhases, CliffracerService):
        recorded = Recording()

        async def connect(self) -> None:
            calls.append("connect")

        async def disconnect(self) -> None:
            calls.append("disconnect")

        async def _setup_subscriptions(self) -> None:
            calls.append("subscriptions")

        async def on_startup(self) -> None:
            calls.append("on_startup")
            if stop_in == "on_startup":
                await self.stop()
            if on_startup_waits is not None:
                await on_startup_waits()

        async def on_shutdown(self) -> None:
            calls.append("on_shutdown")

    return Svc


def _config(**overrides) -> ServiceConfig:
    return ServiceConfig(name="svc", health_port=0, **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["ext.setup", "on_startup", "ext.start"])
async def test_a_stop_from_inside_the_startup_makes_start_raise_and_names_the_service(step):
    calls: list[str] = []
    svc = _service_class(calls, stop_in=step)(_config())

    with pytest.raises(ServiceLifecycleError) as caught:
        await svc.start()

    assert "Service 'svc' was stopped while it was starting" in str(caught.value)
    assert svc.container.lifecycle.is_stopped
    assert not svc.container.lifecycle._running
    assert "subscriptions" not in calls, "the startup went on after the stop"


@pytest.mark.asyncio
async def test_the_calls_a_same_task_stop_makes_are_unchanged_and_run_once():
    # on_shutdown is absent on purpose: the service never finished starting, as before this change.
    calls: list[str] = []
    svc = _service_class(calls, stop_in="on_startup")(_config())

    with pytest.raises(ServiceLifecycleError):
        await svc.start()

    assert calls == [
        "ext.setup",
        "connect",
        "on_startup",
        "ext.stop",
        "disconnect",
    ], calls


@pytest.mark.asyncio
async def test_CONTROL_a_stop_from_another_task_still_cancels_start():
    calls: list[str] = []
    reached = asyncio.Event()

    async def wait_forever() -> None:
        reached.set()
        await asyncio.Event().wait()

    svc = _service_class(calls, on_startup_waits=wait_forever)(_config())
    starting = asyncio.create_task(svc.start())
    await reached.wait()

    await svc.stop()

    with pytest.raises(asyncio.CancelledError):
        await starting
    assert svc.container.lifecycle.is_stopped


@pytest.mark.asyncio
async def test_CONTROL_a_stop_still_in_flight_when_startup_notices_it_does_not_make_start_raise():
    """An `on_startup` that swallows the cancellation lets `start()` reach a checkpoint while the
    other task's stop is still running. That stop finishes the teardown, so `start()` returns as
    before and does not race it with an abortive cleanup of its own."""
    calls: list[str] = []
    reached = asyncio.Event()

    async def swallow_the_cancel() -> None:
        reached.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    svc = _service_class(calls, on_startup_waits=swallow_the_cancel)(_config())
    starting = asyncio.create_task(svc.start())
    await reached.wait()

    await svc.stop()

    assert await starting is None
    assert svc.container.lifecycle.is_stopped
    assert calls.count("ext.stop") == 1 and calls.count("disconnect") == 1, calls


@pytest.mark.asyncio
async def test_CONTROL_a_start_cancelled_by_its_caller_is_still_a_cancellation():
    """A startup timeout is the caller cancelling `start()`; it must see that, not an error."""
    reached = asyncio.Event()

    async def wait_forever() -> None:
        reached.set()
        await asyncio.Event().wait()

    svc = _service_class([], on_startup_waits=wait_forever)(_config())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(svc.start(), timeout=0.2)

    assert reached.is_set()


@pytest.mark.asyncio
async def test_CONTROL_a_start_nobody_interrupts_returns_and_the_service_is_running():
    calls: list[str] = []
    svc = _service_class(calls)(_config())

    assert await svc.start() is None

    assert svc.container.lifecycle._running and not svc.container.lifecycle.is_stopped
    await svc.stop()


@pytest.mark.asyncio
async def test_the_runner_reports_the_stop_not_a_connection_that_closed_unexpectedly():
    """The runner counted the return as a successful start, then a second later logged that the
    NATS connection had closed unexpectedly, which is not what happened."""
    calls: list[str] = []
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])), level="INFO"
    )
    runner = ServiceRunner(_service_class(calls, stop_in="on_startup"), _config(auto_restart=False))
    runner._running = True
    try:
        await asyncio.wait_for(runner._run_service(), timeout=5)
    finally:
        logger.remove(sink)

    messages = [m for _, m in lines]
    assert any("was stopped while it was starting" in m for m in messages), messages
    assert not any("closed unexpectedly" in m for m in messages), messages
    assert (runner._start_attempts, runner._successful_starts) == (1, 0)


_STEPS = [
    "setup_extensions",
    "connect",
    "ensure_streams",
    "on_startup",
    "start_extensions",
    "start_health_listener",
    "start_timers",
]


def _manager_that_stops_in(step: str) -> tuple[LifecycleManager, list[str]]:
    """A lifecycle manager over recording hooks, whose `step` hook stops the manager itself.

    Stopping from inside `ensure_streams` is the only way to reach the checkpoint after
    stream provisioning, because `validate_dlq` is synchronous.
    """
    calls: list[str] = []
    holder: dict[str, LifecycleManager] = {}

    def recording(name: str):
        async def hook() -> None:
            calls.append(name)
            if name == step:
                await holder["manager"].stop()

        return hook

    def sync(name: str):
        return lambda: calls.append(name)

    hooks = LifecycleHooks(
        setup_extensions=recording("setup_extensions"),
        discover_handlers=sync("discover_handlers"),
        connect=recording("connect"),
        ensure_streams=recording("ensure_streams"),
        validate_dlq=sync("validate_dlq"),
        is_jetstream_active=lambda: step == "ensure_streams",
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
    manager = LifecycleManager(ServiceConfig(name="svc", health_listener=False), hooks)
    holder["manager"] = manager
    return manager, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("step", _STEPS)
async def test_a_stop_from_inside_each_startup_step_makes_start_raise(step):
    """Every one of the seven awaitable steps of `start()` ends the same way, so a checkpoint
    that went back to a quiet return is caught at the step it belongs to."""
    manager, calls = _manager_that_stops_in(step)

    with pytest.raises(ServiceLifecycleError, match="was stopped while it was starting"):
        await manager.start()

    assert manager.is_stopped and not manager._running
    assert "setup_subscriptions" not in calls
    for teardown in (
        "stop_extensions",
        "disconnect",
        "cancel_subscriptions",
        "stop_timers",
        "stop_health_listener",
    ):
        assert calls.count(teardown) <= 1, (teardown, calls)

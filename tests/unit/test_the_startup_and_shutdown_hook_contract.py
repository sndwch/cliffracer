"""What runs, and in what order, when startup completes, fails, or is stopped part-way.

The contract, read off a recording extension and a recording `on_startup`/`on_shutdown`, with
no broker (`connect`, `disconnect` and the subscription step are stubbed):

- `on_shutdown` pairs with an `on_startup` that RETURNED, the way `__aexit__` pairs with a
  completed `__aenter__`: any teardown after it returned runs `on_shutdown`, including the
  teardown of a startup that failed in a later step. An `on_startup` that raises, or is
  cancelled, gets no `on_shutdown`: it cleans up after itself.
- Extensions start BEFORE the service subscribes to its handlers (`ext.start` precedes the
  subscription step), so a `start()` cannot assume the core subscriptions exist.
- An extension's `stop()` pairs with its `setup()`, not with its `start()`: `setup()` is where
  its resources are built, so a startup that stops before step 6 still releases them.

These tests pin that, so a later change to either pairing is a decision, not an accident.
"""

from __future__ import annotations

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.extension import Extension
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit


def _service(calls: list[str], *, on_startup, fail_subscriptions: bool = False):
    """A service whose every lifecycle call is appended to `calls`; nothing touches a broker."""

    class Recording(Extension):
        async def setup(self, ctx) -> None:
            calls.append("ext.setup")

        async def start(self) -> None:
            calls.append("ext.start")

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
            if fail_subscriptions:
                raise RuntimeError("subscribe failed")

        async def on_startup(self) -> None:
            calls.append("on_startup")
            await on_startup(self)

        async def on_shutdown(self) -> None:
            calls.append("on_shutdown")

    return Svc(ServiceConfig(name="hook_contract_svc", health_port=0))


async def _nothing(svc) -> None:
    return None


async def test_CONTROL_a_completed_startup_and_a_clean_stop_run_every_hook_in_order():
    calls: list[str] = []
    svc = _service(calls, on_startup=_nothing)

    await svc.start()
    await svc.stop()

    assert calls == [
        "ext.setup",
        "connect",
        "on_startup",
        "ext.start",
        "subscriptions",
        "on_shutdown",
        "ext.stop",
        "disconnect",
    ], calls


async def test_an_on_startup_that_raises_gets_no_on_shutdown_and_its_extension_is_stopped():
    calls: list[str] = []

    async def fails(svc) -> None:
        raise RuntimeError("on_startup failed")

    svc = _service(calls, on_startup=fails)

    with pytest.raises(RuntimeError, match="on_startup failed"):
        await svc.start()

    assert calls == ["ext.setup", "connect", "on_startup", "ext.stop", "disconnect"], calls


async def test_an_on_startup_stopped_by_another_task_gets_no_on_shutdown_and_its_ext_is_stopped():
    calls: list[str] = []
    in_hook = asyncio.Event()

    async def waits(svc) -> None:
        in_hook.set()
        await asyncio.sleep(30)

    svc = _service(calls, on_startup=waits)
    starting = asyncio.create_task(svc.start())
    await asyncio.wait_for(in_hook.wait(), timeout=5)

    await svc.stop()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert calls == ["ext.setup", "connect", "on_startup", "ext.stop", "disconnect"], calls


async def test_a_stop_from_inside_on_startup_stops_the_extension_that_was_set_up_but_not_started():
    """The case the issue read as an extension stopped without being set up: `setup()` ran."""
    calls: list[str] = []

    async def stops_the_service(svc) -> None:
        await svc.stop()

    svc = _service(calls, on_startup=stops_the_service)

    with pytest.raises(ServiceLifecycleError, match="stopped while it was starting"):
        await svc.start()

    assert calls == ["ext.setup", "connect", "on_startup", "ext.stop", "disconnect"], calls
    assert svc.container.lifecycle.is_stopped


async def test_a_failure_after_on_startup_returned_still_runs_on_shutdown_then_stops_the_extension():
    """`on_startup` returned, so it is paired: a later failed step does not skip `on_shutdown`."""
    calls: list[str] = []
    svc = _service(calls, on_startup=_nothing, fail_subscriptions=True)

    with pytest.raises(RuntimeError, match="subscribe failed"):
        await svc.start()

    assert calls == [
        "ext.setup",
        "connect",
        "on_startup",
        "ext.start",
        "subscriptions",
        "on_shutdown",
        "ext.stop",
        "disconnect",
    ], calls

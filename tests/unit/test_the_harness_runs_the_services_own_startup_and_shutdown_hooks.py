"""`ServiceTestHarness` runs the service's own `on_startup` and `on_shutdown`, in a live start's order.

The harness ran the extension hooks and flipped the lifecycle to running, and never called the
service's own hooks. A service whose `on_startup` builds what its handlers use (a pool, a cache,
a client) behaved differently under the harness than under `start()`: a suite that used the
harness alone was green for a service that raised on its first request in production, and an
`on_shutdown` that releases a resource never ran.

The order is the one `LifecycleManager` runs: extension `setup()`, the service's `on_startup`,
extension `start()`; and on the way down the service's `on_shutdown`, then extension `stop()`.
`on_shutdown` pairs with an `on_startup` that returned, as it does live.
"""

import pytest

from cliffracer.core.decorators import rpc
from cliffracer.core.extension import Extension
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing import ServiceTestHarness
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit

HOOKS = ["ext.setup", "on_startup", "ext.start", "on_shutdown", "ext.stop"]


def _service_class(
    calls: list[str],
    *,
    live: bool = False,
    on_startup_raises: bool = False,
    ext_start_raises: bool = False,
    on_shutdown_raises: bool = False,
):
    """A service whose hooks record to `calls`. `live` stubs the broker steps of a real `start()`."""

    class Recording(Extension):
        async def setup(self, ctx) -> None:
            calls.append("ext.setup")

        async def start(self) -> None:
            calls.append("ext.start")
            if ext_start_raises:
                raise RuntimeError("extension start failed")

        async def stop(self) -> None:
            calls.append("ext.stop")

    class Svc(ServicePhases, CliffracerService):
        recorded = Recording()
        pool: str | None = None
        handlers_seen: int | None = None

        async def on_startup(self) -> None:
            calls.append("on_startup")
            self.handlers_seen = len(self.container.registry.rpc_handlers)
            if on_startup_raises:
                raise RuntimeError("startup failed")
            self.pool = "pool-1"

        async def on_shutdown(self) -> None:
            calls.append("on_shutdown")
            self.pool = None
            if on_shutdown_raises:
                raise RuntimeError("shutdown failed")

        @rpc
        async def pool_name(self) -> str:
            return self.pool or "no pool"

        if live:

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

            async def _setup_subscriptions(self) -> None:
                pass

    return Svc


def _config() -> ServiceConfig:
    return ServiceConfig(name="hooks_svc", health_port=0)


async def test_a_handler_sees_the_state_on_startup_built():
    calls: list[str] = []

    async with ServiceTestHarness(_service_class(calls), config=_config()) as harness:
        response = await harness.rpc("pool_name")

        assert response.result == "pool-1"


async def test_the_hooks_run_in_the_order_a_live_start_and_stop_run_them():
    harness_calls: list[str] = []
    async with ServiceTestHarness(_service_class(harness_calls), config=_config()):
        pass

    live_calls: list[str] = []
    live = _service_class(live_calls, live=True)(_config())
    await live.start()
    await live.stop()

    assert live_calls == HOOKS, "the premise: this is the live order"
    assert harness_calls == live_calls


async def test_on_shutdown_runs_once_at_teardown_and_releases_what_on_startup_built():
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls), config=_config())
    await harness.setup()
    service = harness.service
    assert service.pool == "pool-1"

    await harness.teardown()
    await harness.teardown()

    assert calls.count("on_shutdown") == 1
    assert service.pool is None


async def test_a_harness_that_never_started_runs_no_shutdown_hook():
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls), config=_config())

    await harness.teardown()

    assert calls == []


async def test_an_on_startup_that_raises_propagates_and_stops_the_extensions_without_on_shutdown():
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls, on_startup_raises=True), config=_config())

    with pytest.raises(RuntimeError, match="startup failed"):
        await harness.setup()

    assert calls == ["ext.setup", "on_startup", "ext.stop"], calls
    with pytest.raises(RuntimeError, match="torn down"):
        await harness.setup()


async def test_an_extension_start_that_raises_runs_on_shutdown_and_stops_the_extensions():
    """`on_startup` had returned, so its pair runs, as it does when a live start fails later."""
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls, ext_start_raises=True), config=_config())

    with pytest.raises(RuntimeError, match="extension start failed"):
        await harness.setup()

    assert calls == ["ext.setup", "on_startup", "ext.start", "on_shutdown", "ext.stop"], calls


async def test_an_on_shutdown_that_raises_propagates_after_the_extensions_are_stopped():
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls, on_shutdown_raises=True), config=_config())
    await harness.setup()

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await harness.teardown()

    assert calls[-2:] == ["on_shutdown", "ext.stop"], calls


async def test_CONTROL_a_service_that_defines_no_hooks_is_unchanged():
    class Plain(CliffracerService):
        @rpc
        async def ping(self) -> str:
            return "pong"

    async with ServiceTestHarness(Plain, config=_config()) as harness:
        assert (await harness.rpc("ping")).result == "pong"


async def test_a_service_stop_after_the_harness_teardown_does_not_run_on_shutdown_again():
    """The lifecycle's own record that `on_startup` returned is cleared, as a live stop clears it."""
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls), config=_config())
    await harness.setup()
    await harness.teardown()

    await harness.service.stop()

    assert calls.count("on_shutdown") == 1, calls


async def test_on_startup_runs_after_handler_discovery():
    """A live start discovers handlers before `on_startup`, so a hook that reads its own registry
    sees the handlers; the order of the three hooks alone does not pin that, discovery is not one."""
    calls: list[str] = []
    harness = ServiceTestHarness(_service_class(calls), config=_config())

    await harness.setup()
    try:
        assert harness.service.handlers_seen == 1  # the one @rpc the class declares
    finally:
        await harness.teardown()

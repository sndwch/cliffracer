"""Tests verifying race condition fixes between start() and stop() in CliffracerService."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer.core.exceptions import ServiceLifecycleError
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


class SlowStartupService(CliffracerService):
    """Service with delayed startup hook to simulate concurrent stop() invocation."""

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.startup_started = asyncio.Event()
        self.allow_startup_to_finish = asyncio.Event()
        self.startup_finished = False
        self.shutdown_called_count = 0

    async def on_startup(self) -> None:
        self.startup_started.set()
        await self.allow_startup_to_finish.wait()
        self.startup_finished = True

    async def on_shutdown(self) -> None:
        self.shutdown_called_count += 1


async def test_stop_during_start_cancels_start_task():
    """Stopping a service while start() is running cancels the start task and cleans up."""
    cfg = ServiceConfig(name="race_svc", health_port=0)
    svc = SlowStartupService(cfg)

    # Mock low-level networking
    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
        patch.object(svc.container, "setup_subscriptions", new_callable=AsyncMock) as mock_subs,
    ):
        start_task = asyncio.create_task(svc.start())

        # Wait until on_startup is running and service is in _starting state
        await svc.startup_started.wait()
        assert svc._starting is True
        assert svc._stopped is False

        # Now call stop() concurrently from outside
        await svc.stop()

        # The start task should have completed / been cancelled
        assert start_task.done()

        # Service must not be left running or starting
        assert svc._running is False
        assert svc._starting is False
        assert svc._stopped is True

        # Subscriptions should not have been set up after stop
        mock_subs.assert_not_called()
        assert svc.startup_finished is False
        assert svc.shutdown_called_count == 0


async def test_stop_idempotent_after_cancel():
    """Consecutive stop() calls after a cancelled start() are safe and idempotent."""
    cfg = ServiceConfig(name="race_idempotent_svc", health_port=0)
    svc = SlowStartupService(cfg)

    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        _ = asyncio.create_task(svc.start())
        await svc.startup_started.wait()

        # First stop
        await svc.stop()
        assert svc.shutdown_called_count == 0
        assert svc._stopped is True
        assert svc._running is False

        # Second stop
        await svc.stop()
        assert svc.shutdown_called_count == 0  # Not incremented again


async def test_stop_called_from_within_on_startup():
    """A service that invokes stop() inside its own on_startup hook exits cleanly."""

    class SelfStoppingService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.stop_invoked = False

        async def on_startup(self) -> None:
            self.stop_invoked = True
            await self.stop()

    cfg = ServiceConfig(name="self_stopping_svc", health_port=0)
    svc = SelfStoppingService(cfg)

    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
        patch.object(svc.container, "setup_subscriptions", new_callable=AsyncMock) as mock_subs,
    ):
        await svc.start()

        assert svc.stop_invoked is True
        assert svc._running is False
        assert svc._stopped is True
        assert svc._starting is False
        mock_subs.assert_not_called()


async def test_on_shutdown_not_called_if_on_startup_never_ran():
    """If start() fails before on_startup (e.g. connect fails), on_shutdown must not run."""

    class FailingConnectService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.startup_called = False
            self.shutdown_called = False

        async def on_startup(self) -> None:
            self.startup_called = True

        async def on_shutdown(self) -> None:
            self.shutdown_called = True

    cfg = ServiceConfig(name="failing_connect_svc", health_port=0)
    svc = FailingConnectService(cfg)

    with (
        patch.object(svc, "connect", side_effect=ConnectionError("broker down")),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        with pytest.raises(ConnectionError, match="broker down"):
            await svc.start()

        assert svc.startup_called is False
        assert svc.shutdown_called is False
        assert svc._running is False
        assert svc._stopped is True


async def test_abortive_startup_cleanup_and_idempotent_stop():
    """Defensive stop() call after an abortive start() is clean and idempotent."""

    class FailingStartupService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.shutdown_called = False

        async def on_startup(self) -> None:
            raise RuntimeError("startup hook crashed")

        async def on_shutdown(self) -> None:
            self.shutdown_called = True

    cfg = ServiceConfig(name="abortive_svc", health_port=0)
    svc = FailingStartupService(cfg)

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        with pytest.raises(RuntimeError, match="startup hook crashed"):
            await svc.start()

        assert svc._startup_succeeded is False
        assert svc.shutdown_called is False
        assert svc._running is False
        assert svc._stopped is True

        # Defensive stop in finally block must be safe and idempotent
        await svc.stop()
        assert svc.shutdown_called is False
        assert svc._stopped is True


async def test_on_shutdown_invoked_when_startup_succeeded():
    """When start() completes successfully, stop() invokes on_shutdown()."""

    class NormalService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.shutdown_count = 0

        async def on_shutdown(self) -> None:
            self.shutdown_count += 1

    cfg = ServiceConfig(name="normal_svc", health_port=0)
    svc = NormalService(cfg)

    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
        patch.object(svc.container, "setup_subscriptions", new_callable=AsyncMock),
    ):
        await svc.start()
        assert svc._startup_succeeded is True
        assert svc._running is True

        await svc.stop()
        assert svc.shutdown_count == 1
        assert svc._running is False
        assert svc._stopped is True


@pytest.mark.asyncio
async def test_queued_start_rejected_when_stop_cancels_inflight_start():
    """Verify that a start() coroutine waiting on _lock is rejected when stop() cancels startup."""
    cfg = ServiceConfig(name="queued_start_svc", health_port=0)
    svc = SlowStartupService(cfg)
    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False

    with (
        patch("nats.connect", return_value=mock_nc),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        # Task 1 starts the service and pauses in on_startup
        task1 = asyncio.create_task(svc.start())
        await svc.startup_started.wait()

        # Task 2 attempts start() and blocks waiting for _lock
        task2 = asyncio.create_task(svc.start())
        await asyncio.sleep(0.01)
        assert not task2.done()

        # Task 3 calls stop()
        stop_task = asyncio.create_task(svc.stop())
        await asyncio.wait_for(stop_task, timeout=2.0)

        # Task 1 was cancelled
        assert task1.done()

        # Task 2 must have failed with ServiceLifecycleError, NOT acquired lock and started afresh
        with pytest.raises(ServiceLifecycleError, match="stop has been requested"):
            await task2

        # Service must be stopped and not running
        assert svc._running is False
        assert svc._stopped is True


@pytest.mark.asyncio
async def test_abortive_cleanup_failure_allows_stop_retry():
    """Verify that if abortive cleanup fails, _stopped remains False and stop() retries teardown."""
    cfg = ServiceConfig(name="abortive_retry_svc", health_port=0)
    svc = CliffracerService(cfg)

    disconnect_attempts = 0

    async def flaky_disconnect():
        nonlocal disconnect_attempts
        disconnect_attempts += 1
        if disconnect_attempts == 1:
            raise OSError("First disconnect attempt dropped")

    svc.connect = AsyncMock()  # type: ignore[method-assign]
    svc.disconnect = flaky_disconnect  # type: ignore[method-assign]
    svc.on_startup = AsyncMock(side_effect=RuntimeError("Startup failed"))  # type: ignore[method-assign]

    # Primary start fails
    with pytest.raises(RuntimeError, match="Startup failed"):
        await svc.start()

    # Because disconnect failed during abortive cleanup, _stopped MUST be False
    assert svc._stopped is False
    assert svc._running is False
    assert disconnect_attempts == 1

    # Defensive call in finally: await svc.stop() retries teardown
    await svc.stop()
    assert disconnect_attempts == 2
    assert svc._stopped is True


@pytest.mark.asyncio
async def test_shutdown_hook_invoked_on_failure_after_on_startup():
    """Verify on_shutdown() is invoked if startup fails after on_startup() completed."""
    cfg = ServiceConfig(name="hook_cleanup_svc", health_port=0)

    class ResourceTrackingService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.resource_allocated = False
            self.resource_cleaned_up = False

        async def on_startup(self) -> None:
            self.resource_allocated = True

        async def on_shutdown(self) -> None:
            self.resource_cleaned_up = True

    svc = ResourceTrackingService(cfg)
    svc.connect = AsyncMock()  # type: ignore[method-assign]
    svc.disconnect = AsyncMock()  # type: ignore[method-assign]
    # Simulate failure in subsequent startup stage
    svc.container.setup_subscriptions = AsyncMock(
        side_effect=ConnectionError("Subscriptions failed")
    )  # type: ignore[method-assign]

    with pytest.raises(ConnectionError, match="Subscriptions failed"):
        await svc.start()

    # on_startup ran, so on_shutdown MUST run during teardown to clean up the resource
    assert svc.resource_allocated is True
    assert svc.resource_cleaned_up is True
    assert svc._stopped is True


@pytest.mark.asyncio
async def test_shutdown_hook_skipped_on_failure_before_on_startup():
    """Verify on_shutdown() is skipped if startup fails before on_startup() executes."""
    cfg = ServiceConfig(name="hook_inversion_svc", health_port=0)

    class InversionTrackingService(CliffracerService):
        def __init__(self, config: ServiceConfig) -> None:
            super().__init__(config)
            self.startup_ran = False
            self.shutdown_ran = False

        async def on_startup(self) -> None:
            self.startup_ran = True

        async def on_shutdown(self) -> None:
            self.shutdown_ran = True

    svc = InversionTrackingService(cfg)
    # Fail at connect stage before on_startup
    svc.connect = AsyncMock(side_effect=ConnectionError("Broker unreachable"))  # type: ignore[method-assign]
    svc.disconnect = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ConnectionError, match="Broker unreachable"):
        await svc.start()

    assert svc.startup_ran is False
    assert svc.shutdown_ran is False
    assert svc._stopped is True

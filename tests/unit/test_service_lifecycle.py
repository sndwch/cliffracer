"""
Comprehensive tests for service lifecycle management
"""

import asyncio
from unittest.mock import AsyncMock, patch

import nats
import pytest

from cliffracer import CliffracerService, ServiceConfig, ServiceOrchestrator, ServiceRunner
from cliffracer.runners.orchestrator import RUNNER_OK

pytestmark = pytest.mark.unit


class TestServiceLifecycle:
    """Test service startup, shutdown, and lifecycle management"""

    class LifecycleSvc(CliffracerService):
        def __init__(self, config):
            super().__init__(config)
            self.startup_called = False
            self.shutdown_called = False

        async def on_startup(self):
            """Custom startup logic"""
            self.startup_called = True
            await super().on_startup()

        async def on_shutdown(self):
            """Custom shutdown logic"""
            self.shutdown_called = True
            await super().on_shutdown()

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(
            name="test_lifecycle_service",
            on_connect=None,  # Will be set in tests
            on_disconnect=None,
        )

    @pytest.fixture
    def service(self, service_config):
        return self.LifecycleSvc(service_config)

    @pytest.mark.asyncio
    async def test_service_startup_sequence(self, service):
        """Test complete startup sequence"""
        # Mock NATS connection
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            # Start service
            await service.start()

            # Verify startup sequence
            assert service._running is True
            assert service.startup_called is True
            # The connection `nats.connect` returned, not merely something
            assert service.nc is mock_nc

            # Verify the framework's own subscriptions were created: the subjects, and the queue
            # group on the two request paths (a replica pair shares one, so a request is handled
            # once)
            name = service.config.name
            calls = {c.args[0]: c.kwargs for c in mock_nc.subscribe.call_args_list}
            assert {f"{name}.rpc.*", f"{name}.async.*", f"{name}.describe"} <= set(calls), calls
            assert calls[f"{name}.rpc.*"]["queue"] == f"{name}.rpc"
            assert calls[f"{name}.async.*"]["queue"] == f"{name}.async"

            # Stop service
            await service.stop()

    @pytest.mark.asyncio
    async def test_service_shutdown_sequence(self, service):
        """Test complete shutdown sequence"""
        # Mock NATS connection
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            # Start and stop service
            await service.start()
            await service.stop()

            # Verify shutdown sequence
            assert service._running is False
            assert service.shutdown_called is True
            assert mock_nc.drain.called
            assert mock_nc.close.called

    @pytest.mark.asyncio
    async def test_service_reconnection_handling(self):
        """The broker's callbacks reach the hooks the service configured, in the order they fire.

        `on_connect` / `on_disconnect` / `on_error` are `ServiceConfig` hooks: the framework calls
        them from nats-py's `connected`/`disconnected`/`reconnected`/`error` callbacks and from
        nothing else, so each callback is fired by hand and the hook it must reach is read.
        """
        timeline: list[tuple[str, object]] = []
        service = self.LifecycleSvc(
            ServiceConfig(
                name="test_lifecycle_reconnect",
                health_port=0,
                on_connect=lambda: timeline.append(("connect", None)),
                on_disconnect=lambda: timeline.append(("disconnect", None)),
                on_error=lambda exc: timeline.append(("error", exc)),
            )
        )
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        # Capture callbacks
        callbacks = {}

        async def mock_connect(*args, **kwargs):
            callbacks["error_cb"] = kwargs.get("error_cb")
            callbacks["disconnected_cb"] = kwargs.get("disconnected_cb")
            callbacks["reconnected_cb"] = kwargs.get("reconnected_cb")
            callbacks["closed_cb"] = kwargs.get("closed_cb")
            return mock_nc

        boom = Exception("Test error")
        with patch("cliffracer.core.dial.connect", side_effect=mock_connect):
            await service.start()
            assert timeline == [("connect", None)], "the initial connect fires on_connect once"

            # Simulate disconnection
            await callbacks["disconnected_cb"]()
            assert timeline[1:] == [("disconnect", None)], timeline

            # Simulate reconnection: the connect hook fires again
            await callbacks["reconnected_cb"]()
            assert timeline[2:] == [("connect", None)], timeline

            # Simulate error: the hook receives the exception itself
            await callbacks["error_cb"](boom)
            assert len(timeline) == 4 and timeline[3] == ("error", boom), timeline

            # Simulate closed. A close while the service is running stops
            # the service without exiting the process.
            await callbacks["closed_cb"]()
            assert not service._running

            # stop() is reached and is idempotent.
            await service.stop()

    @pytest.mark.asyncio
    async def test_service_with_lifecycle_hooks(self, service_config):
        """Test service with custom lifecycle hooks"""
        connect_called = False
        disconnect_called = False
        error_called = False

        async def on_connect():
            nonlocal connect_called
            connect_called = True

        async def on_disconnect():
            nonlocal disconnect_called
            disconnect_called = True

        async def on_error(e):
            nonlocal error_called
            error_called = True

        # Update config with hooks
        service_config.on_connect = on_connect
        service_config.on_disconnect = on_disconnect
        service_config.on_error = on_error

        service = self.LifecycleSvc(service_config)

        # Mock NATS
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        callbacks = {}

        async def mock_connect(*args, **kwargs):
            callbacks["disconnected_cb"] = kwargs.get("disconnected_cb")
            callbacks["error_cb"] = kwargs.get("error_cb")
            # Mock supplies an inert connection without invoking callbacks directly.
            return mock_nc

        with patch("cliffracer.core.dial.connect", side_effect=mock_connect):
            await service.start()

            # Verify connect hook was called
            assert connect_called is True

            # Trigger disconnect
            if "disconnected_cb" in callbacks:
                await callbacks["disconnected_cb"]()
            assert disconnect_called is True

            # Trigger error
            if "error_cb" in callbacks:
                await callbacks["error_cb"](Exception("Test"))
            assert error_called is True

            await service.stop()

    @pytest.mark.asyncio
    async def test_service_subscription_cleanup(self, service):
        """Verify stop() cancels the tasks spawned by start()."""
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        mock_nc.subscribe = AsyncMock(return_value=AsyncMock())

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            await service.start()

            started = list(service.container._subscriptions)
            assert started, "start() spawned no subscription tasks to test"

            await service.stop()

            assert service.container._subscriptions == set(), "stop() must clear the registry"
            for task in started:
                assert task.cancelled() or task.done(), task

    @pytest.mark.asyncio
    async def test_service_already_running(self, service):
        """Test that starting an already running service is handled idempotently."""
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            await service.start()
            initial_subs_count = len(service.container._subscriptions)
            subscribe_call_count = mock_nc.subscribe.call_count

            # Try to start again - should not raise, logs warning, and does not duplicate subscriptions
            await service.start()

            # Service should still be running and subscription count unchanged
            assert service._running is True
            assert len(service.container._subscriptions) == initial_subs_count
            assert mock_nc.subscribe.call_count == subscribe_call_count

            await service.stop()

    @pytest.mark.asyncio
    async def test_stop_during_reconnecting_broker_does_not_raise(self, service):
        """stop() must not raise ConnectionReconnectingError when broker is reconnecting."""
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        mock_nc.is_connecting = False
        mock_nc.is_reconnecting = True
        mock_nc.drain.side_effect = nats.errors.ConnectionReconnectingError

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            await service.start()
            # Must succeed cleanly without raising
            await service.stop()

            assert service._stopped is True
            assert service._running is False
            assert mock_nc.close.called

    @pytest.mark.asyncio
    async def test_stop_cleans_up_and_closes_connection_even_when_hook_raises(self, service_config):
        """stop() must guarantee NATS disconnect even if on_shutdown raises."""

        class FailingShutdownSvc(CliffracerService):
            async def on_shutdown(self):
                raise RuntimeError("user shutdown hook crashed")

        service = FailingShutdownSvc(service_config)
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            await service.start()

            with pytest.raises(RuntimeError, match="user shutdown hook crashed"):
                await service.stop()

            # Connection must still be closed (no leak)
            assert mock_nc.close.called
            assert service._stopped is True

            # Subsequent stop is a safe no-op
            await service.stop()

    @pytest.mark.asyncio
    async def test_service_double_stop(self, service):
        """Test that stopping an already stopped service is safe"""
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            await service.start()
            await service.stop()

            # Stop again - should not raise
            await service.stop()

            # Verify drain/close were only called once
            assert mock_nc.drain.call_count == 1
            assert mock_nc.close.call_count == 1


async def _until(predicate, timeout=5.0):
    """Poll *predicate* until it holds, failing by name at *timeout*."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition still false after {timeout}s")
        await asyncio.sleep(0.01)


class TestServiceRunner:
    """Test ServiceRunner functionality"""

    @pytest.mark.asyncio
    async def test_service_runner_basic(self):
        """The runner starts its service, and a shutdown stops it and reports success."""
        config = ServiceConfig(name="test_runner_service")
        runner = ServiceRunner(CliffracerService, config)

        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            run_task = asyncio.create_task(runner.run())

            await _until(lambda: runner.service is not None and runner.service._running)

            runner._running = False
            runner._shutdown_event.set()
            result = await asyncio.wait_for(run_task, timeout=5)

        assert result == RUNNER_OK
        assert runner.service._running is False

    @pytest.mark.asyncio
    async def test_service_runner_auto_restart(self):
        """Test ServiceRunner auto-restart functionality"""
        config = ServiceConfig(
            name="test_restart_service",
            auto_restart=True,
            restart_delay=0.1,
        )

        restart_count = 0

        class RestartTestService(CliffracerService):
            async def start(self):
                nonlocal restart_count
                restart_count += 1
                if restart_count < 2:
                    # Simulate failure on first attempt
                    raise Exception("Simulated failure")
                await super().start()

        runner = ServiceRunner(RestartTestService, config)

        # Mock NATS
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            # Start runner - should retry once
            run_task = asyncio.create_task(runner.run())

            # Wait for retries
            await asyncio.sleep(0.3)

            # Verify it restarted
            assert restart_count == 2

            # Stop runner
            runner._running = False
            runner._shutdown_event.set()

            # Await run task shutdown.
            await asyncio.wait_for(run_task, timeout=5)


class TestServiceOrchestrator:
    """Test ServiceOrchestrator functionality"""

    @pytest.mark.asyncio
    async def test_orchestrator_multiple_services(self):
        """Test orchestrator managing multiple services"""
        orchestrator = ServiceOrchestrator()

        # Add multiple services
        configs = [
            ServiceConfig(name="service1"),
            ServiceConfig(name="service2"),
            ServiceConfig(name="service3"),
        ]

        for config in configs:
            orchestrator.add_service(CliffracerService, config)

        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            run_task = asyncio.create_task(orchestrator.run())

            await _until(
                lambda: all(
                    r.service is not None and r.service._running for r in orchestrator.runners
                )
            )
            assert sorted(r.service.config.name for r in orchestrator.runners) == [
                "service1",
                "service2",
                "service3",
            ]

            await orchestrator.stop()
            await asyncio.wait_for(run_task, timeout=5)

        assert not any(r.service._running for r in orchestrator.runners)

    @pytest.mark.asyncio
    async def test_orchestrator_service_failure_handling(self):
        """Test orchestrator handles service failures"""
        orchestrator = ServiceOrchestrator()

        # Counted on the class: the runner builds a fresh instance per attempt.
        class FailingService(CliffracerService):
            start_calls = 0

            async def start(self):
                type(self).start_calls += 1
                raise Exception("Service failed to start")

        # A short restart_delay, so a retry the runner should not make would
        # land inside the window below.
        config = ServiceConfig(name="failing_service", auto_restart=False, restart_delay=0.01)
        orchestrator.add_service(FailingService, config)

        # Add a normal service
        orchestrator.add_service(CliffracerService, ServiceConfig(name="normal_service"))

        # Mock NATS
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        failing, healthy = orchestrator.runners

        with patch("cliffracer.core.dial.connect", return_value=mock_nc):
            run_task = asyncio.create_task(orchestrator.run())

            # The failure happened and was contained: the sibling still came up.
            await _until(lambda: FailingService.start_calls == 1)
            await _until(lambda: healthy.service is not None and healthy.service._running)
            await asyncio.sleep(0.2)

            await orchestrator.stop()
            await asyncio.wait_for(run_task, timeout=5)

        # auto_restart=False: one attempt, never retried.
        assert FailingService.start_calls == 1
        assert failing._successful_starts == 0
        assert healthy._successful_starts == 1


@pytest.mark.asyncio
async def test_overlapping_starts_run_the_startup_once():
    """Two start() calls that overlap run the startup once, and stop() then stops it.

    The count is what reads the serialization: without the lifecycle lock both
    calls enter the startup together, and without start()'s already-running
    check the second runs it again after the first. Either way it runs twice.
    Both calls must have entered start() while the first was inside the startup,
    or a count of one would only show that they ran one after the other.
    """
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False
    mock_nc.drain = AsyncMock()
    mock_nc.close = AsyncMock()

    with patch("cliffracer.core.dial.connect", return_value=mock_nc):
        svc = CliffracerService(ServiceConfig(name="race_svc", health_port=0))
        hooks = svc.container.lifecycle.hooks
        setup_extensions = hooks.setup_extensions
        startups = 0
        entered = 0
        entered_while_starting: list[int] = []

        async def counted_setup_extensions():
            nonlocal startups
            startups += 1
            await asyncio.sleep(0)  # a point at which the other start() can interleave
            entered_while_starting.append(entered)
            await setup_extensions()

        hooks.setup_extensions = counted_setup_extensions

        async def c1():
            nonlocal entered
            entered += 1
            await svc.start()

        async def c2():
            nonlocal entered
            entered += 1
            await svc.start()
            await svc.stop()

        await asyncio.gather(c1(), c2())

        assert entered_while_starting[:1] == [2], "the second start() did not overlap the first"
        assert startups == 1, f"the startup ran {startups} times for overlapping start() calls"
        assert svc._running is False
        assert svc._stopped is True


@pytest.mark.asyncio
@pytest.mark.parametrize("yield_after_start", [False, True])
async def test_stop_unsubscribes_then_drains_then_closes(yield_after_start):
    """Intake closes before work drains, including an immediate stop after startup."""
    events: list[str] = []
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False
    mock_nc.is_draining = False

    async def subscribe(subject, **kwargs):
        sub = AsyncMock()

        async def unsubscribe():
            events.append(f"unsubscribe {subject}")

        sub.unsubscribe = unsubscribe
        return sub

    async def drain():
        events.append("drain")

    async def close():
        events.append("close")

    mock_nc.subscribe = subscribe
    mock_nc.drain = drain
    mock_nc.close = close

    with patch("cliffracer.core.dial.connect", return_value=mock_nc):
        svc = CliffracerService(ServiceConfig(name="warehouse", health_port=0))
        drain_tasks = svc.container.lifecycle.drain_active_tasks

        async def finish_orders(*, timeout):
            events.append("finish orders")
            await drain_tasks(timeout=timeout)

        svc.container.lifecycle.drain_active_tasks = finish_orders
        await svc.start()
        if yield_after_start:
            await asyncio.sleep(0)
        await svc.stop()

    assert events[3:] == ["finish orders", "drain", "close"], events
    assert sorted(events[:3]) == [
        "unsubscribe warehouse.async.*",
        "unsubscribe warehouse.describe",
        "unsubscribe warehouse.rpc.*",
    ], events
    assert len(svc.container._subscriptions) == 0

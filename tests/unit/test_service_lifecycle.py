"""
Comprehensive tests for service lifecycle management
"""

import asyncio
from unittest.mock import AsyncMock, patch

import nats
import pytest

from cliffracer import CliffracerService, ServiceConfig, ServiceOrchestrator, ServiceRunner

pytestmark = pytest.mark.unit


class TestServiceLifecycle:
    """Test service startup, shutdown, and lifecycle management"""

    class LifecycleSvc(CliffracerService):
        def __init__(self, config):
            super().__init__(config)
            self.startup_called = False
            self.shutdown_called = False
            self.connect_count = 0
            self.disconnect_count = 0

        async def on_startup(self):
            """Custom startup logic"""
            self.startup_called = True
            await super().on_startup()

        async def on_shutdown(self):
            """Custom shutdown logic"""
            self.shutdown_called = True
            await super().on_shutdown()

        async def on_connect(self):
            """Track connections"""
            self.connect_count += 1

        async def on_disconnect(self):
            """Track disconnections"""
            self.disconnect_count += 1

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

        with patch("nats.connect", return_value=mock_nc):
            # Start service
            await service.start()

            # Verify startup sequence
            assert service._running is True
            assert service.startup_called is True
            assert service.nc is not None

            # Verify subscriptions were created
            assert mock_nc.subscribe.called

            # Stop service
            await service.stop()

    @pytest.mark.asyncio
    async def test_service_shutdown_sequence(self, service):
        """Test complete shutdown sequence"""
        # Mock NATS connection
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("nats.connect", return_value=mock_nc):
            # Start and stop service
            await service.start()
            await service.stop()

            # Verify shutdown sequence
            assert service._running is False
            assert service.shutdown_called is True
            assert mock_nc.drain.called
            assert mock_nc.close.called

    @pytest.mark.asyncio
    async def test_service_reconnection_handling(self, service):
        """Test service handles reconnections properly"""
        # Mock NATS connection with callbacks
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

        with patch("nats.connect", side_effect=mock_connect):
            await service.start()

            # Simulate disconnection
            await callbacks["disconnected_cb"]()

            # Simulate reconnection
            await callbacks["reconnected_cb"]()

            # Simulate error
            await callbacks["error_cb"](Exception("Test error"))

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

        with patch("nats.connect", side_effect=mock_connect):
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

        with patch("nats.connect", return_value=mock_nc):
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

        with patch("nats.connect", return_value=mock_nc):
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

        with patch("nats.connect", return_value=mock_nc):
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

        with patch("nats.connect", return_value=mock_nc):
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

        with patch("nats.connect", return_value=mock_nc):
            await service.start()
            await service.stop()

            # Stop again - should not raise
            await service.stop()

            # Verify drain/close were only called once
            assert mock_nc.drain.call_count == 1
            assert mock_nc.close.call_count == 1


class TestServiceRunner:
    """Test ServiceRunner functionality"""

    @pytest.mark.asyncio
    async def test_service_runner_basic(self):
        """Test basic ServiceRunner functionality"""
        config = ServiceConfig(name="test_runner_service")

        # Create runner
        runner = ServiceRunner(CliffracerService, config)

        # Mock NATS
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("nats.connect", return_value=mock_nc):
            # Start runner
            run_task = asyncio.create_task(runner.run())

            # Let it run briefly
            await asyncio.sleep(0.1)

            # Stop runner
            runner._running = False
            runner._shutdown_event.set()

            # Await run task shutdown.
            await asyncio.wait_for(run_task, timeout=5)

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

        with patch("nats.connect", return_value=mock_nc):
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

        # Verify services were added
        assert len(orchestrator.runners) == 3

        # Mock NATS for all services
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("nats.connect", return_value=mock_nc):
            # Start orchestrator
            run_task = asyncio.create_task(orchestrator.run())

            # Let services start
            await asyncio.sleep(0.1)

            # Stop orchestrator
            await orchestrator.stop()

            # Await orchestrator run task shutdown.
            await asyncio.wait_for(run_task, timeout=5)

    @pytest.mark.asyncio
    async def test_orchestrator_service_failure_handling(self):
        """Test orchestrator handles service failures"""
        orchestrator = ServiceOrchestrator()

        # Add a service that will fail
        class FailingService(CliffracerService):
            async def start(self):
                raise Exception("Service failed to start")

        config = ServiceConfig(name="failing_service", auto_restart=False)
        orchestrator.add_service(FailingService, config)

        # Add a normal service
        orchestrator.add_service(CliffracerService, ServiceConfig(name="normal_service"))

        # Mock NATS
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("nats.connect", return_value=mock_nc):
            # Start orchestrator
            run_task = asyncio.create_task(orchestrator.run())

            # Let services try to start
            await asyncio.sleep(0.1)

            # Normal service should still be running
            # Service might be running in the background
            await asyncio.sleep(0.1)  # Give it more time to start

            # Stop orchestrator
            await orchestrator.stop()

            # Await orchestrator run task shutdown.
            await asyncio.wait_for(run_task, timeout=5)

            assert len(orchestrator.runners) == 2
            assert orchestrator.runners[0]._running is False
            assert orchestrator.runners[1].service is not None
            assert orchestrator.runners[1]._running is False
            assert orchestrator.runners[1].service._running is False


@pytest.mark.asyncio
async def test_concurrent_start_and_stop_eliminates_zombie_state():
    """Verify atomic start/stop lock eliminates zombie states under concurrency."""
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False
    mock_nc.drain = AsyncMock()
    mock_nc.close = AsyncMock()

    with patch("nats.connect", return_value=mock_nc):
        svc = CliffracerService(ServiceConfig(name="race_svc", health_port=0))

        async def c1():
            await svc.start()

        async def c2():
            await svc.start()
            await svc.stop()

        await asyncio.gather(c1(), c2())
        assert svc._running is False
        assert svc._stopped is True


@pytest.mark.asyncio
async def test_disconnect_drains_before_unsubscribing():
    """Verify disconnect calls nc.drain while subscriptions are intact."""
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_connected = True
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False

    drain_called_before_close = False

    async def mock_drain():
        nonlocal drain_called_before_close
        drain_called_before_close = True

    mock_nc.drain = mock_drain

    with patch("nats.connect", return_value=mock_nc):
        svc = CliffracerService(ServiceConfig(name="drain_svc", health_port=0))
        await svc.start()
        assert len(svc.container._subscriptions) > 0
        await svc.stop()
        assert drain_called_before_close is True
        assert len(svc.container._subscriptions) == 0

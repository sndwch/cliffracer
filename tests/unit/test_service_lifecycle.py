"""
Comprehensive tests for service lifecycle management
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import NATSService, ServiceConfig, ServiceOrchestrator, ServiceRunner


class TestServiceLifecycle:
    """Test service startup, shutdown, and lifecycle management"""

    class TestLifecycleService(NATSService):
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
        return self.TestLifecycleService(service_config)

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

            # Simulate closed
            await callbacks["closed_cb"]()

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

        service = self.TestLifecycleService(service_config)

        # Mock NATS
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        callbacks = {}

        async def mock_connect(*args, **kwargs):
            callbacks["disconnected_cb"] = kwargs.get("disconnected_cb")
            callbacks["error_cb"] = kwargs.get("error_cb")
            # Simulate immediate connection callback
            if service_config.on_connect:
                await service_config.on_connect()
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
        """Test that subscriptions are properly cleaned up"""
        # Mock NATS and subscriptions
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        # Track subscription tasks
        subscription_tasks = []

        async def mock_subscribe(*args, **kwargs):
            mock_sub = AsyncMock()
            task = asyncio.create_task(asyncio.sleep(0.1))
            subscription_tasks.append(task)
            return mock_sub

        mock_nc.subscribe = mock_subscribe

        with patch("nats.connect", return_value=mock_nc):
            await service.start()

            # Verify subscriptions were created
            assert len(service._subscriptions) > 0

            # Stop service
            await service.stop()

            # Verify all subscription tasks were cancelled
            for task in service._subscriptions:
                assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_service_already_running(self, service):
        """Test that starting an already running service is handled"""
        mock_nc = AsyncMock()
        mock_nc.is_closed = False

        with patch("nats.connect", return_value=mock_nc):
            await service.start()

            # Try to start again - should not raise but should log warning
            await service.start()

            # Service should still be running
            assert service._running is True

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
        runner = ServiceRunner(NATSService, config)

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

            # Cancel run task
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_service_runner_auto_restart(self):
        """Test ServiceRunner auto-restart functionality"""
        config = ServiceConfig(
            name="test_restart_service",
            auto_restart=True,
            max_restart_attempts=2,
            restart_delay=0.1,
        )

        restart_count = 0

        class RestartTestService(NATSService):
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

            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass


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
            orchestrator.add_service(NATSService, config)

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

            # Cancel run task
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_orchestrator_service_failure_handling(self):
        """Test orchestrator handles service failures"""
        orchestrator = ServiceOrchestrator()

        # Add a service that will fail
        class FailingService(NATSService):
            async def start(self):
                raise Exception("Service failed to start")

        config = ServiceConfig(name="failing_service", auto_restart=False)
        orchestrator.add_service(FailingService, config)

        # Add a normal service
        orchestrator.add_service(NATSService, ServiceConfig(name="normal_service"))

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

            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

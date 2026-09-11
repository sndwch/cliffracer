"""
Unit tests for base service functionality
"""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc


class TestNatsService:
    """Test base NatsService class"""

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_service")

    @pytest.fixture
    def service(self, service_config):
        return CliffracerService(service_config)

    def test_service_initialization(self, service, service_config):
        """Test service initialization"""
        assert service.config == service_config
        assert service.nc is None
        assert service.js is None
        assert service.container._subscriptions == set()
        assert service._running is False
        assert service.container.registry.rpc_handlers == {}
        assert service.container.registry.event_handlers == {}

    def test_subject_matches(self, service):
        """Test subject matching with wildcards"""
        # Exact match
        assert service.container._subject_matches("test.subject", "test.subject")

        # Single wildcard
        assert service.container._subject_matches("test.*", "test.anything")
        assert not service.container._subject_matches("test.*", "test.anything.else")

        # Multi-level wildcard
        assert service.container._subject_matches("test.>", "test.anything")
        assert service.container._subject_matches("test.>", "test.anything.else")
        assert service.container._subject_matches("test.>", "test.anything.else.more")

        # No match
        assert not service.container._subject_matches("test.subject", "other.subject")
        assert not service.container._subject_matches("test.*", "other.anything")

    @pytest.mark.asyncio
    async def test_connection_callbacks(self, service):
        """Test NATS connection callbacks"""
        service.config.on_error = lambda e: setattr(service, "_test_error", e)
        service.config.on_disconnect = lambda: setattr(service, "_test_disconnect", True)
        service.config.on_connect = lambda: setattr(service, "_test_connect", True)

        test_err = Exception("test error")
        await service.container.connection._error_callback(test_err)
        assert getattr(service, "_test_error", None) is test_err

        await service.container.connection._disconnected_callback()
        assert getattr(service, "_test_disconnect", False) is True

        await service.container.connection._reconnected_callback()
        assert getattr(service, "_test_connect", False) is True

        await service.container.connection._closed_callback()


class TestExtendedService:
    """Test ExtendedService functionality"""

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_extended_service")

    @pytest.fixture
    def service(self, service_config):
        return CliffracerService(service_config)

    def test_extended_service_initialization(self, service, service_config):
        """Test extended service initialization"""
        assert service.config == service_config
        assert hasattr(service.container.registry, "rpc_specs")
        assert isinstance(service.container.registry.rpc_specs, dict)

    @pytest.mark.asyncio
    async def test_schema_validation_mixin_methods(self, service):
        """Ensure schema validation handler specs and subject matching work on base service."""
        # Test subject matching
        assert service.container._subject_matches("test.*", "test.subject")

        # Mock NATS connection for call_rpc test
        service.nc = AsyncMock()
        mock_response = AsyncMock()
        mock_response.data = json.dumps({"result": "test"}).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        # ValidationExtension reads rpc_specs for handler validation.
        assert hasattr(service.container.registry, "rpc_specs")
        assert isinstance(service.container.registry.rpc_specs, dict)


class TestServiceWithDecorators:
    """Test service with decorated methods"""

    class ServiceBaseSvc(CliffracerService):
        def __init__(self, config):
            super().__init__(config)
            self.call_log = []

        @rpc
        async def test_rpc_method(self, param1: str, param2: int = 0) -> dict[str, str]:
            self.call_log.append(f"rpc: {param1}, {param2}")
            return {"result": f"{param1}_{param2}"}

        @listener("test.events.*", fanout=True)
        async def test_event_handler(
            self,
            subject: str,
            user_id: str | None = None,
            event_data: str | None = None,
        ):
            kw = {}
            if user_id is not None:
                kw["user_id"] = user_id
            if event_data is not None:
                kw["event_data"] = event_data
            self.call_log.append(f"event: {subject}, {kw}")

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_decorated_service")

    @pytest.fixture
    def service(self, service_config):
        svc = self.ServiceBaseSvc(service_config)
        svc._discover_handlers()
        return svc

    def test_decorated_methods_registration(self, service):
        """Test that decorated methods are properly registered"""
        # Check RPC method registration
        assert "test_rpc_method" in service.container.registry.rpc_handlers
        # Compare the function objects, not bound methods
        assert (
            service.container.registry.rpc_handlers["test_rpc_method"].__name__ == "test_rpc_method"
        )
        assert hasattr(
            service.container.registry.rpc_handlers["test_rpc_method"], "_cliffracer_rpc"
        )

        # Check event handler registration
        assert "test.events.*" in service.container.registry.event_handlers
        assert (
            service.container.registry.event_handlers["test.events.*"].__name__
            == "test_event_handler"
        )
        assert hasattr(
            service.container.registry.event_handlers["test.events.*"], "_cliffracer_events"
        )

    @pytest.mark.asyncio
    async def test_rpc_call_execution(self, service):
        """Test RPC method execution"""
        # Call the RPC method directly
        result = await service.test_rpc_method("hello", 42)

        assert result == {"result": "hello_42"}
        assert "rpc: hello, 42" in service.call_log

    @pytest.mark.asyncio
    async def test_event_handler_execution(self, service):
        """Test event handler execution"""
        # Call the event handler directly
        await service.test_event_handler("test.events.user_created", user_id="123")

        expected_log = "event: test.events.user_created, {'user_id': '123'}"
        assert expected_log in service.call_log

    @pytest.mark.asyncio
    async def test_rpc_request_handling(self, service, test_helper):
        """Test RPC request handling via message"""
        # Create a mock message
        message = test_helper.create_mock_message(
            subject="test_decorated_service.rpc.test_rpc_method",
            data={"param1": "test", "param2": 123},
        )

        # Handle the RPC request
        await service.container._handle_rpc_request(message)

        # Check that response was sent
        assert message._response_sent

        # Parse the response
        response_data = json.loads(message.response_data.decode())
        assert "result" in response_data
        assert response_data["result"] == {"result": "test_123"}

    @pytest.mark.asyncio
    async def test_event_handling(self, service, test_helper):
        """Test event handling via message"""
        # Create a mock message
        message = test_helper.create_mock_message(
            subject="test.events.something", data={"event_data": "test"}
        )

        # Handle the event
        await service.container._handle_event(message)

        # Check that event was handled
        expected_log = "event: test.events.something, {'event_data': 'test'}"
        assert expected_log in service.call_log

    @pytest.mark.asyncio
    async def test_unknown_rpc_method(self, service, test_helper):
        """Test handling of unknown RPC method"""
        # Create a mock message for unknown method
        message = test_helper.create_mock_message(
            subject="test_decorated_service.rpc.unknown_method", data={}
        )

        # Handle the RPC request
        await service.container._handle_rpc_request(message)

        # Check that error response was sent
        assert message._response_sent

        # Parse the response
        response_data = json.loads(message.response_data.decode())
        assert "error" in response_data
        assert "Unknown method" in response_data["error"]

"""
Unit tests for base service functionality
"""

import json
from unittest.mock import AsyncMock

import pytest

from nats_service import BaseNATSService, ServiceConfig, event_handler, rpc
from nats_service_extended import ValidatedNATSService


class TestNatsService:
    """Test base NatsService class"""

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_service")

    @pytest.fixture
    def service(self, service_config):
        return BaseNATSService(service_config)

    def test_service_initialization(self, service, service_config):
        """Test service initialization"""
        assert service.config == service_config
        assert service.nc is None
        assert service.js is None
        assert service._subscriptions == set()
        assert service._running is False
        assert service._rpc_handlers == {}
        assert service._event_handlers == {}

    def test_subject_matches(self, service):
        """Test subject matching with wildcards"""
        # Exact match
        assert service._subject_matches("test.subject", "test.subject")

        # Single wildcard
        assert service._subject_matches("test.*", "test.anything")
        assert not service._subject_matches("test.*", "test.anything.else")

        # Multi-level wildcard
        assert service._subject_matches("test.>", "test.anything")
        assert service._subject_matches("test.>", "test.anything.else")
        assert service._subject_matches("test.>", "test.anything.else.more")

        # No match
        assert not service._subject_matches("test.subject", "other.subject")
        assert not service._subject_matches("test.*", "other.anything")

    @pytest.mark.asyncio
    async def test_connection_callbacks(self, service):
        """Test NATS connection callbacks"""
        # Test error callback
        await service._error_callback(Exception("test error"))

        # Test disconnected callback
        await service._disconnected_callback()

        # Test reconnected callback
        await service._reconnected_callback()

        # Test closed callback
        await service._closed_callback()

        # All callbacks should complete without error


class TestExtendedService:
    """Test ExtendedService functionality"""

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_extended_service")

    @pytest.fixture
    def service(self, service_config):
        return ValidatedNATSService(service_config)

    def test_extended_service_initialization(self, service, service_config):
        """Test extended service initialization"""
        assert service.config == service_config
        assert hasattr(service, "_broadcast_methods")

    @pytest.mark.asyncio
    async def test_schema_validation_mixin_methods(self, service):
        """Test schema validation mixin methods"""
        # Test subject matching (inherited from mixin)
        assert service._subject_matches("test.*", "test.subject")

        # Mock NATS connection for call_rpc test
        service.nc = AsyncMock()
        mock_response = AsyncMock()
        mock_response.data = json.dumps({"result": "test"}).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        # Test call_rpc_validated would work (needs actual implementation)
        # This tests the interface exists
        assert hasattr(service, "call_rpc_validated")


class TestServiceWithDecorators:
    """Test service with decorated methods"""

    class TestService(ValidatedNATSService):
        def __init__(self, config):
            super().__init__(config)
            self.call_log = []

        @rpc
        async def test_rpc_method(self, param1: str, param2: int = 0):
            self.call_log.append(f"rpc: {param1}, {param2}")
            return {"result": f"{param1}_{param2}"}

        @event_handler("test.events.*")
        async def test_event_handler(self, subject: str, **kwargs):
            self.call_log.append(f"event: {subject}, {kwargs}")

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_decorated_service")

    @pytest.fixture
    def service(self, service_config):
        return self.TestService(service_config)

    def test_decorated_methods_registration(self, service):
        """Test that decorated methods are properly registered"""
        # Check RPC method registration
        assert "test_rpc_method" in service._rpc_handlers
        assert service._rpc_handlers["test_rpc_method"] == service.test_rpc_method

        # Check event handler registration
        assert "test.events.*" in service._event_handlers
        assert service._event_handlers["test.events.*"] == service.test_event_handler

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
        await service._handle_rpc_request(message)

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
        await service._handle_event(message)

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
        await service._handle_rpc_request(message)

        # Check that error response was sent
        assert message._response_sent

        # Parse the response
        response_data = json.loads(message.response_data.decode())
        assert "error" in response_data
        assert "Unknown method" in response_data["error"]

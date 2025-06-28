"""
Unit tests for service decorators
"""

import pytest
from pydantic import Field

from cliffracer import (
    BroadcastMessage,
    RPCRequest,
    RPCResponse,
    broadcast,
    listener,
    rpc,
)
from cliffracer.core.base_service import event_handler
from cliffracer.core.extended_service import validated_rpc


# Test models for validation tests
class TestRequest(RPCRequest):
    """Test RPC request model"""

    name: str = Field(..., min_length=1)
    age: int = Field(..., ge=0, le=150)


class TestResponse(RPCResponse):
    """Test RPC response model"""

    message: str
    user_id: str


class TestBroadcast(BroadcastMessage):
    """Test broadcast message model"""

    event_type: str
    data: dict


class TestDecorators:
    """Test service decorators"""

    def test_rpc_decorator(self):
        """Test @rpc decorator"""

        @rpc
        async def test_method(self, param1: str, param2: int):
            return {"param1": param1, "param2": param2}

        # Check decorator attributes
        assert hasattr(test_method, "_is_rpc")
        assert test_method._is_rpc is True
        assert hasattr(test_method, "_rpc_name")
        assert test_method._rpc_name == "test_method"

    def test_event_handler_decorator(self):
        """Test @event_handler decorator"""

        @event_handler("test.events.*")
        async def test_handler(self, subject: str, **kwargs):
            return f"Handled {subject}"

        # Check decorator attributes
        assert hasattr(test_handler, "_is_event_handler")
        assert test_handler._is_event_handler is True
        assert hasattr(test_handler, "_event_pattern")
        assert test_handler._event_pattern == "test.events.*"

    def test_validated_rpc_decorator(self):
        """Test @validated_rpc decorator"""

        @validated_rpc(TestRequest, TestResponse)
        async def test_validated_method(self, request: TestRequest):
            return TestResponse(message=f"Hello {request.name}", user_id=f"user_{request.age}")

        # Check decorator attributes
        assert hasattr(test_validated_method, "_is_rpc")
        assert test_validated_method._is_rpc is True
        assert hasattr(test_validated_method, "_is_validated_rpc")
        assert test_validated_method._is_validated_rpc is True
        assert hasattr(test_validated_method, "_request_class")
        assert test_validated_method._request_class == TestRequest
        assert hasattr(test_validated_method, "_response_class")
        assert test_validated_method._response_class == TestResponse

    def test_broadcast_decorator(self):
        """Test @broadcast decorator"""

        @broadcast(TestBroadcast)
        async def test_broadcast_method(self, event_type: str, data: dict):
            return TestBroadcast(event_type=event_type, data=data, source_service="test_service")

        # Check decorator attributes
        assert hasattr(test_broadcast_method, "_is_broadcast")
        assert test_broadcast_method._is_broadcast is True
        assert hasattr(test_broadcast_method, "_broadcast_message_class")
        assert test_broadcast_method._broadcast_message_class == TestBroadcast
        assert hasattr(test_broadcast_method, "_broadcast_subject")
        assert test_broadcast_method._broadcast_subject == "broadcast.testbroadcast"

    def test_broadcast_decorator_custom_subject(self):
        """Test @broadcast decorator with custom subject"""

        @broadcast(TestBroadcast, subject="custom.broadcast.subject")
        async def test_custom_broadcast(self, event_type: str, data: dict):
            return TestBroadcast(event_type=event_type, data=data, source_service="test_service")

        assert test_custom_broadcast._broadcast_subject == "custom.broadcast.subject"

    def test_listener_decorator(self):
        """Test @listener decorator"""

        @listener(TestBroadcast)
        async def test_listener_method(self, message: TestBroadcast):
            return f"Received {message.event_type}"

        # Check decorator attributes
        assert hasattr(test_listener_method, "_is_event_handler")
        assert test_listener_method._is_event_handler is True
        assert hasattr(test_listener_method, "_is_listener")
        assert test_listener_method._is_listener is True
        assert hasattr(test_listener_method, "_message_class")
        assert test_listener_method._message_class == TestBroadcast
        assert hasattr(test_listener_method, "_event_pattern")
        assert test_listener_method._event_pattern == "broadcast.testbroadcast"

    def test_listener_decorator_custom_subject(self):
        """Test @listener decorator with custom subject"""

        @listener(TestBroadcast, subject="custom.listener.subject")
        async def test_custom_listener(self, message: TestBroadcast):
            return f"Received {message.event_type}"

        assert test_custom_listener._event_pattern == "custom.listener.subject"

    def test_multiple_decorators(self):
        """Test that decorators can be combined (though not recommended)"""

        @rpc
        @event_handler("test.subject")
        async def test_multi_decorated(self, **kwargs):
            return "multi"

        # Should have both decorator attributes
        assert hasattr(test_multi_decorated, "_is_rpc")
        assert hasattr(test_multi_decorated, "_is_event_handler")


class TestPydanticModels:
    """Test Pydantic model validation"""

    def test_rpc_request_validation(self):
        """Test RPC request validation"""
        # Valid request
        valid_request = TestRequest(name="John", age=30)
        assert valid_request.name == "John"
        assert valid_request.age == 30
        assert valid_request.timestamp is not None

    def test_rpc_request_validation_errors(self):
        """Test RPC request validation errors"""
        # Empty name should fail
        with pytest.raises(ValueError):
            TestRequest(name="", age=30)

        # Negative age should fail
        with pytest.raises(ValueError):
            TestRequest(name="John", age=-1)

        # Age too high should fail
        with pytest.raises(ValueError):
            TestRequest(name="John", age=200)

    def test_rpc_response_creation(self):
        """Test RPC response creation"""
        response = TestResponse(message="Hello", user_id="123")
        assert response.message == "Hello"
        assert response.user_id == "123"
        assert response.success is True
        assert response.error is None
        assert response.timestamp is not None

    def test_broadcast_message_creation(self):
        """Test broadcast message creation"""
        broadcast = TestBroadcast(
            event_type="user_created", data={"user_id": "123"}, source_service="user_service"
        )
        assert broadcast.event_type == "user_created"
        assert broadcast.data == {"user_id": "123"}
        assert broadcast.source_service == "user_service"
        assert broadcast.timestamp is not None

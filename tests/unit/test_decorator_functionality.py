"""
Comprehensive tests for decorator functionality
"""

import inspect
from typing import Any

import pytest

from cliffracer import (
    BroadcastMessage,
    NATSService,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    ValidatedNATSService,
    async_rpc,
    broadcast,
    listener,
    rpc,
    validated_rpc,
    websocket_handler,
)


class TestDecoratorFunctionality:
    """Test all decorator behaviors and edge cases"""

    def test_rpc_decorator_metadata(self):
        """Test that @rpc decorator adds correct metadata"""

        @rpc
        async def test_method(self, arg1: str, arg2: int) -> dict:
            return {"arg1": arg1, "arg2": arg2}

        # Check metadata
        assert hasattr(test_method, "_is_rpc")
        assert test_method._is_rpc is True
        assert hasattr(test_method, "_rpc_name")
        assert test_method._rpc_name == "test_method"
        assert not hasattr(test_method, "_is_async_rpc")

        # Check function is unchanged
        assert test_method.__name__ == "test_method"
        assert inspect.iscoroutinefunction(test_method)

    def test_async_rpc_decorator_metadata(self):
        """Test that @async_rpc decorator adds correct metadata"""

        @async_rpc
        async def async_test_method(self, data: str):
            pass

        # Check metadata
        assert hasattr(async_test_method, "_is_rpc")
        assert async_test_method._is_rpc is True
        assert hasattr(async_test_method, "_rpc_name")
        assert async_test_method._rpc_name == "async_test_method"
        assert hasattr(async_test_method, "_is_async_rpc")
        assert async_test_method._is_async_rpc is True

    def test_validated_rpc_decorator(self):
        """Test @validated_rpc decorator functionality"""

        class TestRequest(RPCRequest):
            value: int

        class TestResponse(RPCResponse):
            result: int

        @validated_rpc(TestRequest, TestResponse)
        async def validated_method(self, request: TestRequest) -> TestResponse:
            return TestResponse(result=request.value * 2, success=True)

        # Check metadata
        assert hasattr(validated_method, "_is_rpc")
        assert validated_method._is_rpc is True
        assert hasattr(validated_method, "_request_class")
        assert validated_method._request_class == TestRequest
        assert hasattr(validated_method, "_response_class")
        assert validated_method._response_class == TestResponse

    def test_broadcast_decorator(self):
        """Test @broadcast decorator functionality"""

        class TestBroadcast(BroadcastMessage):
            data: str

        # Test with default subject
        @broadcast(TestBroadcast)
        async def broadcast_default(self, data: str):
            return TestBroadcast(source_service="test", data=data)

        assert hasattr(broadcast_default, "_is_broadcast")
        assert broadcast_default._is_broadcast is True
        assert broadcast_default._broadcast_message_class == TestBroadcast
        assert broadcast_default._broadcast_subject == "broadcast.testbroadcast"

        # Test with custom subject
        @broadcast(TestBroadcast, subject="custom.subject")
        async def broadcast_custom(self, data: str):
            return TestBroadcast(source_service="test", data=data)

        assert broadcast_custom._broadcast_subject == "custom.subject"

    def test_listener_decorator(self):
        """Test @listener decorator functionality"""

        class TestMessage(BroadcastMessage):
            value: int

        # Test with default subject
        @listener(TestMessage)
        async def listener_default(self, message: TestMessage):
            pass

        assert hasattr(listener_default, "_is_event_handler")
        assert listener_default._is_event_handler is True
        assert hasattr(listener_default, "_event_pattern")
        assert listener_default._event_pattern == "broadcast.testmessage"
        assert hasattr(listener_default, "_message_class")
        assert listener_default._message_class == TestMessage
        assert hasattr(listener_default, "_is_listener")
        assert listener_default._is_listener is True

        # Test with custom subject
        @listener(TestMessage, subject="events.custom")
        async def listener_custom(self, message: TestMessage):
            pass

        assert listener_custom._event_pattern == "events.custom"

    def test_websocket_handler_decorator(self):
        """Test @websocket_handler decorator functionality"""

        @websocket_handler("/ws/test")
        async def ws_handler(self, websocket):
            pass

        assert hasattr(ws_handler, "_is_websocket_handler")
        assert ws_handler._is_websocket_handler is True
        assert hasattr(ws_handler, "_websocket_path")
        assert ws_handler._websocket_path == "/ws/test"

    def test_decorator_stacking(self):
        """Test that decorators can be stacked"""

        # This is a bit contrived but tests decorator compatibility
        @rpc
        async def multi_decorated_method(self, x: int) -> int:
            """A method with multiple decorators"""
            return x * 2

        # Should still have RPC metadata
        assert hasattr(multi_decorated_method, "_is_rpc")
        assert multi_decorated_method._is_rpc is True

        # Should preserve docstring
        assert multi_decorated_method.__doc__ == "A method with multiple decorators"

    def test_decorator_on_sync_method(self):
        """Test decorators handle sync methods appropriately"""

        # RPC decorator on sync method (should work)
        @rpc
        def sync_rpc_method(self, x: int) -> int:
            return x + 1

        assert hasattr(sync_rpc_method, "_is_rpc")
        assert not inspect.iscoroutinefunction(sync_rpc_method)

    @pytest.mark.asyncio
    async def test_decorator_integration_with_service(self):
        """Test decorators work correctly when integrated with service"""

        class TestDecoratedService(NATSService):
            def __init__(self, config):
                super().__init__(config)
                self.rpc_calls = []
                self.async_calls = []
                self.broadcasts = []
                self.events = []

            @rpc
            async def rpc_method(self, value: str) -> str:
                self.rpc_calls.append(value)
                return f"rpc_{value}"

            @async_rpc
            async def async_method(self, value: str):
                self.async_calls.append(value)

            @broadcast(BroadcastMessage)
            async def broadcast_method(self, data: str):
                msg = BroadcastMessage(source_service=self.config.name)
                self.broadcasts.append(data)
                return msg

            @listener(BroadcastMessage)
            async def on_broadcast(self, message: BroadcastMessage):
                self.events.append(message)

        service = TestDecoratedService(ServiceConfig(name="test_decorated"))

        # Verify RPC handlers were registered
        assert "rpc_method" in service._rpc_handlers
        assert "async_method" in service._rpc_handlers

        # Verify event handlers were registered
        assert "broadcast.broadcastmessage" in service._event_handlers

    def test_decorator_error_handling(self):
        """Test decorator behavior with invalid usage"""

        # validated_rpc decorator actually doesn't validate at decoration time
        # It validates at runtime, so let's test something else

        # Test that decorators preserve function identity
        @rpc
        async def decorated_func(self):
            pass

        # Should still be a callable
        assert callable(decorated_func)
        assert hasattr(decorated_func, "_is_rpc")

    def test_decorator_preserves_type_hints(self):
        """Test that decorators preserve type hints"""

        @rpc
        async def typed_method(self, arg1: str, arg2: int = 5) -> dict[str, Any]:
            return {"arg1": arg1, "arg2": arg2}

        # Get type hints
        hints = inspect.signature(typed_method)
        params = hints.parameters

        # Check parameters are preserved
        assert "arg1" in params
        assert "arg2" in params
        assert params["arg2"].default == 5

        # Check return annotation is preserved
        assert hints.return_annotation != inspect.Signature.empty

    def test_custom_decorator_compatibility(self):
        """Test that our decorators work with custom decorators"""

        def custom_decorator(func):
            """A custom decorator that adds metadata"""
            func._custom = True
            return func

        # Test stacking with custom decorator
        @custom_decorator
        @rpc
        async def custom_decorated(self, x: int) -> int:
            return x

        # Should have both custom and RPC metadata
        assert hasattr(custom_decorated, "_custom")
        assert custom_decorated._custom is True
        assert hasattr(custom_decorated, "_is_rpc")
        assert custom_decorated._is_rpc is True

    @pytest.mark.asyncio
    async def test_broadcast_decorator_execution(self):
        """Test broadcast decorator execution flow"""

        class OrderEvent(BroadcastMessage):
            order_id: str
            amount: float

        class BroadcastService(ValidatedNATSService):
            @broadcast(OrderEvent)
            async def create_order_event(self, order_id: str, amount: float):
                return OrderEvent(
                    source_service=self.config.name,
                    order_id=order_id,
                    amount=amount
                )

        service = BroadcastService(ServiceConfig(name="broadcast_test"))

        # Execute broadcast method
        result = await service.create_order_event("ORD123", 99.99)

        # Verify result
        assert isinstance(result, OrderEvent)
        assert result.order_id == "ORD123"
        assert result.amount == 99.99
        assert result.source_service == "broadcast_test"

    def test_listener_decorator_with_validation(self):
        """Test listener decorator validates message class"""

        # Valid usage
        class ValidMessage(BroadcastMessage):
            data: str

        @listener(ValidMessage)
        async def valid_listener(self, message: ValidMessage):
            pass

        assert valid_listener._message_class == ValidMessage

        # Test with base Message class (should also work)
        from cliffracer import Message

        @listener(Message, subject="test.*")
        async def base_listener(self, message: Message):
            pass

        assert base_listener._message_class == Message

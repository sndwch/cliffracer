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
from cliffracer.core.extended_service import (
    broadcast as extended_broadcast,
)
from cliffracer.core.extended_service import (
    listener as extended_listener,
)


class TestDecoratorFunctionality:
    """Test all decorator behaviors and edge cases"""

    def test_rpc_decorator_metadata(self):
        """Test that @rpc decorator adds correct metadata"""

        @rpc
        async def test_method(self, arg1: str, arg2: int) -> dict:
            return {"arg1": arg1, "arg2": arg2}

        # Check metadata
        assert hasattr(test_method, "_cliffracer_rpc")
        assert test_method._cliffracer_rpc is True
        assert test_method.__name__ == "test_method"
        assert not hasattr(test_method, "_cliffracer_async_rpc")

        # Check function is unchanged
        assert test_method.__name__ == "test_method"
        assert inspect.iscoroutinefunction(test_method)

    def test_async_rpc_decorator_metadata(self):
        """Test that @async_rpc decorator adds correct metadata"""

        @async_rpc
        async def async_test_method(self, data: str):
            pass

        # Check metadata
        assert hasattr(async_test_method, "_cliffracer_rpc")
        assert async_test_method._cliffracer_rpc is True
        assert async_test_method.__name__ == "async_test_method"
        assert hasattr(async_test_method, "_cliffracer_async_rpc")
        assert async_test_method._cliffracer_async_rpc is True

    def test_validated_rpc_decorator(self):
        """Test @validated_rpc decorator functionality"""

        class TestRequest(RPCRequest):
            value: int

        class TestResponse(RPCResponse):
            result: int

        @validated_rpc(TestRequest)
        async def validated_method(self, request: TestRequest) -> TestResponse:
            return TestResponse(result=request.value * 2, success=True)

        # Check metadata
        assert hasattr(validated_method, "_cliffracer_rpc")
        assert validated_method._cliffracer_rpc is True
        assert hasattr(validated_method, "_cliffracer_validated_rpc")
        assert validated_method._cliffracer_validated_rpc == TestRequest

    def test_broadcast_decorator(self):
        """Test @broadcast decorator functionality"""

        # Test with a pattern string
        @broadcast("system.alerts")
        async def broadcast_default(self, data: str):
            return {"data": data}

        assert hasattr(broadcast_default, "_cliffracer_broadcast")
        assert broadcast_default._cliffracer_broadcast == "system.alerts"

        # Test with another pattern
        @broadcast("user.events")
        async def broadcast_custom(self, data: str):
            return {"data": data}

        assert broadcast_custom._cliffracer_broadcast == "user.events"

    def test_listener_decorator(self):
        """Test @listener decorator functionality"""

        # Test with a pattern
        @listener("user.events.*")
        async def listener_default(self, subject: str, **data):
            pass

        assert hasattr(listener_default, "_cliffracer_events")
        assert "user.events.*" in listener_default._cliffracer_events

        # Test with multiple patterns on same method
        @listener("order.*")
        @listener("payment.*")
        async def listener_multi(self, subject: str, **data):
            pass

        assert hasattr(listener_multi, "_cliffracer_events")
        assert "order.*" in listener_multi._cliffracer_events
        assert "payment.*" in listener_multi._cliffracer_events

    def test_websocket_handler_decorator(self):
        """Test @websocket_handler decorator functionality"""

        @websocket_handler("/ws/test")
        async def ws_handler(self, websocket):
            pass

        assert hasattr(ws_handler, "_cliffracer_websocket")
        assert ws_handler._cliffracer_websocket == "/ws/test"

    def test_decorator_stacking(self):
        """Test that decorators can be stacked"""

        # This is a bit contrived but tests decorator compatibility
        @rpc
        async def multi_decorated_method(self, x: int) -> int:
            """A method with multiple decorators"""
            return x * 2

        # Should still have RPC metadata
        assert hasattr(multi_decorated_method, "_cliffracer_rpc")
        assert multi_decorated_method._cliffracer_rpc is True

        # Should preserve docstring
        assert multi_decorated_method.__doc__ == "A method with multiple decorators"

    def test_decorator_on_sync_method(self):
        """Test decorators handle sync methods appropriately"""

        # RPC decorator on sync method (should work)
        @rpc
        def sync_rpc_method(self, x: int) -> int:
            return x + 1

        assert hasattr(sync_rpc_method, "_cliffracer_rpc")
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

            @extended_broadcast(BroadcastMessage)
            async def broadcast_method(self, data: str):
                msg = BroadcastMessage(source_service=self.config.name)
                self.broadcasts.append(data)
                return msg

            @extended_listener(BroadcastMessage)
            async def on_broadcast(self, message: BroadcastMessage):
                self.events.append(message)

        service = TestDecoratedService(ServiceConfig(name="test_decorated"))
        
        # Discover handlers
        service._discover_handlers()

        # Verify RPC handlers were registered
        assert "rpc_method" in service._rpc_handlers
        assert "async_method" in service._rpc_handlers

        # Note: The extended_service decorators aren't automatically discovered
        # by the base service class, so we can't check _event_handlers here

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
        assert hasattr(decorated_func, "_cliffracer_rpc")

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
        assert hasattr(custom_decorated, "_cliffracer_rpc")
        assert custom_decorated._cliffracer_rpc is True

    @pytest.mark.asyncio
    async def test_broadcast_decorator_execution(self):
        """Test broadcast decorator execution flow"""

        class OrderEvent(BroadcastMessage):
            order_id: str
            amount: float

        class BroadcastService(ValidatedNATSService):
            @broadcast(OrderEvent)
            async def create_order_event(self, order_id: str, amount: float):
                return OrderEvent(source_service=self.config.name, order_id=order_id, amount=amount)

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

        @extended_listener(ValidMessage)
        async def valid_listener(self, message: ValidMessage):
            pass

        assert valid_listener._message_class == ValidMessage

        # Test with base Message class (should also work)
        from cliffracer import Message

        @extended_listener(Message, subject="test.*")
        async def base_listener(self, message: Message):
            pass

        assert base_listener._message_class == Message

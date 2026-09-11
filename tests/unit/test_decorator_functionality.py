"""
Comprehensive tests for decorator functionality
"""

import hashlib
import inspect
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cliffracer import (
    BroadcastMessage,
    CliffracerService,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    async_rpc,
    broadcast,
    listener,
    rpc,
)
from cliffracer.core.decorators import (
    broadcast as extended_broadcast,
)
from cliffracer.core.decorators import (
    listener as extended_listener,
)
from cliffracer.core.typed_rpc import build_handler_spec


class _Echoed(BaseModel):
    """The shape test_method returns: a str and an int, so not a dict[str, T]."""

    arg1: str
    arg2: int


class TestDecoratorFunctionality:
    """Test all decorator behaviors and edge cases"""

    def test_rpc_decorator_metadata(self):
        """Test that @rpc decorator adds correct metadata"""

        @rpc
        async def test_method(self, arg1: str, arg2: int) -> _Echoed:
            return _Echoed(arg1=arg1, arg2=arg2)

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
        async def async_test_method(self, data: str) -> None:
            pass

        # Check metadata
        assert hasattr(async_test_method, "_cliffracer_rpc")
        assert async_test_method._cliffracer_rpc is True
        assert async_test_method.__name__ == "async_test_method"
        assert hasattr(async_test_method, "_cliffracer_async_rpc")
        assert async_test_method._cliffracer_async_rpc is True

    def test_a_model_parameter_needs_no_decorator_of_its_own(self):
        """What `@validated_rpc(Schema)` used to declare, the annotation says.

        The decorator marked the handler AND recorded the schema separately.
        There is one marker now, and the schema is read from the parameter, so
        the two can no longer disagree about which model validates the payload.
        """

        class TestRequest(RPCRequest):
            value: int

        class TestResponse(RPCResponse):
            result: int

        class Svc:
            @rpc
            async def validated_method(self, request: TestRequest) -> TestResponse:
                return TestResponse(result=request.value * 2, success=True)

        assert Svc.validated_method._cliffracer_rpc is True

        spec = build_handler_spec("validated_method", Svc.validated_method, owner=Svc)
        req_hash = hashlib.sha256(
            json.dumps(TestRequest.model_json_schema(), sort_keys=True).encode()
        ).hexdigest()[:16]
        assert spec.params[0].ref == {
            "kind": "model",
            "module": TestRequest.__module__,
            "qualname": TestRequest.__qualname__,
            "schema_hash": req_hash,
        }
        with pytest.raises(ValidationError):
            spec.payload_model.model_validate({"request": {"value": 1}, "bogus": 2})

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
        @listener("user.events.*", fanout=True)
        async def listener_default(self, subject: str) -> None:
            pass

        assert hasattr(listener_default, "_cliffracer_events")
        assert "user.events.*" in listener_default._cliffracer_events

        # Test with multiple patterns on same method
        @listener("order.*", fanout=True)
        @listener("payment.*", fanout=True)
        async def listener_multi(self, subject: str) -> None:
            pass

        assert hasattr(listener_multi, "_cliffracer_events")
        assert "order.*" in listener_multi._cliffracer_events
        assert "payment.*" in listener_multi._cliffracer_events

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

        class TestDecoratedService(CliffracerService):
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
            async def async_method(self, value: str) -> None:
                self.async_calls.append(value)

            @extended_broadcast("system.alerts")
            async def broadcast_method(self, data: str):
                msg = BroadcastMessage(source_service=self.config.name)
                self.broadcasts.append(data)
                return msg

            @extended_listener("system.broadcasts", fanout=True)
            async def on_broadcast(self, message: BroadcastMessage):
                self.events.append(message)

        service = TestDecoratedService(ServiceConfig(name="test_decorated"))

        # Discover handlers
        service._discover_handlers()

        # Verify RPC handlers were registered
        assert "rpc_method" in service.container.registry.rpc_handlers
        assert "async_method" in service.container.registry.rpc_handlers

        # Both decorators register string subject keys on the service.
        assert "system.alerts" in service.container.registry.event_handlers
        assert "system.broadcasts" in service.container.registry.event_handlers
        assert all(isinstance(k, str) for k in service.container.registry.event_handlers)

    def test_decorator_error_handling(self):
        """Test decorator behavior with invalid usage"""

        # A decorator does not validate at decoration time; it validates at
        # runtime, from the annotations. So test something else.

        # Test that decorators preserve function identity
        @rpc
        async def decorated_func(self) -> None:
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

        class BroadcastService(CliffracerService):
            @broadcast("order.events.created")
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

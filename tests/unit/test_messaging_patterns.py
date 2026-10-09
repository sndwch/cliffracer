"""
Comprehensive tests for messaging patterns and communication
"""

import asyncio
import json
from unittest.mock import AsyncMock

import nats.errors
import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    async_rpc,
    listener,
    rpc,
)
from cliffracer.core.exceptions import RpcServerError, RpcTimeoutError
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit


def _published(svc: CliffracerService) -> list[tuple[str, bytes, dict]]:
    """What a service put on the wire through its (mock) connection: subject, bytes, headers."""
    return [
        (call.args[0], call.args[1], dict(call.kwargs.get("headers") or {}))
        for call in svc.nc.publish.call_args_list
    ]


async def _deliver(consumer: CliffracerService, wire: tuple[str, bytes, dict]) -> None:
    """Hand one published message to a consumer's own dispatcher, as the broker would."""
    subject, data, headers = wire
    await consumer.container._dispatch_event(
        MockMessage(subject=subject, data=data, headers=headers), raise_on_error=True
    )


async def _rpc(svc: CliffracerService, method: str, **payload) -> dict:
    """Send one RPC request through the service's own dispatcher; return the reply envelope.

    Calling the decorated method directly runs the function body and nothing
    else -- no discovery, no validation, no envelope -- so it cannot tell a
    working `@rpc` from a plain method.
    """
    svc._discover_handlers()
    msg = MockMessage(subject=f"{svc.config.name}.rpc.{method}", data=json.dumps(payload).encode())
    await svc.container._handle_rpc_request(msg)
    assert msg.responded_data is not None, "the dispatcher sent no reply"
    return json.loads(msg.responded_data)


class TestMessagingPatterns:
    """Test various messaging patterns in the framework"""

    @pytest.mark.asyncio
    async def test_rpc_request_response_pattern(self):
        """Test basic RPC request/response pattern"""

        class CalculatorService(CliffracerService):
            @rpc
            async def add(self, a: float, b: float) -> float:
                return a + b

            @rpc
            async def multiply(self, a: float, b: float) -> float:
                return a * b

        service = CalculatorService(ServiceConfig(name="calculator"))
        service.nc = AsyncMock()

        # Served: each request is routed by method name and answered in an envelope.
        assert (await _rpc(service, "add", a=5, b=3))["result"] == 8
        assert (await _rpc(service, "multiply", a=4, b=7))["result"] == 28

        # Called: the request goes out on the method's subject, carrying its arguments.
        mock_response = AsyncMock()
        mock_response.data = json.dumps({"result": 15}).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        result = await service.call_rpc("calculator", "add", a=10, b=5)
        assert result == 15
        assert service.nc.request.call_args.args[0] == "calculator.rpc.add"
        assert json.loads(service.nc.request.call_args.args[1])["a"] == 10

    @pytest.mark.asyncio
    async def test_async_fire_and_forget_pattern(self):
        """Test async (fire-and-forget) messaging pattern"""

        call_log = []

        class LoggingService(CliffracerService):
            @async_rpc
            async def log_event(self, event: str, level: str = "info") -> None:
                call_log.append({"event": event, "level": level})
                # No return value for async methods

        service = LoggingService(ServiceConfig(name="logger"))
        service.nc = AsyncMock()

        # Test async call
        await service.call_async("logger", "log_event", event="test_event", level="debug")

        # Verify publish was called (not request)
        service.nc.publish.assert_called_once()
        call_args = service.nc.publish.call_args
        assert "logger.async.log_event" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_broadcast_listener_pattern(self):
        """A broadcast one service publishes is delivered, through dispatch, to a fanout listener."""

        received_events = []

        class UserEvent(BaseModel):
            user_id: str
            action: str

        class EventProducer(CliffracerService):
            pass

        class EventConsumer(CliffracerService):
            @listener("user.events.announced", fanout=True)
            async def on_user_event(self, event: UserEvent):
                received_events.append({"user_id": event.user_id, "action": event.action})

        producer = EventProducer(ServiceConfig(name="producer"))
        consumer = EventConsumer(ServiceConfig(name="consumer"))
        producer.nc = AsyncMock()
        consumer._discover_handlers()

        await producer.broadcast_message("user.events.announced", user_id="user123", action="login")

        wire = _published(producer)
        assert [subject for subject, _, _ in wire] == ["user.events.announced"]
        # Declared fanout, so every replica of the consumer receives it.
        assert "user.events.announced" in consumer.container.registry.event_fanout

        await _deliver(consumer, wire[0])

        assert received_events == [{"user_id": "user123", "action": "login"}]

    @pytest.mark.asyncio
    async def test_a_model_parameter_is_the_schema(self):
        """A pydantic model annotation validates the payload; no decorator declares it"""

        class CreateUserRequest(RPCRequest):
            username: str
            email: str
            age: int

        class CreateUserResponse(RPCResponse):
            user_id: str
            username: str
            created: bool = True

        class UserService(CliffracerService):
            def __init__(self, config):
                super().__init__(config)
                self.user_count = 0

            @rpc
            async def create_user(self, request: CreateUserRequest) -> CreateUserResponse:
                self.user_count += 1
                return CreateUserResponse(
                    user_id=f"user_{self.user_count}", username=request.username, success=True
                )

        service = UserService(ServiceConfig(name="user_service"))

        valid = {"username": "testuser", "email": "test@example.com", "age": 25}
        reply = await _rpc(service, "create_user", request=valid)
        assert reply["success"] is True
        assert reply["result"]["user_id"] == "user_1"
        assert reply["result"]["username"] == "testuser"
        assert service.user_count == 1

        # The annotation is the schema: a payload that does not fit it never reaches the body.
        reply = await _rpc(service, "create_user", request={**valid, "age": "not a number"})
        assert reply["success"] is False
        assert reply["error"] == "validation failed"
        assert [d["loc"] for d in reply["details"]] == [["request", "age"]]
        assert service.user_count == 1

    @pytest.mark.asyncio
    async def test_multiple_listeners_pattern(self):
        """One published event reaches every service that listens on its subject."""

        class OrderEvent(BaseModel):
            order_id: str
            amount: float

        notifications = []
        analytics = []
        inventory = []

        class NotificationService(CliffracerService):
            @listener("order.events.created", fanout=True)
            async def on_order(self, event: OrderEvent):
                notifications.append(f"Order {event.order_id}: ${event.amount}")

        class AnalyticsService(CliffracerService):
            @listener("order.events.created", fanout=True)
            async def track_order(self, event: OrderEvent):
                analytics.append({"order_id": event.order_id, "amount": event.amount})

        class InventoryService(CliffracerService):
            @listener("order.events.created", fanout=True)
            async def update_inventory(self, event: OrderEvent):
                inventory.append(event.order_id)

        class OrderService(CliffracerService):
            pass

        order_svc = OrderService(ServiceConfig(name="order_service"))
        order_svc.nc = AsyncMock()
        consumers = [
            NotificationService(ServiceConfig(name="notifications")),
            AnalyticsService(ServiceConfig(name="analytics")),
            InventoryService(ServiceConfig(name="inventory")),
        ]
        for consumer in consumers:
            consumer._discover_handlers()
            assert "order.events.created" in consumer.container.registry.event_fanout

        await order_svc.publish_event("order.events.created", order_id="ORD123", amount=99.99)
        (wire,) = _published(order_svc)

        for consumer in consumers:
            await _deliver(consumer, wire)

        assert notifications == ["Order ORD123: $99.99"]
        assert analytics == [{"order_id": "ORD123", "amount": 99.99}]
        assert inventory == ["ORD123"]

    @pytest.mark.asyncio
    async def test_error_handling_in_rpc(self):
        """Test error handling in RPC calls"""

        class ErrorService(CliffracerService):
            @rpc
            async def divide(self, a: float, b: float) -> float:
                if b == 0:
                    raise ValueError("Division by zero")
                return a / b

        service = ErrorService(ServiceConfig(name="error_service"))
        service.nc = AsyncMock()

        # Test direct call with error
        with pytest.raises(ValueError, match="Division by zero"):
            await service.divide(10, 0)

        # Test RPC call with error response
        error_response = {
            "error": "Division by zero",
            "traceback": "...",
            "timestamp": "2023-01-01T00:00:00",
        }
        mock_response = AsyncMock()
        mock_response.data = json.dumps(error_response).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        # An envelope no recognised code or prefix names is the service's own fault: the typed
        # RpcServerError, with the subject in front, as the standalone client raises it.
        with pytest.raises(RpcServerError, match="error_service.rpc.divide: Division by zero"):
            await service.call_rpc("error_service", "divide", a=10, b=0)

    @pytest.mark.asyncio
    async def test_event_handler_subject_patterns(self):
        """Test event handler with various subject patterns"""

        events_received = []

        class MultiEventService(CliffracerService):
            @rpc
            async def get_events(self) -> int:
                return len(events_received)

            # Different event patterns
            @listener("orders.*", fanout=True)
            async def on_order_events(self, subject: str) -> None:
                events_received.append(("orders", subject))

            @listener("users.*.created", fanout=True)
            async def on_user_created(self, subject: str) -> None:
                events_received.append(("user_created", subject))

            @listener("system.>", fanout=True)
            async def on_system_events(self, subject: str) -> None:
                events_received.append(("system", subject))

        service = MultiEventService(ServiceConfig(name="multi_event"))
        service._discover_handlers()

        # Each pattern is judged by what it matches. The near misses are the half that can fail:
        # `*` is one token, `>` is one or more, and a pattern's literal tokens must all be there.
        deliveries = [
            ("orders.created", [("orders", "orders.created")]),
            ("orders.created.eu", []),
            ("orders", []),
            ("users.bob.created", [("user_created", "users.bob.created")]),
            ("users.bob.deleted", []),
            ("users.created", []),
            ("users.a.b.created", []),
            ("system.a", [("system", "system.a")]),
            ("system.a.b.c", [("system", "system.a.b.c")]),
            ("system", []),
        ]
        for subject, expected in deliveries:
            before = len(events_received)
            await service.container._dispatch_event(
                MockMessage(subject=subject, data=b"{}"), raise_on_error=True
            )
            assert events_received[before:] == expected, subject

    @pytest.mark.asyncio
    async def test_rpc_timeout_handling(self):
        """A NATS request timeout reaches the caller as the framework's own RpcTimeoutError.

        The broker's timeout is `nats.errors.TimeoutError`; a builtin `TimeoutError` would be
        a different class that the translation never matches, so the double raises the real one.
        """

        service = CliffracerService(ServiceConfig(name="timeout_test", request_timeout=0.1))
        service.nc = AsyncMock()
        service.nc.request = AsyncMock(side_effect=nats.errors.TimeoutError())

        with pytest.raises(RpcTimeoutError) as raised:
            await service.call_rpc("slow_service", "slow_method")

        assert isinstance(raised.value.__cause__, nats.errors.TimeoutError)
        assert "slow_service.slow_method" in str(raised.value)
        # The configured timeout is the one the request was made with.
        assert service.nc.request.call_args.kwargs["timeout"] == 0.1

    @pytest.mark.asyncio
    async def test_concurrent_rpc_calls(self):
        """Test handling concurrent RPC calls"""

        class ConcurrentService(CliffracerService):
            def __init__(self, config):
                super().__init__(config)
                self.call_count = 0
                self.active_calls = 0
                self.max_concurrent = 0

            @rpc
            async def concurrent_method(self, delay: float = 0.1) -> int:
                self.call_count += 1
                ticket = self.call_count
                self.active_calls += 1
                self.max_concurrent = max(self.max_concurrent, self.active_calls)

                await asyncio.sleep(delay)

                self.active_calls -= 1
                return ticket

        service = ConcurrentService(ServiceConfig(name="concurrent"))

        # Five requests in flight at once, each through the dispatcher.
        service._discover_handlers()
        messages = [
            MockMessage(
                subject="concurrent.rpc.concurrent_method",
                data=json.dumps({"delay": 0.05}).encode(),
            )
            for _ in range(5)
        ]
        await asyncio.gather(*(service.container._handle_rpc_request(m) for m in messages))

        replies = [json.loads(m.responded_data) for m in messages]
        assert all(r["success"] is True for r in replies), replies
        # Each caller got its own call's answer: 1..5, none repeated or dropped.
        assert sorted(r["result"] for r in replies) == [1, 2, 3, 4, 5]
        assert service.call_count == 5
        assert service.active_calls == 0
        assert service.max_concurrent == 5  # all five overlapped; none was serialised

    @pytest.mark.asyncio
    async def test_message_ordering_preservation(self):
        """Test that message ordering is preserved in RPC calls"""

        received_order = []

        class OrderedService(CliffracerService):
            @rpc
            async def process_ordered(self, sequence: int) -> int:
                received_order.append(sequence)
                return sequence

        service = OrderedService(ServiceConfig(name="ordered"))

        # Send messages in order, each through the dispatcher
        for i in range(10):
            reply = await _rpc(service, "process_ordered", sequence=i)
            assert reply["result"] == i

        # Verify order was preserved
        assert received_order == list(range(10))

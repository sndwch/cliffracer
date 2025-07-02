"""
Comprehensive tests for messaging patterns and communication
"""

import asyncio
import json
from unittest.mock import AsyncMock

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
)


class TestMessagingPatterns:
    """Test various messaging patterns in the framework"""

    @pytest.mark.asyncio
    async def test_rpc_request_response_pattern(self):
        """Test basic RPC request/response pattern"""

        class CalculatorService(NATSService):
            @rpc
            async def add(self, a: float, b: float) -> float:
                return a + b

            @rpc
            async def multiply(self, a: float, b: float) -> float:
                return a * b

        service = CalculatorService(ServiceConfig(name="calculator"))
        service.nc = AsyncMock()

        # Test direct method calls
        result = await service.add(5, 3)
        assert result == 8

        result = await service.multiply(4, 7)
        assert result == 28

        # Test RPC call simulation
        mock_response = AsyncMock()
        mock_response.data = json.dumps({"result": 15}).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        result = await service.call_rpc("calculator", "add", a=10, b=5)
        assert result == 15

    @pytest.mark.asyncio
    async def test_async_fire_and_forget_pattern(self):
        """Test async (fire-and-forget) messaging pattern"""

        call_log = []

        class LoggingService(NATSService):
            @async_rpc
            async def log_event(self, event: str, level: str = "info"):
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
        """Test broadcast/listener pattern"""

        received_events = []

        class UserEvent(BroadcastMessage):
            user_id: str
            action: str

        class EventProducer(ValidatedNATSService):
            @broadcast(UserEvent)
            async def announce_user_action(self, user_id: str, action: str):
                return UserEvent(source_service=self.config.name, user_id=user_id, action=action)

        class EventConsumer(ValidatedNATSService):
            @listener(UserEvent)
            async def on_user_event(self, event: UserEvent):
                received_events.append(
                    {
                        "user_id": event.user_id,
                        "action": event.action,
                        "source": event.source_service,
                    }
                )

        producer = EventProducer(ServiceConfig(name="producer"))
        consumer = EventConsumer(ServiceConfig(name="consumer"))

        # Mock NATS
        producer.nc = AsyncMock()
        consumer.nc = AsyncMock()

        # Test broadcast
        result = await producer.announce_user_action("user123", "login")
        assert isinstance(result, UserEvent)
        assert result.user_id == "user123"
        assert result.action == "login"

        # Simulate receiving the broadcast
        await consumer.on_user_event(result)

        assert len(received_events) == 1
        assert received_events[0]["user_id"] == "user123"
        assert received_events[0]["action"] == "login"

    @pytest.mark.asyncio
    async def test_validated_rpc_pattern(self):
        """Test validated RPC with Pydantic models"""

        class CreateUserRequest(RPCRequest):
            username: str
            email: str
            age: int

        class CreateUserResponse(RPCResponse):
            user_id: str
            username: str
            created: bool = True

        class UserService(ValidatedNATSService):
            def __init__(self, config):
                super().__init__(config)
                self.user_count = 0

            @validated_rpc(CreateUserRequest, CreateUserResponse)
            async def create_user(self, request: CreateUserRequest) -> CreateUserResponse:
                self.user_count += 1
                return CreateUserResponse(
                    user_id=f"user_{self.user_count}", username=request.username, success=True
                )

        service = UserService(ServiceConfig(name="user_service"))

        # Test with valid request
        request = CreateUserRequest(username="testuser", email="test@example.com", age=25)

        response = await service.create_user(request)
        assert isinstance(response, CreateUserResponse)
        assert response.user_id == "user_1"
        assert response.username == "testuser"
        assert response.success is True

    @pytest.mark.asyncio
    async def test_multiple_listeners_pattern(self):
        """Test multiple services listening to same event"""

        class OrderEvent(BroadcastMessage):
            order_id: str
            amount: float

        notifications = []
        analytics = []
        inventory = []

        class NotificationService(ValidatedNATSService):
            @listener(OrderEvent)
            async def on_order(self, event: OrderEvent):
                notifications.append(f"Order {event.order_id}: ${event.amount}")

        class AnalyticsService(ValidatedNATSService):
            @listener(OrderEvent)
            async def track_order(self, event: OrderEvent):
                analytics.append({"order_id": event.order_id, "amount": event.amount})

        class InventoryService(ValidatedNATSService):
            @listener(OrderEvent)
            async def update_inventory(self, event: OrderEvent):
                inventory.append(event.order_id)

        # Create services
        notif_svc = NotificationService(ServiceConfig(name="notifications"))
        analytics_svc = AnalyticsService(ServiceConfig(name="analytics"))
        inventory_svc = InventoryService(ServiceConfig(name="inventory"))

        # Create event
        order_event = OrderEvent(source_service="order_service", order_id="ORD123", amount=99.99)

        # Simulate all services receiving the event
        await notif_svc.on_order(order_event)
        await analytics_svc.track_order(order_event)
        await inventory_svc.update_inventory(order_event)

        # Verify all services processed the event
        assert len(notifications) == 1
        assert len(analytics) == 1
        assert len(inventory) == 1
        assert "ORD123" in notifications[0]
        assert analytics[0]["amount"] == 99.99
        assert inventory[0] == "ORD123"

    @pytest.mark.asyncio
    async def test_error_handling_in_rpc(self):
        """Test error handling in RPC calls"""

        class ErrorService(NATSService):
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

        # RPC call should raise an exception for error responses
        with pytest.raises(Exception, match="RPC Error: Division by zero"):
            await service.call_rpc("error_service", "divide", a=10, b=0)

    @pytest.mark.asyncio
    async def test_event_handler_subject_patterns(self):
        """Test event handler with various subject patterns"""

        events_received = []

        class MultiEventService(NATSService):
            @rpc
            async def get_events(self):
                return events_received

            # Different event patterns
            @listener(BroadcastMessage, subject="orders.*")
            async def on_order_events(self, msg: BroadcastMessage):
                events_received.append(("orders", msg.model_dump()))

            @listener(BroadcastMessage, subject="users.*.created")
            async def on_user_created(self, msg: BroadcastMessage):
                events_received.append(("user_created", msg.model_dump()))

            @listener(BroadcastMessage, subject="system.>")
            async def on_system_events(self, msg: BroadcastMessage):
                events_received.append(("system", msg.model_dump()))

        service = MultiEventService(ServiceConfig(name="multi_event"))

        # Verify event handlers were registered
        assert "orders.*" in service._event_handlers
        assert "users.*.created" in service._event_handlers
        assert "system.>" in service._event_handlers

    @pytest.mark.asyncio
    async def test_rpc_timeout_handling(self):
        """Test RPC timeout handling"""

        service = NATSService(ServiceConfig(name="timeout_test", request_timeout=0.1))
        service.nc = AsyncMock()

        # Simulate timeout
        service.nc.request = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(asyncio.TimeoutError):
            await service.call_rpc("slow_service", "slow_method")

    @pytest.mark.asyncio
    async def test_concurrent_rpc_calls(self):
        """Test handling concurrent RPC calls"""

        class ConcurrentService(NATSService):
            def __init__(self, config):
                super().__init__(config)
                self.call_count = 0
                self.active_calls = 0
                self.max_concurrent = 0

            @rpc
            async def concurrent_method(self, delay: float = 0.1):
                self.call_count += 1
                self.active_calls += 1
                self.max_concurrent = max(self.max_concurrent, self.active_calls)

                await asyncio.sleep(delay)

                self.active_calls -= 1
                return self.call_count

        service = ConcurrentService(ServiceConfig(name="concurrent"))

        # Make multiple concurrent calls
        tasks = [service.concurrent_method(0.05) for _ in range(5)]

        results = await asyncio.gather(*tasks)

        # Verify all calls completed
        assert len(results) == 5
        assert service.call_count == 5
        assert service.active_calls == 0
        assert service.max_concurrent > 1  # Should have had multiple concurrent calls

    @pytest.mark.asyncio
    async def test_message_ordering_preservation(self):
        """Test that message ordering is preserved in RPC calls"""

        received_order = []

        class OrderedService(NATSService):
            @rpc
            async def process_ordered(self, sequence: int):
                received_order.append(sequence)
                return sequence

        service = OrderedService(ServiceConfig(name="ordered"))

        # Send messages in order
        for i in range(10):
            result = await service.process_ordered(i)
            assert result == i

        # Verify order was preserved
        assert received_order == list(range(10))

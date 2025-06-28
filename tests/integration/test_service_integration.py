"""
Integration tests for Cliffracer services
"""

import asyncio

import pytest
from pydantic import Field

from cliffracer import (
    BroadcastMessage,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    ServiceRunner,
    ValidatedNATSService,
    broadcast,
    listener,
    validated_rpc,
)


# Test models
class CreateOrderRequest(RPCRequest):
    """Test order creation request"""

    customer_id: str = Field(..., min_length=1)
    items: list[str] = Field(..., min_items=1)
    total: float = Field(..., gt=0)


class CreateOrderResponse(RPCResponse):
    """Test order creation response"""

    order_id: str
    status: str


class OrderCreatedBroadcast(BroadcastMessage):
    """Test order created broadcast"""

    order_id: str
    customer_id: str
    total: float


# Test services
class TestOrderService(ValidatedNATSService):
    """Test order service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.orders = {}
        self.order_counter = 0

    @validated_rpc(CreateOrderRequest, CreateOrderResponse)
    async def create_order(self, request: CreateOrderRequest) -> CreateOrderResponse:
        """Create a new order"""
        self.order_counter += 1
        order_id = f"order_{self.order_counter}"

        order = {
            "id": order_id,
            "customer_id": request.customer_id,
            "items": request.items,
            "total": request.total,
            "status": "created",
        }

        self.orders[order_id] = order

        # Broadcast order created event
        await self.broadcast_order_created(order_id, request.customer_id, request.total)

        return CreateOrderResponse(order_id=order_id, status="created")

    @broadcast(OrderCreatedBroadcast)
    async def broadcast_order_created(self, order_id: str, customer_id: str, total: float):
        """Broadcast order created event"""
        return OrderCreatedBroadcast(
            order_id=order_id, customer_id=customer_id, total=total, source_service=self.config.name
        )


class TestNotificationService(ValidatedNATSService):
    """Test notification service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.notifications = []

    @listener(OrderCreatedBroadcast)
    async def on_order_created(self, message: OrderCreatedBroadcast):
        """Handle order created events"""
        notification = {
            "type": "order_created",
            "order_id": message.order_id,
            "customer_id": message.customer_id,
            "total": message.total,
            "message": f"Order {message.order_id} created for customer {message.customer_id}",
        }

        self.notifications.append(notification)


class TestIntegrationServices:
    """Integration tests for services working together"""

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_service_to_service_rpc(self):
        """Test RPC calls between services"""
        # Skip if NATS not available
        pytest.importorskip("nats")

        # Create services
        order_config = ServiceConfig(name="test_order_service", auto_restart=False)
        notification_config = ServiceConfig(name="test_notification_service", auto_restart=False)

        order_service = TestOrderService(order_config)
        notification_service = TestNotificationService(notification_config)

        try:
            # Start services
            await order_service.start()
            await notification_service.start()

            # Wait for services to be ready
            await asyncio.sleep(1)

            # Make RPC call from notification service to order service
            response = await notification_service.call_rpc(
                "test_order_service",
                "create_order",
                customer_id="customer_123",
                items=["item1", "item2"],
                total=99.99,
            )

            # Verify response
            assert "order_id" in response
            assert response["status"] == "created"

            # Verify order was created in order service
            order_id = response["order_id"]
            assert order_id in order_service.orders
            assert order_service.orders[order_id]["customer_id"] == "customer_123"

        finally:
            # Cleanup
            await order_service.stop()
            await notification_service.stop()

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_broadcast_and_listener(self):
        """Test broadcast and listener functionality"""
        pytest.importorskip("nats")

        # Create services
        order_config = ServiceConfig(name="test_order_service_2", auto_restart=False)
        notification_config = ServiceConfig(name="test_notification_service_2", auto_restart=False)

        order_service = TestOrderService(order_config)
        notification_service = TestNotificationService(notification_config)

        try:
            # Start services
            await order_service.start()
            await notification_service.start()

            # Wait for services to be ready
            await asyncio.sleep(1)

            # Create an order (which should trigger broadcast)
            response = await order_service.create_order(
                CreateOrderRequest(
                    customer_id="customer_456", items=["widget", "gadget"], total=149.99
                )
            )

            # Wait for broadcast to be processed
            await asyncio.sleep(1)

            # Verify notification was received
            assert len(notification_service.notifications) == 1

            notification = notification_service.notifications[0]
            assert notification["type"] == "order_created"
            assert notification["order_id"] == response.order_id
            assert notification["customer_id"] == "customer_456"
            assert notification["total"] == 149.99

        finally:
            # Cleanup
            await order_service.stop()
            await notification_service.stop()

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_validation_errors(self):
        """Test that validation errors are properly handled"""
        pytest.importorskip("nats")

        # Create client service for testing
        client_config = ServiceConfig(name="test_client", auto_restart=False)
        client_service = ValidatedNATSService(client_config)

        # Create order service
        order_config = ServiceConfig(name="test_order_service_3", auto_restart=False)
        order_service = TestOrderService(order_config)

        try:
            # Start services
            await client_service.start()
            await order_service.start()

            # Wait for services to be ready
            await asyncio.sleep(1)

            # Try to make invalid RPC call
            with pytest.raises(Exception) as exc_info:
                await client_service.call_rpc(
                    "test_order_service_3",
                    "create_order",
                    customer_id="",  # Invalid: empty string
                    items=[],  # Invalid: empty list
                    total=-10.0,  # Invalid: negative amount
                )

            # Should get validation error
            assert "Validation error" in str(exc_info.value) or "error" in str(exc_info.value)

        finally:
            # Cleanup
            await client_service.stop()
            await order_service.stop()

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_multiple_services_interaction(self):
        """Test complex interaction between multiple services"""
        pytest.importorskip("nats")

        # Create multiple services
        services = []
        configs = [
            ServiceConfig(name="test_order_service_multi", auto_restart=False),
            ServiceConfig(name="test_notification_service_multi", auto_restart=False),
            ServiceConfig(name="test_client_multi", auto_restart=False),
        ]

        order_service = TestOrderService(configs[0])
        notification_service = TestNotificationService(configs[1])
        client_service = ValidatedNATSService(configs[2])

        services = [order_service, notification_service, client_service]

        try:
            # Start all services
            for service in services:
                await service.start()

            # Wait for all services to be ready
            await asyncio.sleep(2)

            # Create multiple orders from client
            orders = []
            for i in range(3):
                response = await client_service.call_rpc(
                    "test_order_service_multi",
                    "create_order",
                    customer_id=f"customer_{i}",
                    items=[f"item_{i}_1", f"item_{i}_2"],
                    total=float(100 + i * 10),
                )
                orders.append(response)

            # Wait for all broadcasts to be processed
            await asyncio.sleep(2)

            # Verify all orders were created
            assert len(orders) == 3
            for i, order in enumerate(orders):
                assert order["status"] == "created"
                assert order["order_id"] in order_service.orders

            # Verify all notifications were received
            assert len(notification_service.notifications) == 3

            # Verify notification details
            for i, notification in enumerate(notification_service.notifications):
                assert notification["type"] == "order_created"
                assert notification["customer_id"] == f"customer_{i}"
                assert notification["total"] == float(100 + i * 10)

        finally:
            # Cleanup all services
            for service in services:
                await service.stop()


@pytest.mark.nats_required
class TestServiceRunner:
    """Test ServiceRunner integration"""

    @pytest.mark.asyncio
    async def test_service_runner_start_stop(self):
        """Test that ServiceRunner can start and stop services"""
        pytest.importorskip("nats")

        config = ServiceConfig(name="test_runner_service", auto_restart=False)
        runner = ServiceRunner(TestOrderService, config)

        # This is a basic test - in practice you'd need more sophisticated
        # testing for the runner's lifecycle management
        assert runner.service_class == TestOrderService
        assert runner.config == config

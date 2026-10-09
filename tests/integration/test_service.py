"""Integration tests for Cliffracer services, RPC handlers, and broadcast messages."""

import asyncio
import json
import time

import nats
import pytest
from pydantic import Field

from cliffracer import (
    CliffracerService,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    ServiceRunner,
    broadcast,
    listener,
    rpc,
)
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import RPCError, RpcNoRespondersError

pytestmark = pytest.mark.integration


async def until(condition, what: str, timeout: float = 10.0) -> None:
    """Wait for `condition`, bounded, so a miss fails by name instead of hanging."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


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


# Test services
class OrderSvc(CliffracerService):
    """Test order service"""

    def __init__(self, config: ServiceConfig, **kwargs):
        super().__init__(config, **kwargs)
        self.orders = {}
        self.order_counter = 0

    @rpc
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

        # Broadcast order created event. `@broadcast` marks a service that RECEIVES a broadcast, so
        # the producer does not carry it: a producer that did would subscribe to its own subject.
        await self.broadcast_message(
            "order.created",
            order_id=order_id,
            customer_id=request.customer_id,
            total=request.total,
        )

        return CreateOrderResponse(order_id=order_id, status="created")


class NotificationSvc(CliffracerService):
    """Test notification service"""

    def __init__(self, config: ServiceConfig, **kwargs):
        super().__init__(config, **kwargs)
        self.notifications = []

    @listener("order.created", fanout=True)
    async def on_order_created(
        self,
        order_id: str,
        customer_id: str,
        total: float,
        source_service: str = "",
    ) -> None:
        """Handle order created events"""
        notification = {
            "type": "order_created",
            "order_id": order_id,
            "customer_id": customer_id,
            "total": total,
            "message": f"Order {order_id} created for customer {customer_id}",
        }

        self.notifications.append(notification)


class AuditSvc(CliffracerService):
    """Test service that receives the same broadcast with `@broadcast` rather than `@listener`"""

    def __init__(self, config: ServiceConfig, **kwargs):
        super().__init__(config, **kwargs)
        self.heard: list[dict] = []

    @broadcast("order.created")
    async def on_order_created(
        self, subject: str, order_id: str, customer_id: str, total: float
    ) -> None:
        self.heard.append(
            {"subject": subject, "order_id": order_id, "customer_id": customer_id, "total": total}
        )


class TestIntegrationServices:
    """Integration tests for services working together"""

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_service_to_service_rpc(self):
        """Test RPC calls between services"""

        # Create services
        order_config = ServiceConfig(name="test_order_service", auto_restart=False)
        notification_config = ServiceConfig(name="test_notification_service", auto_restart=False)

        order_service = OrderSvc(order_config)
        notification_service = NotificationSvc(notification_config)

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
                request={
                    "customer_id": "customer_123",
                    "items": ["item1", "item2"],
                    "total": 99.99,
                },
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
        # Create services
        order_config = ServiceConfig(name="test_order_service_2", auto_restart=False)
        notification_config = ServiceConfig(name="test_notification_service_2", auto_restart=False)

        order_service = OrderSvc(order_config)
        notification_service = NotificationSvc(notification_config)

        dead_letters: list = []

        async def record_dead_letter(msg) -> None:
            dead_letters.append((msg.subject, json.loads(msg.data)))

        subscriptions = []
        try:
            # Start services
            await order_service.start()
            await notification_service.start()

            # Wait for services to be ready
            await asyncio.sleep(1)

            # Watch both services' dead-letter subjects for the whole exchange
            for config in (order_config, notification_config):
                subscriptions.append(
                    await notification_service.nc.subscribe(
                        HandlerDiscovery.dlq_subject(config), cb=record_dead_letter
                    )
                )
            await notification_service.nc.flush()

            # Create an order (which should trigger broadcast)
            response = await order_service.create_order(
                CreateOrderRequest(
                    customer_id="customer_456", items=["widget", "gadget"], total=149.99
                )
            )

            # Wait for broadcast to be processed
            await until(lambda: notification_service.notifications, "the notification")

            # Stopping the producer drains what its handlers are still running, so a dead letter
            # its own copy of the broadcast caused is on the wire before this flush returns.
            await order_service.stop()
            await notification_service.nc.flush()
            assert dead_letters == [], dead_letters

            # Verify notification was received
            assert len(notification_service.notifications) == 1

            notification = notification_service.notifications[0]
            assert notification["type"] == "order_created"
            assert notification["order_id"] == response.order_id
            assert notification["customer_id"] == "customer_456"
            assert notification["total"] == 149.99

        finally:
            # Cleanup
            for subscription in subscriptions:
                await subscription.unsubscribe()
            await order_service.stop()
            await notification_service.stop()

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_a_broadcast_handler_receives_the_broadcast_and_nothing_is_dead_lettered(self):
        """`@broadcast` is the receiving side: the handler is called from the wire, with the
        broadcast's fields, and no service dead-letters the message."""
        order_config = ServiceConfig(name="test_order_service_3", auto_restart=False)
        audit_config = ServiceConfig(name="test_audit_service_3", auto_restart=False)
        order_service = OrderSvc(order_config)
        audit_service = AuditSvc(audit_config)
        dead_letters: list = []

        async def record_dead_letter(msg) -> None:
            dead_letters.append((msg.subject, json.loads(msg.data)))

        # A third connection watches the dead-letter subjects, so it outlives both services.
        observer = await nats.connect(order_config.nats_url)
        try:
            for config in (order_config, audit_config):
                await observer.subscribe(
                    HandlerDiscovery.dlq_subject(config), cb=record_dead_letter
                )
            await observer.flush()
            await order_service.start()
            await audit_service.start()

            response = await order_service.create_order(
                CreateOrderRequest(customer_id="customer_789", items=["widget"], total=19.5)
            )
            await until(lambda: audit_service.heard, "the broadcast handler to be called")

            # Stopping both drains what their handlers are still running, so a dead letter either
            # caused is on the wire before this flush returns.
            await order_service.stop()
            await audit_service.stop()
            await observer.flush()

            assert len(audit_service.heard) == 1
            heard = audit_service.heard[0]
            assert heard["order_id"] == response.order_id
            assert heard["customer_id"] == "customer_789"
            assert heard["total"] == 19.5
            assert heard["subject"].endswith("order.created"), heard["subject"]
            assert dead_letters == [], dead_letters
        finally:
            await observer.close()
            await order_service.stop()
            await audit_service.stop()

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_validation_errors(self):
        """Test that validation errors are properly handled"""
        # Create client service for testing
        client_config = ServiceConfig(name="test_client", auto_restart=False)
        client_service = CliffracerService(client_config)

        # Create order service
        order_config = ServiceConfig(name="test_order_service_3", auto_restart=False)
        order_service = OrderSvc(order_config)

        try:
            # Start services
            await client_service.start()
            await order_service.start()

            # Wait for services to be ready
            await asyncio.sleep(1)

            # Try to make invalid RPC call
            with pytest.raises(RPCError) as exc_info:
                await client_service.call_rpc(
                    "test_order_service_3",
                    "create_order",
                    request={
                        "customer_id": "",  # Invalid: empty string
                        "items": [],  # Invalid: empty list
                        "total": -10.0,  # Invalid: negative amount
                    },
                )

            # Should get validation error, naming WHICH fields were rejected: the
            # per-field details are what a caller acts on, and a server that rejected
            # the call for another reason, or with no details, would not carry all three.
            assert "validation failed" in str(exc_info.value)
            rejected = {tuple(e["loc"])[-1] for e in exc_info.value.details}
            assert rejected == {"customer_id", "items", "total"}, exc_info.value.details

        finally:
            # Cleanup
            await client_service.stop()
            await order_service.stop()

    @pytest.mark.nats_required
    @pytest.mark.asyncio
    async def test_multiple_services_interaction(self):
        """Test complex interaction between multiple services"""
        # Create multiple services
        services = []
        configs = [
            ServiceConfig(name="test_order_service_multi", auto_restart=False),
            ServiceConfig(name="test_notification_service_multi", auto_restart=False),
            ServiceConfig(name="test_client_multi", auto_restart=False),
        ]

        order_service = OrderSvc(configs[0])
        notification_service = NotificationSvc(configs[1])
        client_service = CliffracerService(configs[2])

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
                    request={
                        "customer_id": f"customer_{i}",
                        "items": [f"item_{i}_1", f"item_{i}_2"],
                        "total": float(100 + i * 10),
                    },
                )
                orders.append(response)

            # Wait for all broadcasts to be processed
            await asyncio.sleep(2)

            # Verify all orders were created
            assert len(orders) == 3
            for _i, order in enumerate(orders):
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
    async def test_service_runner_records_its_class_and_config(self):
        """Construction only: the runner keeps the class and config it was given."""
        config = ServiceConfig(name="test_runner_service", auto_restart=False)
        runner = ServiceRunner(OrderSvc, config)

        assert runner.service_class == OrderSvc
        assert runner.config == config
        assert runner.service is None, "constructing a runner must not start the service"

    @pytest.mark.asyncio
    async def test_service_runner_start_stop(self, monkeypatch):
        """`run()` starts the service, the service answers an RPC, and a shutdown request stops it.

        The shutdown is requested through the event that a signal sets, not by sending a signal:
        `run()` installs SIGINT and SIGTERM handlers on the whole process, which would outlive the
        test, so they are replaced by a no-op here. What is read is the lifecycle `run()` drives.
        """
        from cliffracer.core.container import BrokerConnectionState
        from cliffracer.runners.orchestrator import RUNNER_OK

        runner = ServiceRunner(
            OrderSvc, ServiceConfig(name="test_runner_lifecycle", auto_restart=False)
        )
        monkeypatch.setattr(runner, "_setup_signal_handlers", lambda: None)
        caller = NotificationSvc(
            ServiceConfig(name="test_runner_lifecycle_caller", auto_restart=False)
        )
        order = {"customer_id": "customer_1", "items": ["item1"], "total": 9.5}

        run_task = asyncio.create_task(runner.run())
        try:
            await until(
                lambda: (
                    runner._successful_starts == 1
                    and runner.service is not None
                    and runner.service.broker_state is BrokerConnectionState.CONNECTED
                ),
                "the runner to start its service",
            )
            await caller.start()

            response = await caller.call_rpc("test_runner_lifecycle", "create_order", request=order)
            assert response["status"] == "created", response

            runner._shutdown_event.set()
            status = await asyncio.wait_for(run_task, timeout=15)

            assert status == RUNNER_OK
            assert runner.service.broker_state is not BrokerConnectionState.CONNECTED
            # nothing answers for it any more
            with pytest.raises(RpcNoRespondersError):
                await caller.call_rpc("test_runner_lifecycle", "create_order", request=order)
        finally:
            runner._shutdown_event.set()
            if not run_task.done():
                run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await caller.stop()

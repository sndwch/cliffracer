"""
Example services demonstrating Cliffracer
"""

import asyncio
import random
from datetime import datetime

from cliffracer_logging import LoggingConfig
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, ServiceOrchestrator, ServiceRunner, rpc
from cliffracer import listener as event_handler


class OrderItem(BaseModel):
    name: str
    quantity: int


class Order(BaseModel):
    id: str
    user_id: str
    items: list[OrderItem]
    total: float
    status: str
    created_at: str


class Availability(BaseModel):
    item: str
    requested: int
    available: int
    in_stock: bool


class ReservedItem(BaseModel):
    name: str
    quantity: int
    reserved: bool
    reason: str | None = None


class NotificationSent(BaseModel):
    sent: bool
    channel: str
    timestamp: str


class OrderNATSService(CliffracerService):
    """Example order processing service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.orders = {}

    @rpc
    async def create_order(self, user_id: str, items: list[OrderItem], total: float) -> Order:
        """Create a new order"""
        order_id = f"order_{random.randint(1000, 9999)}"

        order = Order(
            id=order_id,
            user_id=user_id,
            items=items,
            total=total,
            status="pending",
            created_at=datetime.now(datetime.UTC).isoformat(),
        )

        self.orders[order_id] = order

        # Publish order created event
        await self.publish_event("orders.created", order_id=order_id, user_id=user_id, total=total)

        return order

    @rpc
    async def get_order(self, order_id: str) -> Order | None:
        """Get order by ID"""
        return self.orders.get(order_id)

    @rpc
    async def update_status(self, order_id: str, status: str) -> Order:
        """Update order status"""
        if order_id not in self.orders:
            raise ValueError(f"Order {order_id} not found")

        self.orders[order_id].status = status

        # Publish status update event
        await self.publish_event(f"orders.status.{status}", order_id=order_id, status=status)

        return self.orders[order_id]

    # fanout=True ensures events are received and printed directly.
    @event_handler("payments.completed", fanout=True)
    async def handle_payment_completed(self, order_id: str = "") -> None:
        """Handle payment completion events"""
        print(f"Payment completed for order {order_id}")
        await self.update_status(order_id, "paid")


class InventoryService(CliffracerService):
    """Example inventory management service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.inventory = {"widget": 100, "gadget": 50, "doohickey": 25}

    @rpc
    async def check_availability(self, item: str, quantity: int) -> Availability:
        """Check if item is available"""
        available = self.inventory.get(item, 0)
        return Availability(
            item=item, requested=quantity, available=available, in_stock=available >= quantity
        )

    @rpc
    async def reserve_items(self, items: list[OrderItem]) -> list[ReservedItem]:
        """Reserve items from inventory"""
        reserved = []

        for item_data in items:
            item = item_data.name
            quantity = item_data.quantity

            if self.inventory.get(item, 0) >= quantity:
                self.inventory[item] -= quantity
                reserved.append(ReservedItem(name=item, quantity=quantity, reserved=True))
            else:
                reserved.append(
                    ReservedItem(
                        name=item,
                        quantity=quantity,
                        reserved=False,
                        reason="Insufficient stock",
                    )
                )

        return reserved

    @event_handler("orders.created", fanout=True)
    async def handle_order_created(self, order_id: str = "") -> None:
        """React to new orders by checking inventory"""
        print(f"New order created: {order_id}")
        # In a real system, we might auto-reserve items here


class NotificationService(CliffracerService):
    """Example notification service"""

    @event_handler("orders.*", fanout=True)
    async def handle_order_events(self, subject: str, order_id: str = "", status: str = "") -> None:
        """Handle all order-related events"""
        event_type = subject.split(".")[-1]
        print(f"[Notification] Order event '{event_type}': order_id={order_id}, status={status}")

        # In a real system, send emails, SMS, push notifications, etc.

    @event_handler("inventory.low", fanout=True)
    async def handle_low_inventory(self, item: str = "", quantity: int = 0) -> None:
        """Handle low inventory warnings"""
        print(f"[Alert] Low inventory for {item}: only {quantity} remaining")

    @rpc
    async def send_notification(
        self, user_id: str, message: str, channel: str = "email"
    ) -> NotificationSent:
        """Send a notification to a user"""
        print(f"Sending {channel} to user {user_id}: {message}")
        return NotificationSent(sent=True, channel=channel, timestamp=datetime.utcnow().isoformat())


async def test_services():
    """Test the services by making some calls"""
    # Create a test client service
    client_config = ServiceConfig(name="test_client")
    client = CliffracerService(client_config)

    await client.connect()

    # Wait for services to start
    await asyncio.sleep(2)

    try:
        # Create an order
        print("\n=== Creating Order ===")
        order = await client.call_rpc(
            "order_service",
            "create_order",
            user_id="user123",
            items=[
                {"name": "widget", "quantity": 2, "price": 10.0},
                {"name": "gadget", "quantity": 1, "price": 25.0},
            ],
            total=45.0,
        )
        print(f"Created order: {order}")

        # Check inventory
        print("\n=== Checking Inventory ===")
        availability = await client.call_rpc(
            "inventory_service", "check_availability", item="widget", quantity=5
        )
        print(f"Inventory check: {availability}")

        # Reserve items
        print("\n=== Reserving Items ===")
        reserved = await client.call_rpc(
            "inventory_service",
            "reserve_items",
            items=[{"name": "widget", "quantity": 2}, {"name": "gadget", "quantity": 1}],
        )
        print(f"Reserved items: {reserved}")

        # Update order status
        print("\n=== Updating Order Status ===")
        updated_order = await client.call_rpc(
            "order_service", "update_status", order_id=order["id"], status="processing"
        )
        print(f"Updated order: {updated_order}")

        # Send notification
        print("\n=== Sending Notification ===")
        notification = await client.call_rpc(
            "notification_service",
            "send_notification",
            user_id="user123",
            message="Your order is being processed!",
            channel="email",
        )
        print(f"Notification sent: {notification}")

        # Simulate payment completed event
        print("\n=== Simulating Payment Event ===")
        await client.publish_event("payments.completed", order_id=order["id"], amount=45.0)

        await asyncio.sleep(1)

    finally:
        await client.disconnect()


def run_single_service():
    """Example of running a single service"""
    LoggingConfig.configure(service_name="simple_service")

    config = ServiceConfig(name="order_service", auto_restart=True)

    runner = ServiceRunner(OrderNATSService, config)
    runner.run_forever()


def run_all_services():
    """Example of running multiple services together"""
    LoggingConfig.configure(service_name="simple_service")

    runner = ServiceOrchestrator()

    # Add all services
    runner.add_service(OrderNATSService, ServiceConfig(name="order_service", auto_restart=True))

    runner.add_service(InventoryService, ServiceConfig(name="inventory_service", auto_restart=True))

    runner.add_service(
        NotificationService, ServiceConfig(name="notification_service", auto_restart=True)
    )

    # Run everything
    runner.run_forever()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            # Run test client
            asyncio.run(test_services())
        elif sys.argv[1] == "order":
            # Run just order service
            run_single_service()
        else:
            print("Usage: python example_services.py [test|order|all]")
    else:
        # Run all services
        run_all_services()

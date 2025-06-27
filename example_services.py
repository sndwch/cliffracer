"""
Example services demonstrating the NATS microservice framework
"""

import asyncio
import random
from datetime import datetime

from nats_runner import ServiceOrchestrator, ServiceRunner, configure_logging
from nats_service import NATSService, ServiceConfig, event_handler, rpc


class OrderNATSService(NATSService):
    """Example order processing service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.orders = {}

    @rpc
    async def create_order(self, user_id: str, items: list, total: float):
        """Create a new order"""
        order_id = f"order_{random.randint(1000, 9999)}"

        order = {
            "id": order_id,
            "user_id": user_id,
            "items": items,
            "total": total,
            "status": "pending",
            "created_at": datetime.now(datetime.UTC).isoformat(),
        }

        self.orders[order_id] = order

        # Publish order created event
        await self.publish_event("orders.created", order_id=order_id, user_id=user_id, total=total)

        return order

    @rpc
    async def get_order(self, order_id: str):
        """Get order by ID"""
        return self.orders.get(order_id)

    @rpc
    async def update_status(self, order_id: str, status: str):
        """Update order status"""
        if order_id not in self.orders:
            raise ValueError(f"Order {order_id} not found")

        self.orders[order_id]["status"] = status

        # Publish status update event
        await self.publish_event(f"orders.status.{status}", order_id=order_id, status=status)

        return self.orders[order_id]

    @event_handler("payments.completed")
    async def handle_payment_completed(self, order_id: str, **kwargs):
        """Handle payment completion events"""
        print(f"Payment completed for order {order_id}")
        await self.update_status(order_id, "paid")


class InventoryService(NATSService):
    """Example inventory management service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.inventory = {"widget": 100, "gadget": 50, "doohickey": 25}

    @rpc
    async def check_availability(self, item: str, quantity: int):
        """Check if item is available"""
        available = self.inventory.get(item, 0)
        return {
            "item": item,
            "requested": quantity,
            "available": available,
            "in_stock": available >= quantity,
        }

    @rpc
    async def reserve_items(self, items: list):
        """Reserve items from inventory"""
        reserved = []

        for item_data in items:
            item = item_data["name"]
            quantity = item_data["quantity"]

            if self.inventory.get(item, 0) >= quantity:
                self.inventory[item] -= quantity
                reserved.append({"name": item, "quantity": quantity, "reserved": True})
            else:
                reserved.append(
                    {
                        "name": item,
                        "quantity": quantity,
                        "reserved": False,
                        "reason": "Insufficient stock",
                    }
                )

        return reserved

    @event_handler("orders.created")
    async def handle_order_created(self, order_id: str, **kwargs):
        """React to new orders by checking inventory"""
        print(f"New order created: {order_id}")
        # In a real system, we might auto-reserve items here


class NotificationService(NATSService):
    """Example notification service"""

    @event_handler("orders.*")
    async def handle_order_events(self, subject: str, **kwargs):
        """Handle all order-related events"""
        event_type = subject.split(".")[-1]
        print(f"[Notification] Order event '{event_type}': {kwargs}")

        # In a real system, send emails, SMS, push notifications, etc.

    @event_handler("inventory.low")
    async def handle_low_inventory(self, item: str, quantity: int, **kwargs):
        """Handle low inventory warnings"""
        print(f"[Alert] Low inventory for {item}: only {quantity} remaining")

    @rpc
    async def send_notification(self, user_id: str, message: str, channel: str = "email"):
        """Send a notification to a user"""
        print(f"Sending {channel} to user {user_id}: {message}")
        return {"sent": True, "channel": channel, "timestamp": datetime.utcnow().isoformat()}


async def test_services():
    """Test the services by making some calls"""
    # Create a test client service
    client_config = ServiceConfig(name="test_client")
    client = Service(client_config)

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
    configure_logging()

    config = ServiceConfig(
        name="order_service", nats_url="nats://localhost:4222", auto_restart=True
    )

    runner = ServiceRunner(OrderNATSService, config)
    runner.run_forever()


def run_all_services():
    """Example of running multiple services together"""
    configure_logging()

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

"""
Example demonstrating sync vs async RPC patterns in Cliffracer
"""

import asyncio
import time
from datetime import datetime

from cliffracer import ServiceOrchestrator, NATSService, ServiceConfig, async_rpc, rpc
from cliffracer.core.base_service import event_handler
from cliffracer.logging import LoggingConfig


class OrderNATSService(NATSService):
    """Service that processes orders with different calling patterns"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.orders = {}
        self.order_counter = 0

    @rpc
    async def create_order(self, customer_id: str, items: list[dict], total: float) -> dict:
        """Synchronous order creation - waits for response"""
        self.order_counter += 1
        order_id = f"order_{self.order_counter}"

        order = {
            "id": order_id,
            "customer_id": customer_id,
            "items": items,
            "total": total,
            "status": "created",
            "created_at": datetime.now(datetime.UTC).isoformat(),
        }

        self.orders[order_id] = order

        # Simulate some processing time
        await asyncio.sleep(0.1)

        print(f"Order {order_id} created for customer {customer_id}")
        return order

    @async_rpc
    async def update_order_status(self, order_id: str, status: str):
        """Async order status update - fire-and-forget"""
        if order_id in self.orders:
            self.orders[order_id]["status"] = status
            self.orders[order_id]["updated_at"] = datetime.now(datetime.UTC).isoformat()

            # Simulate some processing time
            await asyncio.sleep(0.5)

            print(f"Order {order_id} status updated to: {status}")

            # Could trigger other async operations here
            await self.publish_event("orders.status_updated", order_id=order_id, status=status)
        else:
            print(f"Order {order_id} not found for status update")

    @async_rpc
    async def send_order_confirmation(self, order_id: str, email: str):
        """Async email sending - fire-and-forget"""
        # Simulate email sending delay
        await asyncio.sleep(2.0)
        print(f"Confirmation email sent to {email} for order {order_id}")

    @rpc
    async def get_order(self, order_id: str) -> dict:
        """Synchronous order retrieval"""
        order = self.orders.get(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        return order


class InventoryService(NATSService):
    """Service that manages inventory with async notifications"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.inventory = {"widget": 100, "gadget": 50, "doohickey": 25}

    @rpc
    async def check_availability(self, items: list[dict]) -> dict:
        """Synchronous inventory check - returns availability"""
        results = {}
        for item in items:
            name = item["name"]
            quantity = item["quantity"]
            available = self.inventory.get(name, 0)

            results[name] = {
                "requested": quantity,
                "available": available,
                "in_stock": available >= quantity,
            }

        return results

    @rpc
    async def reserve_items(self, order_id: str, items: list[dict]) -> dict:
        """Synchronous item reservation - returns confirmation"""
        reserved = {}

        for item in items:
            name = item["name"]
            quantity = item["quantity"]

            if self.inventory.get(name, 0) >= quantity:
                self.inventory[name] -= quantity
                reserved[name] = quantity
                print(f"Reserved {quantity} {name} for order {order_id}")
            else:
                raise ValueError(f"Insufficient stock for {name}")

        # Async notification - don't wait for it
        await self.call_async(
            "notification_service", "inventory_reserved", order_id=order_id, items=reserved
        )

        return {"order_id": order_id, "reserved": reserved}

    @async_rpc
    async def restock_item(self, item_name: str, quantity: int):
        """Async restocking - fire-and-forget"""
        # Simulate restocking delay
        await asyncio.sleep(1.0)

        if item_name in self.inventory:
            self.inventory[item_name] += quantity
            print(f"Restocked {quantity} {item_name}. New total: {self.inventory[item_name]}")

            # Notify about restock
            await self.publish_event(
                "inventory.restocked",
                item_name=item_name,
                quantity=quantity,
                new_total=self.inventory[item_name],
            )


class NotificationService(NATSService):
    """Service that handles notifications asynchronously"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.notifications = []

    @async_rpc
    async def send_email(self, recipient: str, subject: str, message: str):
        """Async email sending - fire-and-forget"""
        # Simulate email sending
        await asyncio.sleep(1.5)

        notification = {
            "type": "email",
            "recipient": recipient,
            "subject": subject,
            "message": message,
            "sent_at": datetime.now(datetime.UTC).isoformat(),
        }

        self.notifications.append(notification)
        print(f"Email sent to {recipient}: {subject}")

    @async_rpc
    async def send_sms(self, phone: str, message: str):
        """Async SMS sending - fire-and-forget"""
        # Simulate SMS sending
        await asyncio.sleep(0.8)

        notification = {
            "type": "sms",
            "phone": phone,
            "message": message,
            "sent_at": datetime.now(datetime.UTC).isoformat(),
        }

        self.notifications.append(notification)
        print(f"SMS sent to {phone}: {message}")

    @async_rpc
    async def inventory_reserved(self, order_id: str, items: dict):
        """Handle inventory reservation notifications"""
        print(f"Notification: Items reserved for order {order_id}: {items}")

    @event_handler("orders.status_updated")
    async def on_order_status_updated(self, subject: str, order_id: str, status: str, **kwargs):
        """React to order status updates"""
        print(f"Notification: Order {order_id} status changed to {status}")

    @event_handler("inventory.restocked")
    async def on_inventory_restocked(self, subject: str, item_name: str, quantity: int, **kwargs):
        """React to inventory restocking"""
        print(f"Notification: {item_name} restocked with {quantity} units")


async def demonstrate_patterns():
    """Demonstrate sync vs async calling patterns"""
    print("=== Demonstrating Sync vs Async RPC Patterns ===\n")

    # Create a client service
    client_config = ServiceConfig(name="demo_client")
    client = Service(client_config)
    await client.connect()

    try:
        # Wait for services to start
        await asyncio.sleep(2)

        print("1. SYNCHRONOUS OPERATIONS (wait for response)")
        print("-" * 50)

        # Sync: Create order and wait for response
        start_time = time.time()
        order = await client.call_rpc(
            "order_service",
            "create_order",
            customer_id="customer_123",
            items=[{"name": "widget", "quantity": 2}, {"name": "gadget", "quantity": 1}],
            total=45.99,
        )
        end_time = time.time()
        print(f"Order created in {end_time - start_time:.2f}s: {order['id']}")

        # Sync: Check inventory and wait for response
        start_time = time.time()
        availability = await client.call_rpc(
            "inventory_service",
            "check_availability",
            items=[{"name": "widget", "quantity": 2}, {"name": "gadget", "quantity": 1}],
        )
        end_time = time.time()
        print(f"Inventory checked in {end_time - start_time:.2f}s: {availability}")

        # Sync: Reserve items and wait for confirmation
        start_time = time.time()
        reservation = await client.call_rpc(
            "inventory_service",
            "reserve_items",
            order_id=order["id"],
            items=[{"name": "widget", "quantity": 2}, {"name": "gadget", "quantity": 1}],
        )
        end_time = time.time()
        print(f"Items reserved in {end_time - start_time:.2f}s: {reservation}")

        print(f"\nOrder ID for async operations: {order['id']}")
        print("\n2. ASYNCHRONOUS OPERATIONS (fire-and-forget)")
        print("-" * 50)

        # Async: Update order status - don't wait
        start_time = time.time()
        await client.call_async(
            "order_service", "update_order_status", order_id=order["id"], status="processing"
        )
        end_time = time.time()
        print(f"Status update triggered in {end_time - start_time:.3f}s (async)")

        # Async: Send confirmation email - don't wait
        start_time = time.time()
        await client.call_async(
            "order_service",
            "send_order_confirmation",
            order_id=order["id"],
            email="customer@example.com",
        )
        end_time = time.time()
        print(f"Email sending triggered in {end_time - start_time:.3f}s (async)")

        # Async: Send notification - don't wait
        start_time = time.time()
        await client.call_async(
            "notification_service",
            "send_sms",
            phone="+1234567890",
            message=f"Your order {order['id']} is being processed!",
        )
        end_time = time.time()
        print(f"SMS sending triggered in {end_time - start_time:.3f}s (async)")

        # Async: Restock inventory - don't wait
        start_time = time.time()
        await client.call_async(
            "inventory_service", "restock_item", item_name="widget", quantity=50
        )
        end_time = time.time()
        print(f"Restocking triggered in {end_time - start_time:.3f}s (async)")

        print("\n3. MIXED PATTERN - CHECKING RESULTS")
        print("-" * 50)

        # Wait a bit for async operations to complete
        print("Waiting 3 seconds for async operations to complete...")
        await asyncio.sleep(3)

        # Sync: Get updated order status
        updated_order = await client.call_rpc("order_service", "get_order", order_id=order["id"])
        print(f"Final order status: {updated_order.get('status', 'unknown')}")

        print("\n4. PERFORMANCE COMPARISON")
        print("-" * 50)

        # Demonstrate performance difference
        print("Sequential sync calls:")
        start_time = time.time()
        for i in range(3):
            await client.call_rpc(
                "order_service",
                "create_order",
                customer_id=f"customer_{i}",
                items=[{"name": "widget", "quantity": 1}],
                total=10.0,
            )
        sync_time = time.time() - start_time
        print(f"3 sync order creations took: {sync_time:.2f}s")

        print("\nParallel async calls:")
        start_time = time.time()
        async_tasks = []
        for i in range(3):
            task = client.call_async(
                "notification_service",
                "send_email",
                recipient=f"user{i}@example.com",
                subject="Test",
                message="Hello!",
            )
            async_tasks.append(task)

        # Fire all async calls
        await asyncio.gather(*async_tasks)
        async_time = time.time() - start_time
        print(f"3 async email sends triggered in: {async_time:.3f}s")

        print(
            f"\nPerformance improvement: {sync_time / async_time:.1f}x faster for fire-and-forget operations"
        )

    finally:
        await client.disconnect()


def run_demo():
    """Run the demonstration"""
    LoggingConfig.configure()

    # Create service runners
    runner = ServiceOrchestrator()

    runner.add_service(OrderNATSService, ServiceConfig(name="order_service", auto_restart=True))

    runner.add_service(InventoryService, ServiceConfig(name="inventory_service", auto_restart=True))

    runner.add_service(
        NotificationService, ServiceConfig(name="notification_service", auto_restart=True)
    )

    print("Starting services...")
    print(
        "Run 'python example_async_patterns.py demo' in another terminal to see the demonstration"
    )

    runner.run_forever()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        # Run the demonstration
        asyncio.run(demonstrate_patterns())
    else:
        # Run the services
        run_demo()

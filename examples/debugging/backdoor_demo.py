#!/usr/bin/env python3
"""
Cliffracer Backdoor Demo

This example shows how to use the backdoor debugging feature.
Run this service, then connect to the backdoor to inspect it live.
"""

import asyncio
import random
from datetime import UTC, datetime

from cliffracer_backdoor import BackdoorExtension
from cliffracer_logging import LoggingConfig
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc


class OrderRequest(BaseModel):
    customer_id: str
    product_id: str
    quantity: int


class OrderResponse(BaseModel):
    order_id: str
    status: str
    total_amount: float
    timestamp: str


class ServiceStats(BaseModel):
    """What get_service_stats returns; uptime is 0 before start_time exists."""

    orders_processed: int
    total_revenue: float
    debug_mode: bool
    uptime_seconds: float


class BackdoorDemoService(CliffracerService):
    """
    Demo service showcasing backdoor debugging capabilities.
    """

    # port=0 asks the OS for a free one; the bound port is reported in the logs
    # and under this extension's name in /health.
    backdoor = BackdoorExtension(enabled=True, port=0)

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.orders_processed = 0
        self.total_revenue = 0.0
        self.debug_mode = False

    @rpc
    async def process_order(self, request: OrderRequest) -> OrderResponse:
        """Process an order - perfect for backdoor debugging."""

        # Simulate processing time
        processing_time = random.uniform(0.1, 0.5)
        await asyncio.sleep(processing_time)

        # Calculate order details
        base_price = random.uniform(10.0, 100.0)
        total_amount = base_price * request.quantity

        # Generate order ID
        order_id = f"order_{random.randint(1000, 9999)}"

        # Update service state (visible in backdoor)
        self.orders_processed += 1
        self.total_revenue += total_amount

        if self.debug_mode:
            print(f"[INFO] DEBUG: Processing order {order_id} for ${total_amount:.2f}")

        return OrderResponse(
            order_id=order_id,
            status="processed",
            total_amount=total_amount,
            timestamp=datetime.now(UTC).isoformat(),
        )

    @rpc
    async def get_service_stats(self) -> ServiceStats:
        """Get service statistics - useful for backdoor inspection."""
        return ServiceStats(
            orders_processed=self.orders_processed,
            total_revenue=self.total_revenue,
            debug_mode=self.debug_mode,
            uptime_seconds=(datetime.now(UTC) - self.start_time).total_seconds()
            if hasattr(self, "start_time")
            else 0,
        )

    async def connect(self):
        """Connect and mark start time."""
        self.start_time = datetime.now(UTC)
        await super().connect()
        print("[METRICS] Service statistics available via get_service_stats()")


async def simulate_orders(service: BackdoorDemoService):
    """Simulate incoming orders for testing."""

    customers = ["alice", "bob", "charlie", "diana", "eve"]
    products = ["widget", "gadget", "doohickey", "thingamajig", "whatsit"]

    while True:
        try:
            # Create random order
            request = OrderRequest(
                customer_id=random.choice(customers),
                product_id=random.choice(products),
                quantity=random.randint(1, 3),
            )

            # Process order
            response = await service.process_order(request)
            print(f"[INFO] Processed {response.order_id}: ${response.total_amount:.2f}")

            # Wait between orders
            await asyncio.sleep(random.uniform(2.0, 5.0))

        except Exception as e:
            print(f"[ERROR] Order simulation error: {e}")
            await asyncio.sleep(1.0)


async def main():
    """Run the backdoor demo service."""

    print("[INFO] Cliffracer Backdoor Demo Service")
    print("=" * 50)

    # Configure logging
    LoggingConfig.configure(service_name="backdoor_demo", log_level="INFO")

    # The backdoor console extension is declared directly on the service class.
    config = ServiceConfig(
        name="backdoor_demo",
    )

    # Create and start service
    service = BackdoorDemoService(config)

    try:
        # Connect to NATS (starts backdoor automatically)
        await service.connect()

        print()
        print("[INFO] BACKDOOR DEBUGGING DEMO")
        print("-" * 30)
        print("[OK] Service running with backdoor enabled")
        print("[INFO] Processing simulated orders every 2-5 seconds")
        print()
        print("[INFO]  DEBUGGING INSTRUCTIONS:")
        print("   1. Look for backdoor port in the logs above")
        print("   2. Connect: cliffracer-backdoor 127.0.0.1:<port>")
        print("   3. Try these commands in the backdoor:")
        print("      • inspect_service()     - See service details")
        print("      • inspect_nats()        - Check NATS connection")
        print("      • service.orders_processed  - Check order count")
        print("      • service.debug_mode = True  - Enable debug output")
        print("      • await service.get_service_stats()  - Get statistics")
        print("      • help_backdoor()       - See all commands")
        print()
        print("[NOTE] Use Ctrl+C to stop the service")
        print()

        # Start order simulation
        await simulate_orders(service)

    except KeyboardInterrupt:
        print("\\n[INFO] Shutting down demo service...")
    except Exception as e:
        print(f"[ERROR] Service error: {e}")
    finally:
        # Clean shutdown
        await service.disconnect()
        print("[OK] Service stopped")


if __name__ == "__main__":
    asyncio.run(main())

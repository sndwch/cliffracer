#!/usr/bin/env python3
"""
Correlation ID Propagation Example

This example demonstrates how correlation IDs flow through a microservices
architecture, enabling distributed request tracing across multiple services.
"""

import asyncio
import random
from datetime import UTC, datetime

from cliffracer_http import HttpExtension
from cliffracer_logging import setup_correlation_logging
from loguru import logger
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    listener,
    rpc,
    with_correlation_id,
)


# Pydantic models
class OrderRequest(BaseModel):
    product_id: str
    quantity: int
    customer_id: str


class PaymentRequest(BaseModel):
    order_id: str
    amount: float
    customer_id: str


class OrderResponse(BaseModel):
    order_id: str
    status: str
    correlation_id: str


# Order Service
class OrderResult(BaseModel):
    """Every branch create_order returns; error carries the exception text."""

    order_id: str
    status: str
    correlation_id: str | None = None
    total_price: float | None = None
    error: str | None = None


class Availability(BaseModel):
    product_id: str
    available: bool
    available_quantity: int
    requested_quantity: int


class PriceQuote(BaseModel):
    product_id: str
    quantity: int
    base_price: float
    subtotal: float
    discount_rate: float
    discount_amount: float
    total_price: float


class PaymentResult(BaseModel):
    success: bool
    payment_id: str
    order_id: str


class OrderService(CliffracerService):
    """
    Order service that processes orders and coordinates with other services.
    Demonstrates correlation ID propagation through service calls.
    """

    http = HttpExtension(port=8081)

    def __init__(self):
        config = ServiceConfig(name="order_service")
        super().__init__(config)

        self.orders = {}

    @http.post("/orders")
    @with_correlation_id
    async def create_order_http(self, order: OrderRequest, correlation_id: str = None):
        """HTTP endpoint for creating orders"""
        logger.info(f"HTTP order request received for customer {order.customer_id}")

        # Process order through RPC (will maintain correlation ID)
        result = await self.create_order(
            product_id=order.product_id,
            quantity=order.quantity,
            customer_id=order.customer_id,
            correlation_id=correlation_id,
        )

        return OrderResponse(
            order_id=result["order_id"], status=result["status"], correlation_id=correlation_id
        )

    @rpc
    async def create_order(
        self, product_id: str, quantity: int, customer_id: str, correlation_id: str = None
    ) -> OrderResult:
        """Create a new order and coordinate with other services"""
        logger.info(f"Creating order for product {product_id}, customer {customer_id}")

        order_id = f"ORD-{len(self.orders) + 1:04d}"

        try:
            # Check inventory (maintains correlation ID automatically)
            logger.info("Checking inventory...")
            inventory_result = await self.call_rpc(
                "inventory_service", "check_availability", product_id=product_id, quantity=quantity
            )

            if not inventory_result["available"]:
                logger.warning(f"Insufficient inventory for product {product_id}")
                return OrderResult(
                    order_id=order_id,
                    status="insufficient_inventory",
                    correlation_id=correlation_id,
                )

            # Calculate price
            logger.info("Calculating order price...")
            price_result = await self.call_rpc(
                "pricing_service",
                "calculate_price",
                product_id=product_id,
                quantity=quantity,
                customer_id=customer_id,
            )

            total_price = price_result["total_price"]

            # Process payment
            logger.info(f"Processing payment of ${total_price:.2f}...")
            payment_result = await self.call_rpc(
                "payment_service",
                "process_payment",
                order_id=order_id,
                amount=total_price,
                customer_id=customer_id,
            )

            if not payment_result["success"]:
                logger.error("Payment failed")
                return OrderResult(
                    order_id=order_id,
                    status="payment_failed",
                    correlation_id=correlation_id,
                )

            # Reserve inventory
            logger.info("Reserving inventory...")
            await self.call_async(
                "inventory_service",
                "reserve_inventory",
                product_id=product_id,
                quantity=quantity,
                order_id=order_id,
            )

            # Store order
            self.orders[order_id] = {
                "order_id": order_id,
                "product_id": product_id,
                "quantity": quantity,
                "customer_id": customer_id,
                "total_price": total_price,
                "status": "confirmed",
                "created_at": datetime.now(UTC).isoformat(),
                "correlation_id": correlation_id,
            }

            # Publish order confirmed event
            await self.publish_event(
                "orders.confirmed",
                order_id=order_id,
                customer_id=customer_id,
                total_price=total_price,
            )

            logger.info(f"Order {order_id} created successfully")

            return OrderResult(
                order_id=order_id,
                status="confirmed",
                total_price=total_price,
                correlation_id=correlation_id,
            )

        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return OrderResult(
                order_id=order_id,
                status="error",
                error=str(e),
                correlation_id=correlation_id,
            )

    @http.get("/orders/{order_id}")
    async def get_order(self, order_id: str):
        """Get order details"""
        if order_id not in self.orders:
            return {"error": "Order not found"}

        return self.orders[order_id]

    @listener("orders.events.*", fanout=True)
    async def handle_order_events(self, subject: str, correlation_id: str | None = None) -> None:
        """Handle order-related events"""
        logger.info(f"Received order event: {subject}")


# Inventory Service
class InventoryService(CliffracerService):
    """Inventory service that manages product availability"""

    http = HttpExtension(port=8082)

    def __init__(self):
        config = ServiceConfig(name="inventory_service")
        super().__init__(config)

        # Mock inventory
        self.inventory = {
            "PROD-001": 100,
            "PROD-002": 50,
            "PROD-003": 200,
        }
        self.reservations = {}

    @rpc
    async def check_availability(
        self, product_id: str, quantity: int, correlation_id: str = None
    ) -> Availability:
        """Check if product is available"""
        logger.info(f"Checking availability for {product_id}, quantity: {quantity}")

        available_quantity = self.inventory.get(product_id, 0)
        reserved_quantity = sum(
            r["quantity"] for r in self.reservations.values() if r["product_id"] == product_id
        )

        actual_available = available_quantity - reserved_quantity
        is_available = actual_available >= quantity

        logger.info(f"Product {product_id}: {actual_available} available, requested: {quantity}")

        return Availability(
            product_id=product_id,
            available=is_available,
            available_quantity=actual_available,
            requested_quantity=quantity,
        )

    @rpc
    async def reserve_inventory(
        self, product_id: str, quantity: int, order_id: str, correlation_id: str = None
    ) -> dict[str, str]:
        """Reserve inventory for an order"""
        logger.info(f"Reserving {quantity} units of {product_id} for order {order_id}")

        self.reservations[order_id] = {
            "product_id": product_id,
            "quantity": quantity,
            "order_id": order_id,
            "reserved_at": datetime.now(UTC).isoformat(),
        }

        # Publish inventory event
        await self.publish_event(
            "inventory.reserved", product_id=product_id, quantity=quantity, order_id=order_id
        )

        return {"status": "reserved", "order_id": order_id}


# Pricing Service
class PricingService(CliffracerService):
    """Pricing service that calculates order prices"""

    http = HttpExtension(port=8083)

    def __init__(self):
        config = ServiceConfig(name="pricing_service")
        super().__init__(config)

        # Mock pricing
        self.prices = {
            "PROD-001": 29.99,
            "PROD-002": 49.99,
            "PROD-003": 19.99,
        }
        self.customer_discounts = {
            "CUST-VIP": 0.10,  # 10% discount
            "CUST-GOLD": 0.05,  # 5% discount
        }

    @rpc
    async def calculate_price(
        self, product_id: str, quantity: int, customer_id: str, correlation_id: str = None
    ) -> PriceQuote:
        """Calculate total price with discounts"""
        logger.info(
            f"Calculating price for {product_id}, quantity: {quantity}, customer: {customer_id}"
        )

        base_price = self.prices.get(product_id, 0)
        subtotal = base_price * quantity

        # Apply customer discount
        discount_rate = self.customer_discounts.get(customer_id, 0)
        discount_amount = subtotal * discount_rate
        total_price = subtotal - discount_amount

        logger.info(
            f"Price calculation: base=${base_price:.2f}, "
            f"subtotal=${subtotal:.2f}, discount=${discount_amount:.2f}, "
            f"total=${total_price:.2f}"
        )

        return PriceQuote(
            product_id=product_id,
            quantity=quantity,
            base_price=base_price,
            subtotal=subtotal,
            discount_rate=discount_rate,
            discount_amount=discount_amount,
            total_price=total_price,
        )


# Payment Service
class PaymentService(CliffracerService):
    """Payment service that processes payments"""

    http = HttpExtension(port=8084)

    def __init__(self):
        config = ServiceConfig(name="payment_service")
        super().__init__(config)

        self.payments = {}

    @rpc
    async def process_payment(
        self, order_id: str, amount: float, customer_id: str, correlation_id: str = None
    ) -> PaymentResult:
        """Process payment for an order"""
        logger.info(f"Processing payment of ${amount:.2f} for order {order_id}")

        # Simulate payment processing with 90% success rate
        success = random.random() > 0.1

        payment_id = f"PAY-{len(self.payments) + 1:04d}"

        self.payments[payment_id] = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "customer_id": customer_id,
            "status": "completed" if success else "failed",
            "processed_at": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
        }

        # Publish payment event
        await self.publish_event(
            f"payments.{'completed' if success else 'failed'}",
            payment_id=payment_id,
            order_id=order_id,
            amount=amount,
        )

        if success:
            logger.info(f"Payment {payment_id} completed successfully")
        else:
            logger.error(f"Payment {payment_id} failed")

        return PaymentResult(success=success, payment_id=payment_id, order_id=order_id)


async def main():
    """
    Run the correlation ID example with multiple services
    """
    print("[INFO] Starting Correlation ID Propagation Example")
    print("=" * 60)

    # Setup correlation-aware logging for all services
    setup_correlation_logging("microservices_example", "INFO")

    # Create service instances
    order_service = OrderService()
    inventory_service = InventoryService()
    pricing_service = PricingService()
    payment_service = PaymentService()

    # Start all services
    services = [order_service, inventory_service, pricing_service, payment_service]

    try:
        print("\n[INFO] Starting services...")
        for service in services:
            await service.start()
            print(f"[OK] {service.config.name} started")

        print("\n[INFO] Available endpoints:")
        print("  • POST http://localhost:8081/orders - Create new order")
        print("  • GET  http://localhost:8081/orders/{order_id} - Get order details")
        print("  • GET  http://localhost:8081/health - Order service health")
        print("  • GET  http://localhost:8082/health - Inventory service health")
        print("  • GET  http://localhost:8083/health - Pricing service health")
        print("  • GET  http://localhost:8084/health - Payment service health")

        print("\n[INFO] Example order request:")
        print("""
curl -X POST http://localhost:8081/orders \\
  -H "Content-Type: application/json" \\
  -H "X-Correlation-ID: manual-test-123" \\
  -d '{
    "product_id": "PROD-001",
    "quantity": 2,
    "customer_id": "CUST-VIP"
  }'
        """)

        print("\n[INFO] Watch the logs to see correlation IDs flow through services!")
        print("[NOTE] Each log entry shows: timestamp | level | service | correlation_id | message")
        print(
            "\n[INFO] Notice how the same correlation ID appears across all services for a single request"
        )

        print("\n[INFO] Service is running! Press Ctrl+C to stop...")

        # Keep services running
        while True:
            await asyncio.sleep(60)

            # Show some stats
            print(f"\n[METRICS] Stats: {len(order_service.orders)} orders processed")

    except KeyboardInterrupt:
        print("\n\n[STOP]  Stopping services...")
    finally:
        for service in services:
            await service.stop()
            print(f"[OK] {service.config.name} stopped")


if __name__ == "__main__":
    asyncio.run(main())

"""
RpcProxy Example - Service Communication
========================================

Demonstrates calling dependent services via typed RpcProxy attributes:

    result = await self.inventory.check_availability(...)
"""

import asyncio

from pydantic import BaseModel

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, rpc


class Availability(BaseModel):
    product_id: str
    requested: int
    available: int
    in_stock: bool


class Reservation(BaseModel):
    """Both branches: a success carries the counts, a failure carries a reason."""

    status: str
    product_id: str | None = None
    reserved: int | None = None
    remaining: int | None = None
    reason: str | None = None


class PaymentResult(BaseModel):
    order_id: str
    amount: float
    payment_method: str
    status: str
    transaction_id: str


class Refund(BaseModel):
    transaction_id: str
    amount: float
    status: str


class OrderResult(BaseModel):
    """Every branch create_order returns: success, each failure, and the error."""

    status: str
    order_id: str | None = None
    reason: str | None = None
    product_id: str | None = None
    quantity: int | None = None
    amount: float | None = None
    transaction_id: str | None = None
    message: str | None = None


class InventoryService(CliffracerService):
    """Manages product inventory"""

    def __init__(self, health_port: int = 8010):
        config = ServiceConfig(name="inventory_service", health_port=health_port)
        super().__init__(config)
        # Simulated inventory database
        self.inventory = {"laptop": 10, "mouse": 100, "keyboard": 50}

    @rpc
    async def check_availability(self, product_id: str, quantity: int) -> Availability:
        """Check if we have enough stock"""
        available_qty = self.inventory.get(product_id, 0)
        return Availability(
            product_id=product_id,
            requested=quantity,
            available=available_qty,
            in_stock=available_qty >= quantity,
        )

    @rpc
    async def reserve_items(self, product_id: str, quantity: int) -> Reservation:
        """Reserve items for an order"""
        if self.inventory.get(product_id, 0) >= quantity:
            self.inventory[product_id] -= quantity
            return Reservation(
                status="success",
                product_id=product_id,
                reserved=quantity,
                remaining=self.inventory[product_id],
            )
        return Reservation(status="failed", reason="insufficient_stock")


class PaymentService(CliffracerService):
    """Handles payment processing"""

    def __init__(self, health_port: int = 8011):
        config = ServiceConfig(name="payment_service", health_port=health_port)
        super().__init__(config)

    @rpc
    async def process_payment(
        self, order_id: str, amount: float, payment_method: str = "credit_card"
    ) -> PaymentResult:
        """Process a payment"""
        self.logger.info(f"Processing ${amount:.2f} payment for order {order_id}")

        # Simulate payment processing
        await asyncio.sleep(0.1)

        return PaymentResult(
            order_id=order_id,
            amount=amount,
            payment_method=payment_method,
            status="completed",
            transaction_id=f"txn_{order_id}",
        )

    @rpc
    async def refund_payment(self, transaction_id: str, amount: float) -> Refund:
        """Refund a payment"""
        self.logger.info(f"Refunding ${amount:.2f} for transaction {transaction_id}")
        return Refund(transaction_id=transaction_id, amount=amount, status="refunded")


class NotificationService(CliffracerService):
    """Sends notifications to customers"""

    def __init__(self, health_port: int = 8012):
        config = ServiceConfig(name="notification_service", health_port=health_port)
        super().__init__(config)

    @rpc
    async def send_email(self, email: str, subject: str, body: str) -> dict[str, str]:
        """Send an email notification"""
        self.logger.info(f"Sending email to {email}: {subject}")
        # Simulate email sending
        await asyncio.sleep(0.05)
        return {"status": "sent", "email": email}

    @rpc
    async def send_sms(self, phone: str, message: str) -> dict[str, str]:
        """Send an SMS notification"""
        self.logger.info(f"Sending SMS to {phone}")
        return {"status": "sent", "phone": phone}


class OrderService(CliffracerService):
    """
    Orchestrates order creation using multiple services.

    Demonstrates calling dependent services via RpcProxy.
    """

    # Declare RpcProxy instances
    inventory = RpcProxy("inventory_service")
    payment = RpcProxy("payment_service")
    notifications = RpcProxy("notification_service")

    def __init__(self, health_port: int = 8013):
        config = ServiceConfig(name="order_service", health_port=health_port)
        super().__init__(config)
        self.orders = {}

    @rpc
    async def create_order(
        self, product_id: str, quantity: int, amount: float, customer_email: str
    ) -> OrderResult:
        """
        Create a new order via RPC proxies

        This method demonstrates calling multiple services cleanly.
        """
        order_id = f"order_{len(self.orders) + 1}"
        self.logger.info(f"Creating order {order_id} for {product_id} x{quantity}")

        try:
            # Step 1: Check inventory
            availability = await self.inventory.check_availability(
                product_id=product_id, quantity=quantity
            )

            if not availability["in_stock"]:
                self.logger.warning(f"Insufficient stock for {product_id}")
                return OrderResult(status="failed", reason="out_of_stock", order_id=order_id)

            # Step 2: Reserve the items
            reservation = await self.inventory.reserve_items(
                product_id=product_id, quantity=quantity
            )

            if reservation["status"] != "success":
                return OrderResult(status="failed", reason="reservation_failed", order_id=order_id)

            # Step 3: Process payment
            payment_result = await self.payment.process_payment(order_id=order_id, amount=amount)

            if payment_result["status"] != "completed":
                # Payment failed - would need compensation here
                return OrderResult(status="failed", reason="payment_failed", order_id=order_id)

            # Step 4: Send confirmation (fire-and-forget using call_async)
            await self.notifications.send_email.call_async(
                email=customer_email,
                subject=f"Order {order_id} Confirmed",
                body=f"Your order for {quantity}x {product_id} has been confirmed!",
            )

            # Save order
            self.orders[order_id] = {
                "order_id": order_id,
                "product_id": product_id,
                "quantity": quantity,
                "amount": amount,
                "customer_email": customer_email,
                "status": "completed",
                "payment": payment_result,
            }

            self.logger.info(f"Order {order_id} created successfully")

            return OrderResult(
                status="success",
                order_id=order_id,
                product_id=product_id,
                quantity=quantity,
                amount=amount,
                transaction_id=payment_result["transaction_id"],
            )

        except Exception as e:
            self.logger.error(f"Error creating order: {e}")
            return OrderResult(status="error", message=str(e))

    @rpc
    async def get_order(self, order_id: str) -> OrderResult:
        """Retrieve order details"""
        stored = self.orders.get(order_id)
        if stored is None:
            return OrderResult(status="not_found", order_id=order_id)
        return OrderResult(
            status=stored["status"],
            order_id=stored["order_id"],
            product_id=stored["product_id"],
            quantity=stored["quantity"],
            amount=stored["amount"],
            transaction_id=stored["payment"]["transaction_id"],
        )


async def main():
    """Run the example demonstrating the RpcProxy pattern."""
    print("=" * 70)
    print("RpcProxy Example - Clean Service Communication")
    print("=" * 70)

    # Each service self-configures; start them so their NATS subscriptions are live.
    inventory = InventoryService()
    payment = PaymentService()
    notifications = NotificationService()
    orders = OrderService()
    services = [inventory, payment, notifications, orders]

    for service in services:
        await service.start()

    try:
        print("\n[INFO] Starting order process...")
        result = await orders.create_order(
            product_id="laptop",
            quantity=2,
            amount=2000.0,
            customer_email="customer@example.com",
        )
        print(f"\n[OK] Order Result: {result}")

        if result.status == "success":
            order_details = await orders.get_order(result.order_id)
            print(f"\n[INFO] Order Details: {order_details}")

        print("\n\n[ERROR] Attempting to order 50 laptops (only 10 in stock)...")
        result2 = await orders.create_order(
            product_id="laptop",
            quantity=50,
            amount=50000.0,
            customer_email="customer@example.com",
        )
        print(f"Result: {result2}")

        print("\n" + "=" * 70)
        print("Demo completed!")
        print("=" * 70)
    finally:
        for service in reversed(services):
            await service.stop()


if __name__ == "__main__":
    """
    To run this example:
    1. Start NATS server:
       docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222

    2. Run this script:
       python examples/rpc_proxy_example.py
    """
    asyncio.run(main())

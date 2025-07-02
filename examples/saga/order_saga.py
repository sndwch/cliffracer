"""
E-commerce Order Saga Example

Demonstrates distributed transaction management for order processing
using both orchestration and choreography patterns.
"""

import asyncio

from pydantic import Field

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.database import Repository
from cliffracer.database.models import DatabaseModel
from cliffracer.patterns.saga import ChoreographySaga, SagaCoordinator, SagaParticipant, SagaStep


# Domain Models
class Order(DatabaseModel):
    __tablename__ = "orders"

    order_id: str = Field(..., description="Order ID")
    customer_id: str = Field(..., description="Customer ID")
    items: list[dict] = Field(..., description="Order items")
    total_amount: float = Field(..., description="Total amount")
    status: str = Field(default="pending", description="Order status")
    payment_id: str | None = Field(None, description="Payment ID")
    shipment_id: str | None = Field(None, description="Shipment ID")


class Payment(DatabaseModel):
    __tablename__ = "payments"

    payment_id: str = Field(..., description="Payment ID")
    order_id: str = Field(..., description="Order ID")
    amount: float = Field(..., description="Payment amount")
    status: str = Field(default="pending", description="Payment status")
    method: str = Field(..., description="Payment method")


class Inventory(DatabaseModel):
    __tablename__ = "inventory"

    product_id: str = Field(..., description="Product ID")
    quantity: int = Field(..., description="Available quantity")
    reserved: int = Field(default=0, description="Reserved quantity")


# Orchestration-based Order Saga

class OrderService(CliffracerService, SagaParticipant):
    """Order management service"""

    def __init__(self):
        config = ServiceConfig(name="order_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)

        self.orders = Repository(Order)

    def _register_handlers(self):
        """Register saga action handlers"""

        @self.rpc
        async def create_order(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Create a new order"""
            try:
                order = Order(
                    order_id=f"ORD-{saga_id[:8]}",
                    customer_id=data["customer_id"],
                    items=data["items"],
                    total_amount=data["total_amount"],
                    status="created"
                )

                created_order = await self.orders.create(order)

                return {
                    "result": {
                        "order_id": created_order.order_id,
                        "status": "created"
                    }
                }
            except Exception as e:
                return {"error": str(e)}

        @self.rpc
        async def cancel_order(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Cancel an order (compensation)"""
            try:
                order_id = original_result["order_id"]
                order = await self.orders.get_by_field("order_id", order_id)

                if order:
                    order.status = "cancelled"
                    await self.orders.update(order.id, order)

                return {"result": {"status": "cancelled"}}
            except Exception as e:
                return {"error": str(e)}

        @self.rpc
        async def confirm_order(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Confirm order completion"""
            try:
                order_id = data["create_order_result"]["order_id"]
                order = await self.orders.get_by_field("order_id", order_id)

                if order:
                    order.status = "confirmed"
                    order.payment_id = data["process_payment_result"]["payment_id"]
                    order.shipment_id = data["create_shipment_result"]["shipment_id"]
                    await self.orders.update(order.id, order)

                return {"result": {"status": "confirmed"}}
            except Exception as e:
                return {"error": str(e)}


class PaymentService(CliffracerService, SagaParticipant):
    """Payment processing service"""

    def __init__(self):
        config = ServiceConfig(name="payment_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)

        self.payments = Repository(Payment)

    def _register_handlers(self):
        """Register saga action handlers"""

        @self.rpc
        async def process_payment(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Process payment"""
            try:
                payment = Payment(
                    payment_id=f"PAY-{saga_id[:8]}",
                    order_id=data["create_order_result"]["order_id"],
                    amount=data["total_amount"],
                    status="completed",
                    method=data.get("payment_method", "credit_card")
                )

                created_payment = await self.payments.create(payment)

                # Simulate payment processing
                await asyncio.sleep(0.5)

                return {
                    "result": {
                        "payment_id": created_payment.payment_id,
                        "status": "completed"
                    }
                }
            except Exception as e:
                return {"error": str(e)}

        @self.rpc
        async def refund_payment(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Refund payment (compensation)"""
            try:
                payment_id = original_result["payment_id"]
                payment = await self.payments.get_by_field("payment_id", payment_id)

                if payment:
                    payment.status = "refunded"
                    await self.payments.update(payment.id, payment)

                return {"result": {"status": "refunded"}}
            except Exception as e:
                return {"error": str(e)}


class InventoryService(CliffracerService, SagaParticipant):
    """Inventory management service"""

    def __init__(self):
        config = ServiceConfig(name="inventory_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)

        self.inventory = Repository(Inventory)

    def _register_handlers(self):
        """Register saga action handlers"""

        @self.rpc
        async def reserve_inventory(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Reserve inventory items"""
            try:
                reserved_items = []

                for item in data["items"]:
                    product = await self.inventory.get_by_field("product_id", item["product_id"])

                    if not product or product.quantity < item["quantity"]:
                        return {"error": f"Insufficient inventory for {item['product_id']}"}

                    # Reserve inventory
                    product.quantity -= item["quantity"]
                    product.reserved += item["quantity"]
                    await self.inventory.update(product.id, product)

                    reserved_items.append({
                        "product_id": item["product_id"],
                        "quantity": item["quantity"]
                    })

                return {
                    "result": {
                        "reserved_items": reserved_items,
                        "status": "reserved"
                    }
                }
            except Exception as e:
                return {"error": str(e)}

        @self.rpc
        async def release_inventory(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Release reserved inventory (compensation)"""
            try:
                for item in original_result.get("reserved_items", []):
                    product = await self.inventory.get_by_field("product_id", item["product_id"])

                    if product:
                        product.quantity += item["quantity"]
                        product.reserved -= item["quantity"]
                        await self.inventory.update(product.id, product)

                return {"result": {"status": "released"}}
            except Exception as e:
                return {"error": str(e)}


class ShippingService(CliffracerService, SagaParticipant):
    """Shipping service"""

    def __init__(self):
        config = ServiceConfig(name="shipping_service")
        CliffracerService.__init__(self, config)
        SagaParticipant.__init__(self, self)

    def _register_handlers(self):
        """Register saga action handlers"""

        @self.rpc
        async def create_shipment(saga_id: str, correlation_id: str, step: str, data: dict) -> dict:
            """Create shipment"""
            try:
                # Simulate shipment creation
                shipment_id = f"SHIP-{saga_id[:8]}"

                return {
                    "result": {
                        "shipment_id": shipment_id,
                        "status": "created",
                        "tracking_number": f"TRK-{shipment_id}"
                    }
                }
            except Exception as e:
                return {"error": str(e)}

        @self.rpc
        async def cancel_shipment(saga_id: str, correlation_id: str, step: str, data: dict, original_result: dict) -> dict:
            """Cancel shipment (compensation)"""
            try:
                # Simulate shipment cancellation
                return {"result": {"status": "cancelled"}}
            except Exception as e:
                return {"error": str(e)}


class OrderSagaOrchestrator(CliffracerService):
    """Order saga orchestrator service"""

    def __init__(self):
        config = ServiceConfig(name="order_saga_orchestrator")
        super().__init__(config)

        self.coordinator = SagaCoordinator(self)

        # Define the order processing saga
        self.coordinator.define_saga("order_processing", [
            SagaStep(
                name="create_order",
                service="order_service",
                action="create_order",
                compensation="cancel_order",
                timeout=10.0
            ),
            SagaStep(
                name="reserve_inventory",
                service="inventory_service",
                action="reserve_inventory",
                compensation="release_inventory",
                timeout=15.0
            ),
            SagaStep(
                name="process_payment",
                service="payment_service",
                action="process_payment",
                compensation="refund_payment",
                timeout=30.0
            ),
            SagaStep(
                name="create_shipment",
                service="shipping_service",
                action="create_shipment",
                compensation="cancel_shipment",
                timeout=10.0
            ),
            SagaStep(
                name="confirm_order",
                service="order_service",
                action="confirm_order",
                compensation=None,  # Final step, no compensation
                timeout=10.0
            )
        ])

    @property
    def rpc(self):
        """RPC decorator"""
        return self._rpc_decorator

    def _rpc_decorator(self, func):
        """Register RPC handler"""
        self._rpc_handlers[func.__name__] = func
        return func

    @rpc
    async def place_order(self, customer_id: str, items: list, total_amount: float, payment_method: str = "credit_card") -> dict:
        """Place a new order using saga pattern"""
        result = await self.coordinator._start_saga("order_processing", {
            "customer_id": customer_id,
            "items": items,
            "total_amount": total_amount,
            "payment_method": payment_method
        })

        return result


# Choreography-based Order Saga (Alternative Implementation)

class OrderChoreographyService(CliffracerService):
    """Order service using choreography pattern"""

    def __init__(self):
        config = ServiceConfig(name="order_choreography_service")
        super().__init__(config)

        self.saga = ChoreographySaga(self)
        self.orders = Repository(Order)

        # Register saga event handlers
        self.saga.on_event("payment.completed")(self.saga.emits("order.payment_confirmed", "order.payment_failed")(self.handle_payment_completed))
        self.saga.on_event("shipment.created")(self.handle_shipment_created)
        self.saga.on_event("inventory.insufficient")(self.handle_inventory_insufficient)

    @property
    def rpc(self):
        """RPC decorator"""
        return self._rpc_decorator

    def _rpc_decorator(self, func):
        """Register RPC handler"""
        self._rpc_handlers[func.__name__] = func
        return func

    @rpc
    async def place_order(self, customer_id: str, items: list, total_amount: float) -> dict:
        """Initiate order placement"""
        # Create order and emit event
        order = Order(
            order_id=f"ORD-{self.saga.saga_id[:8]}",
            customer_id=customer_id,
            items=items,
            total_amount=total_amount,
            status="pending"
        )

        created_order = await self.orders.create(order)

        # Start the saga by emitting the first event
        await self.publish("order.created", {
            "saga_id": self.saga.saga_id,
            "order_id": created_order.order_id,
            "customer_id": customer_id,
            "items": items,
            "total_amount": total_amount
        })

        return {
            "order_id": created_order.order_id,
            "saga_id": self.saga.saga_id,
            "status": "processing"
        }

    async def handle_payment_completed(self, data: dict) -> dict:
        """Handle payment completion"""
        order = await self.orders.get_by_field("order_id", data["order_id"])
        if order:
            order.payment_id = data["payment_id"]
            order.status = "paid"
            await self.orders.update(order.id, order)

        return {"order_id": data["order_id"], "status": "paid"}

    async def handle_shipment_created(self, data: dict) -> dict:
        """Handle shipment creation"""
        order = await self.orders.get_by_field("order_id", data["order_id"])
        if order:
            order.shipment_id = data["shipment_id"]
            order.status = "shipped"
            await self.orders.update(order.id, order)

            # Emit final completion event
            await self.publish("order.completed", {
                "saga_id": data["saga_id"],
                "order_id": data["order_id"],
                "status": "completed"
            })

        return {"order_id": data["order_id"], "status": "shipped"}

    async def handle_inventory_insufficient(self, data: dict) -> dict:
        """Handle inventory shortage"""
        order = await self.orders.get_by_field("order_id", data["order_id"])
        if order:
            order.status = "cancelled"
            await self.orders.update(order.id, order)

            # Emit cancellation event
            await self.publish("order.cancelled", {
                "saga_id": data["saga_id"],
                "order_id": data["order_id"],
                "reason": "insufficient_inventory"
            })

        return {"order_id": data["order_id"], "status": "cancelled"}


# Example runner
async def run_orchestrated_saga():
    """Run the orchestrated saga example"""
    # Start all services
    services = [
        OrderService(),
        PaymentService(),
        InventoryService(),
        ShippingService(),
        OrderSagaOrchestrator()
    ]

    # Run services
    tasks = [asyncio.create_task(service.run()) for service in services]

    # Wait a bit for services to start
    await asyncio.sleep(2)

    # Place an order
    orchestrator = services[-1]
    result = await orchestrator.place_order(
        customer_id="CUST-123",
        items=[
            {"product_id": "PROD-1", "quantity": 2, "price": 29.99},
            {"product_id": "PROD-2", "quantity": 1, "price": 49.99}
        ],
        total_amount=109.97,
        payment_method="credit_card"
    )

    print(f"Order placed: {result}")

    # Keep running
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(run_orchestrated_saga())

"""
Tests for RpcProxy - Nameko-style service calling
"""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, rpc

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


class Availability(BaseModel):
    product_id: str
    quantity: int
    available: bool


class Reservation(BaseModel):
    product_id: str
    reserved: int
    status: str


class PaymentResult(BaseModel):
    order_id: str
    amount: float
    status: str


class OrderResult(BaseModel):
    """Both shapes create_order returns: the failure carries status and reason."""

    status: str
    reason: str | None = None
    product_id: str | None = None
    quantity: int | None = None
    amount: float | None = None
    payment: PaymentResult | None = None
    reservation: Reservation | None = None


class InventoryService(CliffracerService):
    """Mock inventory service for testing"""

    def __init__(self):
        config = ServiceConfig(name="inventory_service")
        super().__init__(config)
        self.reserved: list[dict[str, Any]] = []

    @rpc
    async def check_availability(self, product_id: str, quantity: int) -> Availability:
        """Check if product is available"""
        return Availability(product_id=product_id, quantity=quantity, available=quantity <= 100)

    @rpc
    async def reserve_items(self, product_id: str, quantity: int) -> Reservation:
        """Reserve items in inventory"""
        self.reserved.append({"product_id": product_id, "quantity": quantity})
        return Reservation(product_id=product_id, reserved=quantity, status="success")


class PaymentService(CliffracerService):
    """Mock payment service for testing"""

    def __init__(self):
        config = ServiceConfig(name="payment_service")
        super().__init__(config)

    @rpc
    async def process_payment(self, amount: float, order_id: str) -> PaymentResult:
        """Process a payment"""
        return PaymentResult(order_id=order_id, amount=amount, status="completed")


class OrderService(CliffracerService):
    """Service using RpcProxy to call other services"""

    # Nameko-style RpcProxy declarations
    inventory = RpcProxy("inventory_service")
    payment = RpcProxy("payment_service")

    def __init__(self):
        config = ServiceConfig(name="order_service")
        super().__init__(config)

    @rpc
    async def create_order(self, product_id: str, quantity: int, amount: float) -> OrderResult:
        """Create an order using RpcProxy to call other services"""
        # Check inventory using clean proxy syntax
        availability = await self.inventory.check_availability(
            product_id=product_id, quantity=quantity
        )

        if not availability["available"]:
            return OrderResult(status="failed", reason="out_of_stock")

        # Reserve items
        reservation = await self.inventory.reserve_items(product_id=product_id, quantity=quantity)

        # Process payment
        payment_result = await self.payment.process_payment(
            amount=amount, order_id=f"order_{product_id}"
        )

        return OrderResult(
            status="success",
            product_id=product_id,
            quantity=quantity,
            amount=amount,
            payment=payment_result,
            reservation=reservation,
        )


@pytest.mark.asyncio
@pytest.mark.nats_required
class TestRpcProxy:
    """Test suite for RpcProxy functionality"""

    async def test_rpc_proxy_basic_call(self, nats_connection):
        """Test basic RpcProxy call to another service"""
        # Start services
        inventory_service = InventoryService()
        order_service = OrderService()

        await inventory_service.start()
        await order_service.start()

        try:
            # Make RPC call using proxy
            result = await order_service.inventory.check_availability(
                product_id="widget", quantity=50
            )

            assert result["product_id"] == "widget"
            assert result["quantity"] == 50
            assert result["available"] is True

        finally:
            await order_service.stop()
            await inventory_service.stop()

    async def test_rpc_proxy_multiple_services(self, nats_connection):
        """Test calling multiple services via RpcProxy"""
        # Start all services
        inventory_service = InventoryService()
        payment_service = PaymentService()
        order_service = OrderService()

        await inventory_service.start()
        await payment_service.start()
        await order_service.start()

        try:
            # Create order - internally calls both inventory and payment services
            result = await order_service.create_order(
                product_id="laptop", quantity=2, amount=2000.0
            )

            # Attribute access, not subscript: this call is IN-PROCESS, so it
            # returns the OrderResult the handler declares. The dict shape is
            # what a caller sees over NATS -- test_rpc_proxy_basic_call, which
            # goes through the proxy, still reads keys.
            assert result.status == "success"
            assert result.product_id == "laptop"
            assert result.quantity == 2
            assert result.payment.status == "completed"
            assert result.reservation.status == "success"

        finally:
            await order_service.stop()
            await payment_service.stop()
            await inventory_service.stop()

    async def test_rpc_proxy_out_of_stock(self, nats_connection):
        """Test RpcProxy handles business logic correctly"""
        inventory_service = InventoryService()
        order_service = OrderService()

        await inventory_service.start()
        await order_service.start()

        try:
            # Try to order more than available (>100)
            result = await order_service.create_order(
                product_id="rare_item", quantity=150, amount=5000.0
            )

            assert result.status == "failed"
            assert result.reason == "out_of_stock"

        finally:
            await order_service.stop()
            await inventory_service.stop()

    async def test_rpc_proxy_fire_and_forget(self, nats_connection):
        """Verify RpcProxy call_async executes on the receiver without awaiting a reply."""
        inventory_service = InventoryService()
        order_service = OrderService()

        await inventory_service.start()
        await order_service.start()

        try:
            # Fire-and-forget call using call_async returns immediately
            await order_service.inventory.reserve_items.call_async(product_id="widget", quantity=10)

            # Await delivery to receiver state
            delivered = False
            for _ in range(20):
                if len(inventory_service.reserved) > 0:
                    delivered = True
                    break
                await asyncio.sleep(0.1)

            assert delivered is True, "Fire-and-forget RPC was not received by InventoryService"
            assert inventory_service.reserved == [{"product_id": "widget", "quantity": 10}]

        finally:
            await order_service.stop()
            await inventory_service.stop()

    async def test_rpc_proxy_multiple_instances(self, nats_connection):
        """Test that each service instance gets its own proxy, and that each routes on its own"""
        inventory_service = InventoryService()
        order_service1 = OrderService()
        order_service2 = OrderService()

        # Proxies should be different instances but work the same
        assert order_service1.inventory is not order_service2.inventory
        # Against the literal the descriptor was declared with, not against the other proxy,
        # which would agree for any value the descriptor propagated.
        assert order_service1.inventory._service_name == "inventory_service"
        assert order_service2.inventory._service_name == "inventory_service"
        # Each proxy belongs to the service instance it was read from.
        assert order_service1.inventory._service_instance() is order_service1
        assert order_service2.inventory._service_instance() is order_service2

        await inventory_service.start()
        await order_service1.start()
        await order_service2.start()
        try:
            for quantity, order_service in ((5, order_service1), (500, order_service2)):
                availability = await order_service.inventory.check_availability(
                    product_id="widget", quantity=quantity
                )
                assert availability["quantity"] == quantity
                assert availability["available"] is (quantity <= 100)
        finally:
            await order_service2.stop()
            await order_service1.stop()
            await inventory_service.stop()

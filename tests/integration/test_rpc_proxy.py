"""
Tests for RpcProxy - Nameko-style service calling
"""

import asyncio

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, rpc

pytestmark = pytest.mark.integration


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

    @rpc
    async def check_availability(self, product_id: str, quantity: int) -> Availability:
        """Check if product is available"""
        return Availability(product_id=product_id, quantity=quantity, available=quantity <= 100)

    @rpc
    async def reserve_items(self, product_id: str, quantity: int) -> Reservation:
        """Reserve items in inventory"""
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
        """Test RpcProxy call_async for fire-and-forget calls"""
        inventory_service = InventoryService()
        order_service = OrderService()

        await inventory_service.start()
        await order_service.start()

        try:
            # Fire-and-forget call using call_async
            await order_service.inventory.reserve_items.call_async(product_id="widget", quantity=10)

            # Give it a moment to process
            await asyncio.sleep(0.1)

            # No exception means success (fire-and-forget doesn't wait for response)

        finally:
            await order_service.stop()
            await inventory_service.stop()

    async def test_rpc_proxy_multiple_instances(self, nats_connection):
        """Test that each service instance gets its own proxy"""
        order_service1 = OrderService()
        order_service2 = OrderService()

        # Proxies should be different instances but work the same
        assert order_service1.inventory is not order_service2.inventory
        assert order_service1.inventory._service_name == order_service2.inventory._service_name


class TestRpcProxyUnit:
    """Unit tests for RpcProxy without NATS"""

    def test_rpc_proxy_descriptor(self):
        """Test that RpcProxy works as a descriptor"""

        class TestService(CliffracerService):
            other = RpcProxy("other_service")

            def __init__(self):
                config = ServiceConfig(name="test")
                super().__init__(config)

        service = TestService()

        # Should get a ServiceProxy instance
        proxy = service.other
        assert hasattr(proxy, "_service_name")
        assert proxy._service_name == "other_service"

    def test_rpc_proxy_caching(self):
        """Test that ServiceProxy is cached per instance"""

        class TestService(CliffracerService):
            other = RpcProxy("other_service")

            def __init__(self):
                config = ServiceConfig(name="test")
                super().__init__(config)

        service = TestService()

        # Multiple accesses should return the same ServiceProxy
        proxy1 = service.other
        proxy2 = service.other
        assert proxy1 is proxy2

    def test_method_proxy_creation(self):
        """Test that MethodProxy is created for method access"""

        class TestService(CliffracerService):
            other = RpcProxy("other_service")

            def __init__(self):
                config = ServiceConfig(name="test")
                super().__init__(config)

        service = TestService()

        # Accessing a method should give us a MethodProxy
        method = service.other.some_method
        assert hasattr(method, "_service_name")
        assert hasattr(method, "_method_name")
        assert method._service_name == "other_service"
        assert method._method_name == "some_method"

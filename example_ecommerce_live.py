"""
Comprehensive E-commerce System Example with Live Monitoring
==========================================================

This example demonstrates a realistic e-commerce system with:
- Order processing service
- Inventory management service  
- Payment processing service
- Notification service
- User service

All services include comprehensive monitoring, structured logging,
and realistic business logic with error scenarios.
"""

import asyncio
import random
import time
from datetime import datetime, UTC
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from nats_service_extended import ValidatedNATSService, HTTPNATSService, ServiceConfig
from nats_service_extended import validated_rpc, broadcast, listener, event_handler
from nats_runner import ServiceOrchestrator, configure_logging
from pydantic import BaseModel, Field, EmailStr


# Data Models
class OrderStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAYMENT_FAILED = "payment_failed"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class OrderItem(BaseModel):
    product_id: str
    name: str
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)


class CreateOrderRequest(BaseModel):
    user_id: str
    items: List[OrderItem]
    shipping_address: str
    email: EmailStr


class Order(BaseModel):
    order_id: str
    user_id: str
    items: List[OrderItem]
    total_amount: Decimal
    status: OrderStatus
    shipping_address: str
    email: str
    created_at: datetime
    updated_at: datetime


class PaymentRequest(BaseModel):
    order_id: str
    amount: Decimal
    payment_method: str = "credit_card"


class InventoryCheck(BaseModel):
    product_id: str
    quantity: int


class NotificationRequest(BaseModel):
    user_id: str
    email: str
    message: str
    type: str = "email"


# Event Models
class OrderCreatedEvent(BaseModel):
    order_id: str
    user_id: str
    total_amount: Decimal
    items: List[OrderItem]


class OrderStatusChangedEvent(BaseModel):
    order_id: str
    old_status: OrderStatus
    new_status: OrderStatus
    timestamp: datetime


class PaymentCompletedEvent(BaseModel):
    order_id: str
    payment_id: str
    amount: Decimal
    status: PaymentStatus


class InventoryReservedEvent(BaseModel):
    order_id: str
    items: List[OrderItem]
    reservation_id: str


# Services
class OrderService(HTTPNATSService):
    """Order processing service with HTTP endpoints and NATS messaging"""
    
    def __init__(self):
        config = ServiceConfig(
            name="order_service",
            description="E-commerce order processing service",
            auto_restart=True
        )
        super().__init__(config, port=8001)
        
        self.orders: Dict[str, Order] = {}
        self.order_counter = 0
        
        # Add HTTP endpoints
        @self.post("/orders", response_model=Order)
        async def create_order_http(request: CreateOrderRequest):
            """HTTP endpoint for creating orders"""
            return await self.create_order(request)
        
        @self.get("/orders/{order_id}")
        async def get_order_http(order_id: str):
            """HTTP endpoint for retrieving orders"""
            return await self.get_order(order_id)
        
        @self.get("/orders")
        async def list_orders_http():
            """HTTP endpoint for listing all orders"""
            return {"orders": list(self.orders.values())}

    async def on_startup(self):
        """Service startup initialization"""
        self.logger.info("OrderService starting up", extra={
            "service": "order_service",
            "version": "1.0.0"
        })
        
        # Record startup metric
        await self.record_metric("service.startup", 1, {
            "service": "order_service",
            "version": "1.0.0"
        })

    @validated_rpc(CreateOrderRequest, Order)
    async def create_order(self, request: CreateOrderRequest) -> Order:
        """Create a new order with full validation and monitoring"""
        start_time = time.time()
        order_id = f"order_{uuid4().hex[:8]}"
        
        try:
            # Calculate total
            total_amount = sum(item.price * item.quantity for item in request.items)
            
            # Create order
            order = Order(
                order_id=order_id,
                user_id=request.user_id,
                items=request.items,
                total_amount=total_amount,
                status=OrderStatus.PENDING,
                shipping_address=request.shipping_address,
                email=request.email,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC)
            )
            
            self.orders[order_id] = order
            self.order_counter += 1
            
            # Log order creation
            self.logger.info("Order created", extra={
                "order_id": order_id,
                "user_id": request.user_id,
                "total_amount": float(total_amount),
                "item_count": len(request.items),
                "action": "order_created"
            })
            
            # Broadcast event
            await self.broadcast_order_created(order)
            
            # Record metrics
            processing_time = time.time() - start_time
            await self.record_metric("orders.created", 1, {"status": "success"})
            await self.record_metric("orders.processing_time", processing_time)
            await self.record_metric("orders.total_amount", float(total_amount))
            
            return order
            
        except Exception as e:
            processing_time = time.time() - start_time
            self.logger.error("Order creation failed", extra={
                "order_id": order_id,
                "user_id": request.user_id,
                "error": str(e),
                "action": "order_creation_failed"
            })
            
            await self.record_metric("orders.created", 1, {"status": "error"})
            await self.record_metric("orders.processing_time", processing_time)
            raise

    @validated_rpc(str, Optional[Order])
    async def get_order(self, order_id: str) -> Optional[Order]:
        """Get order by ID"""
        start_time = time.time()
        
        order = self.orders.get(order_id)
        
        processing_time = time.time() - start_time
        await self.record_metric("orders.retrieved", 1, {
            "found": "true" if order else "false"
        })
        await self.record_metric("orders.retrieval_time", processing_time)
        
        if order:
            self.logger.debug("Order retrieved", extra={
                "order_id": order_id,
                "status": order.status.value,
                "action": "order_retrieved"
            })
        
        return order

    async def update_order_status(self, order_id: str, new_status: OrderStatus):
        """Update order status and broadcast event"""
        if order_id not in self.orders:
            raise ValueError(f"Order {order_id} not found")
        
        order = self.orders[order_id]
        old_status = order.status
        order.status = new_status
        order.updated_at = datetime.now(UTC)
        
        self.logger.info("Order status updated", extra={
            "order_id": order_id,
            "old_status": old_status.value,
            "new_status": new_status.value,
            "action": "status_updated"
        })
        
        # Broadcast status change event
        await self.broadcast_status_changed(order_id, old_status, new_status)
        
        await self.record_metric("orders.status_changed", 1, {
            "old_status": old_status.value,
            "new_status": new_status.value
        })

    @broadcast(OrderCreatedEvent)
    async def broadcast_order_created(self, order: Order):
        """Broadcast order created event"""
        return OrderCreatedEvent(
            order_id=order.order_id,
            user_id=order.user_id,
            total_amount=order.total_amount,
            items=order.items
        )

    @broadcast(OrderStatusChangedEvent)
    async def broadcast_status_changed(self, order_id: str, old_status: OrderStatus, new_status: OrderStatus):
        """Broadcast order status change event"""
        return OrderStatusChangedEvent(
            order_id=order_id,
            old_status=old_status,
            new_status=new_status,
            timestamp=datetime.now(UTC)
        )

    @listener(PaymentCompletedEvent)
    async def on_payment_completed(self, event: PaymentCompletedEvent):
        """Handle payment completion"""
        if event.status == PaymentStatus.COMPLETED:
            await self.update_order_status(event.order_id, OrderStatus.PAID)
        elif event.status == PaymentStatus.FAILED:
            await self.update_order_status(event.order_id, OrderStatus.PAYMENT_FAILED)


class InventoryService(ValidatedNATSService):
    """Inventory management service"""
    
    def __init__(self):
        config = ServiceConfig(
            name="inventory_service",
            description="Product inventory management",
            auto_restart=True
        )
        super().__init__(config)
        
        # Initialize with some sample inventory
        self.inventory = {
            "laptop-pro": {"name": "Professional Laptop", "quantity": 50, "reserved": 0},
            "smartphone-x": {"name": "Smartphone X", "quantity": 100, "reserved": 0},
            "tablet-air": {"name": "Tablet Air", "quantity": 30, "reserved": 0},
            "headphones-premium": {"name": "Premium Headphones", "quantity": 75, "reserved": 0},
            "mouse-wireless": {"name": "Wireless Mouse", "quantity": 200, "reserved": 0}
        }
        self.reservations = {}

    async def on_startup(self):
        """Service startup"""
        self.logger.info("InventoryService starting up", extra={
            "service": "inventory_service",
            "total_products": len(self.inventory)
        })

    @validated_rpc(InventoryCheck, dict)
    async def check_availability(self, check: InventoryCheck) -> dict:
        """Check product availability"""
        start_time = time.time()
        
        product = self.inventory.get(check.product_id)
        
        if not product:
            result = {
                "product_id": check.product_id,
                "available": False,
                "reason": "product_not_found"
            }
        else:
            available_quantity = product["quantity"] - product["reserved"]
            result = {
                "product_id": check.product_id,
                "available": available_quantity >= check.quantity,
                "available_quantity": available_quantity,
                "requested_quantity": check.quantity
            }
        
        processing_time = time.time() - start_time
        await self.record_metric("inventory.check", 1, {
            "product_id": check.product_id,
            "available": str(result["available"]).lower()
        })
        await self.record_metric("inventory.check_time", processing_time)
        
        return result

    @listener(OrderCreatedEvent)
    async def on_order_created(self, event: OrderCreatedEvent):
        """Reserve inventory when order is created"""
        start_time = time.time()
        reservation_id = f"res_{uuid4().hex[:8]}"
        
        try:
            # Check and reserve all items
            can_reserve_all = True
            for item in event.items:
                product = self.inventory.get(item.product_id)
                if not product:
                    can_reserve_all = False
                    break
                    
                available = product["quantity"] - product["reserved"]
                if available < item.quantity:
                    can_reserve_all = False
                    break
            
            if can_reserve_all:
                # Reserve items
                for item in event.items:
                    self.inventory[item.product_id]["reserved"] += item.quantity
                
                self.reservations[reservation_id] = {
                    "order_id": event.order_id,
                    "items": event.items,
                    "created_at": datetime.now(UTC)
                }
                
                self.logger.info("Inventory reserved", extra={
                    "order_id": event.order_id,
                    "reservation_id": reservation_id,
                    "item_count": len(event.items),
                    "action": "inventory_reserved"
                })
                
                # Broadcast reservation event
                await self.broadcast_inventory_reserved(event.order_id, event.items, reservation_id)
                
                await self.record_metric("inventory.reserved", 1, {"status": "success"})
            else:
                self.logger.warning("Insufficient inventory", extra={
                    "order_id": event.order_id,
                    "action": "inventory_insufficient"
                })
                
                await self.record_metric("inventory.reserved", 1, {"status": "insufficient"})
        
        except Exception as e:
            self.logger.error("Inventory reservation failed", extra={
                "order_id": event.order_id,
                "error": str(e),
                "action": "reservation_failed"
            })
            
            await self.record_metric("inventory.reserved", 1, {"status": "error"})
        
        processing_time = time.time() - start_time
        await self.record_metric("inventory.reservation_time", processing_time)

    @broadcast(InventoryReservedEvent)
    async def broadcast_inventory_reserved(self, order_id: str, items: List[OrderItem], reservation_id: str):
        """Broadcast inventory reservation event"""
        return InventoryReservedEvent(
            order_id=order_id,
            items=items,
            reservation_id=reservation_id
        )


class PaymentService(ValidatedNATSService):
    """Payment processing service"""
    
    def __init__(self):
        config = ServiceConfig(
            name="payment_service",
            description="Payment processing and validation",
            auto_restart=True
        )
        super().__init__(config)
        self.payments = {}

    async def on_startup(self):
        """Service startup"""
        self.logger.info("PaymentService starting up", extra={
            "service": "payment_service"
        })

    @validated_rpc(PaymentRequest, dict)
    async def process_payment(self, request: PaymentRequest) -> dict:
        """Process payment (simulated)"""
        start_time = time.time()
        payment_id = f"pay_{uuid4().hex[:8]}"
        
        # Simulate payment processing delay
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        # Simulate payment success/failure (90% success rate)
        success = random.random() < 0.9
        
        payment = {
            "payment_id": payment_id,
            "order_id": request.order_id,
            "amount": request.amount,
            "method": request.payment_method,
            "status": PaymentStatus.COMPLETED if success else PaymentStatus.FAILED,
            "processed_at": datetime.now(UTC)
        }
        
        self.payments[payment_id] = payment
        
        processing_time = time.time() - start_time
        
        if success:
            self.logger.info("Payment processed successfully", extra={
                "payment_id": payment_id,
                "order_id": request.order_id,
                "amount": float(request.amount),
                "processing_time": processing_time,
                "action": "payment_success"
            })
        else:
            self.logger.warning("Payment failed", extra={
                "payment_id": payment_id,
                "order_id": request.order_id,
                "amount": float(request.amount),
                "processing_time": processing_time,
                "action": "payment_failed"
            })
        
        # Broadcast payment completion
        await self.broadcast_payment_completed(
            request.order_id, 
            payment_id, 
            request.amount, 
            payment["status"]
        )
        
        await self.record_metric("payments.processed", 1, {
            "status": "success" if success else "failed",
            "method": request.payment_method
        })
        await self.record_metric("payments.processing_time", processing_time)
        await self.record_metric("payments.amount", float(request.amount))
        
        return payment

    @listener(InventoryReservedEvent)
    async def on_inventory_reserved(self, event: InventoryReservedEvent):
        """Process payment when inventory is reserved"""
        # Calculate total amount
        total_amount = sum(item.price * item.quantity for item in event.items)
        
        payment_request = PaymentRequest(
            order_id=event.order_id,
            amount=total_amount,
            payment_method="credit_card"
        )
        
        await self.process_payment(payment_request)

    @broadcast(PaymentCompletedEvent)
    async def broadcast_payment_completed(self, order_id: str, payment_id: str, amount: Decimal, status: PaymentStatus):
        """Broadcast payment completion event"""
        return PaymentCompletedEvent(
            order_id=order_id,
            payment_id=payment_id,
            amount=amount,
            status=status
        )


class NotificationService(ValidatedNATSService):
    """Notification service for emails and SMS"""
    
    def __init__(self):
        config = ServiceConfig(
            name="notification_service", 
            description="Email and SMS notifications",
            auto_restart=True
        )
        super().__init__(config)
        self.sent_notifications = []

    async def on_startup(self):
        """Service startup"""
        self.logger.info("NotificationService starting up", extra={
            "service": "notification_service"
        })

    @validated_rpc(NotificationRequest, dict)
    async def send_notification(self, request: NotificationRequest) -> dict:
        """Send notification (simulated)"""
        start_time = time.time()
        notification_id = f"notif_{uuid4().hex[:8]}"
        
        # Simulate sending delay
        await asyncio.sleep(random.uniform(0.05, 0.2))
        
        notification = {
            "notification_id": notification_id,
            "user_id": request.user_id,
            "email": request.email,
            "message": request.message,
            "type": request.type,
            "sent_at": datetime.now(UTC),
            "status": "sent"
        }
        
        self.sent_notifications.append(notification)
        
        processing_time = time.time() - start_time
        
        self.logger.info("Notification sent", extra={
            "notification_id": notification_id,
            "user_id": request.user_id,
            "type": request.type,
            "processing_time": processing_time,
            "action": "notification_sent"
        })
        
        await self.record_metric("notifications.sent", 1, {"type": request.type})
        await self.record_metric("notifications.processing_time", processing_time)
        
        return notification

    @listener(OrderCreatedEvent)
    async def on_order_created(self, event: OrderCreatedEvent):
        """Send order confirmation notification"""
        await self.send_notification(NotificationRequest(
            user_id=event.user_id,
            email="customer@example.com",  # In real system, get from user service
            message=f"Order {event.order_id} created successfully! Total: ${event.total_amount}",
            type="order_confirmation"
        ))

    @listener(OrderStatusChangedEvent)
    async def on_order_status_changed(self, event: OrderStatusChangedEvent):
        """Send status update notifications"""
        if event.new_status in [OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.DELIVERED]:
            await self.send_notification(NotificationRequest(
                user_id="user_123",  # In real system, get from order
                email="customer@example.com",
                message=f"Order {event.order_id} status updated to: {event.new_status.value}",
                type="status_update"
            ))


class LoadGeneratorService(ValidatedNATSService):
    """Load generator to create realistic traffic"""
    
    def __init__(self):
        config = ServiceConfig(
            name="load_generator",
            description="Generates realistic load for testing",
            auto_restart=True
        )
        super().__init__(config)
        self.running = False

    async def on_startup(self):
        """Start load generation"""
        self.logger.info("LoadGeneratorService starting up")
        self.running = True
        asyncio.create_task(self.generate_load())

    async def generate_load(self):
        """Generate realistic e-commerce traffic"""
        products = [
            ("laptop-pro", "Professional Laptop", Decimal("1299.99")),
            ("smartphone-x", "Smartphone X", Decimal("899.99")),
            ("tablet-air", "Tablet Air", Decimal("599.99")),
            ("headphones-premium", "Premium Headphones", Decimal("199.99")),
            ("mouse-wireless", "Wireless Mouse", Decimal("49.99"))
        ]
        
        order_counter = 1
        
        while self.running:
            try:
                # Create random order
                num_items = random.randint(1, 3)
                items = []
                
                for _ in range(num_items):
                    product_id, name, price = random.choice(products)
                    quantity = random.randint(1, 3)
                    items.append(OrderItem(
                        product_id=product_id,
                        name=name,
                        quantity=quantity,
                        price=price
                    ))
                
                order_request = CreateOrderRequest(
                    user_id=f"user_{random.randint(1, 100)}",
                    items=items,
                    shipping_address=f"{random.randint(100, 9999)} Main St, City {random.randint(1, 50)}",
                    email=f"user{random.randint(1, 100)}@example.com"
                )
                
                # Create order via RPC
                order = await self.call_rpc("order_service", "create_order", order_request)
                
                self.logger.info("Generated order", extra={
                    "order_id": order["order_id"],
                    "total_amount": float(order["total_amount"]),
                    "action": "load_generated"
                })
                
                await self.record_metric("load_generator.orders_created", 1)
                
                # Wait before next order (2-10 seconds)
                await asyncio.sleep(random.uniform(2, 10))
                
            except Exception as e:
                self.logger.error("Load generation error", extra={
                    "error": str(e),
                    "action": "load_generation_error"
                })
                await asyncio.sleep(5)


async def main():
    """Run the complete e-commerce system"""
    print("🚀 Starting Cliffracer E-commerce System with Live Monitoring")
    print("=" * 70)
    
    # Configure structured logging
    configure_logging(level="INFO")
    
    # Create service orchestrator
    orchestrator = ServiceOrchestrator()
    
    # Add all services
    services = [
        (OrderService, ServiceConfig(name="order_service", auto_restart=True)),
        (InventoryService, ServiceConfig(name="inventory_service", auto_restart=True)),
        (PaymentService, ServiceConfig(name="payment_service", auto_restart=True)),
        (NotificationService, ServiceConfig(name="notification_service", auto_restart=True)),
        (LoadGeneratorService, ServiceConfig(name="load_generator", auto_restart=True))
    ]
    
    for service_class, config in services:
        orchestrator.add_service(service_class, config)
    
    print("\n📊 Monitoring Information:")
    print("- Order Service API: http://localhost:8001/docs")
    print("- NATS Monitoring: http://localhost:8222")
    print("- Zabbix Dashboard: http://localhost:8080 (admin/zabbix)")
    print("- Service Logs: Check terminal output for structured JSON logs")
    print("\n🔄 The system will automatically generate orders every 2-10 seconds")
    print("💡 Watch the Zabbix dashboards for real-time metrics!")
    print("\nPress Ctrl+C to stop all services")
    print("=" * 70)
    
    try:
        await orchestrator.run()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down services...")
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
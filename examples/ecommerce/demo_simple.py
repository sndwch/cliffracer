#!/usr/bin/env python3
"""
Cliffracer Demo - Core Concepts (No Docker Required)
==================================================

This demo shows the key Cliffracer features without requiring Docker:
- Multi-service communication via in-memory NATS simulation
- Event-driven architecture
- Structured logging
- Type-safe APIs
- Real-time monitoring metrics

This runs entirely in Python to showcase the framework concepts.
"""

import asyncio
import json
import random
import time
from datetime import datetime, UTC
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4
import logging

from pydantic import BaseModel, Field, EmailStr

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler()]
)

class StructuredLogger:
    """Simple structured logger for demo"""
    
    def __init__(self, service_name: str):
        self.service_name = service_name
        self.logger = logging.getLogger(service_name)
    
    def info(self, message: str, **extra):
        log_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "INFO",
            "service": self.service_name,
            "message": message,
            **extra
        }
        self.logger.info(json.dumps(log_data, default=str))
    
    def error(self, message: str, **extra):
        log_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "ERROR", 
            "service": self.service_name,
            "message": message,
            **extra
        }
        self.logger.error(json.dumps(log_data, default=str))


class InMemoryMessageBus:
    """Simple in-memory message bus to simulate NATS"""
    
    def __init__(self):
        self.subscribers = {}
        self.message_count = 0
        self.total_latency = 0
    
    def subscribe(self, subject: str, callback):
        if subject not in self.subscribers:
            self.subscribers[subject] = []
        self.subscribers[subject].append(callback)
    
    async def publish(self, subject: str, data: dict):
        start_time = time.time()
        self.message_count += 1
        
        if subject in self.subscribers:
            for callback in self.subscribers[subject]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    print(f"Error in message handler: {e}")
        
        latency = time.time() - start_time
        self.total_latency += latency
        
        # Show message routing
        print(f"📤 NATS: {subject} -> {len(self.subscribers.get(subject, []))} subscribers ({latency*1000:.2f}ms)")
    
    def get_stats(self):
        avg_latency = self.total_latency / max(self.message_count, 1)
        return {
            "messages_processed": self.message_count,
            "average_latency_ms": avg_latency * 1000,
            "active_subscriptions": sum(len(subs) for subs in self.subscribers.values())
        }


# Global message bus
message_bus = InMemoryMessageBus()


# Data Models (same as full demo)
class OrderStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAYMENT_FAILED = "payment_failed"
    PAID = "paid"
    SHIPPED = "shipped"


class OrderItem(BaseModel):
    product_id: str
    name: str
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)


class Order(BaseModel):
    order_id: str
    user_id: str
    items: List[OrderItem]
    total_amount: Decimal
    status: OrderStatus
    created_at: datetime


# Simplified Services
class OrderService:
    """Order processing service"""
    
    def __init__(self):
        self.logger = StructuredLogger("order_service")
        self.orders: Dict[str, Order] = {}
        self.metrics = {"orders_created": 0, "processing_times": []}
        
        # Subscribe to payment events
        message_bus.subscribe("payment.completed", self.on_payment_completed)
    
    async def create_order(self, user_id: str, items: List[OrderItem]) -> Order:
        """Create a new order"""
        start_time = time.time()
        order_id = f"order_{uuid4().hex[:8]}"
        
        # Calculate total
        total_amount = sum(item.price * item.quantity for item in items)
        
        # Create order
        order = Order(
            order_id=order_id,
            user_id=user_id,
            items=items,
            total_amount=total_amount,
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC)
        )
        
        self.orders[order_id] = order
        self.metrics["orders_created"] += 1
        
        processing_time = time.time() - start_time
        self.metrics["processing_times"].append(processing_time)
        
        # Log with structured data
        self.logger.info("Order created", 
            order_id=order_id,
            user_id=user_id,
            total_amount=float(total_amount),
            item_count=len(items),
            processing_time_ms=processing_time * 1000,
            action="order_created"
        )
        
        # Publish event
        await message_bus.publish("order.created", {
            "order_id": order_id,
            "user_id": user_id,
            "total_amount": float(total_amount),
            "items": [item.model_dump() for item in items]
        })
        
        return order
    
    async def on_payment_completed(self, data: dict):
        """Handle payment completion events"""
        order_id = data["order_id"]
        success = data["success"]
        
        if order_id in self.orders:
            old_status = self.orders[order_id].status
            new_status = OrderStatus.PAID if success else OrderStatus.PAYMENT_FAILED
            self.orders[order_id].status = new_status
            
            self.logger.info("Order status updated",
                order_id=order_id,
                old_status=old_status.value,
                new_status=new_status.value,
                action="status_updated"
            )
            
            # Publish status change
            await message_bus.publish("order.status_changed", {
                "order_id": order_id,
                "old_status": old_status.value,
                "new_status": new_status.value
            })


class InventoryService:
    """Inventory management service"""
    
    def __init__(self):
        self.logger = StructuredLogger("inventory_service")
        self.inventory = {
            "laptop-pro": {"name": "Professional Laptop", "quantity": 50},
            "smartphone-x": {"name": "Smartphone X", "quantity": 100},
            "tablet-air": {"name": "Tablet Air", "quantity": 30}
        }
        self.metrics = {"reservations": 0, "items_reserved": 0}
        
        # Subscribe to order events
        message_bus.subscribe("order.created", self.on_order_created)
    
    async def on_order_created(self, data: dict):
        """Reserve inventory when order is created"""
        order_id = data["order_id"]
        items = data["items"]
        
        # Check availability
        can_reserve = True
        for item in items:
            product = self.inventory.get(item["product_id"])
            if not product or product["quantity"] < item["quantity"]:
                can_reserve = False
                break
        
        if can_reserve:
            # Reserve items
            for item in items:
                self.inventory[item["product_id"]]["quantity"] -= item["quantity"]
                self.metrics["items_reserved"] += item["quantity"]
            
            self.metrics["reservations"] += 1
            
            self.logger.info("Inventory reserved",
                order_id=order_id,
                item_count=len(items),
                action="inventory_reserved"
            )
            
            # Trigger payment processing
            await message_bus.publish("inventory.reserved", {
                "order_id": order_id,
                "total_amount": data["total_amount"]
            })
        else:
            self.logger.error("Insufficient inventory",
                order_id=order_id,
                action="inventory_insufficient"
            )


class PaymentService:
    """Payment processing service"""
    
    def __init__(self):
        self.logger = StructuredLogger("payment_service")
        self.payments = {}
        self.metrics = {"payments_processed": 0, "success_rate": 0.9}
        
        # Subscribe to inventory events
        message_bus.subscribe("inventory.reserved", self.on_inventory_reserved)
    
    async def on_inventory_reserved(self, data: dict):
        """Process payment when inventory is reserved"""
        order_id = data["order_id"]
        amount = data["total_amount"]
        
        # Simulate payment processing delay
        await asyncio.sleep(random.uniform(0.1, 0.3))
        
        # Simulate success/failure (90% success rate)
        success = random.random() < self.metrics["success_rate"]
        
        payment_id = f"pay_{uuid4().hex[:8]}"
        payment = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "success": success,
            "processed_at": datetime.now(UTC)
        }
        
        self.payments[payment_id] = payment
        self.metrics["payments_processed"] += 1
        
        if success:
            self.logger.info("Payment processed successfully",
                payment_id=payment_id,
                order_id=order_id,
                amount=amount,
                action="payment_success"
            )
        else:
            self.logger.error("Payment failed",
                payment_id=payment_id,
                order_id=order_id,
                amount=amount,
                action="payment_failed"
            )
        
        # Publish completion
        await message_bus.publish("payment.completed", {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "success": success
        })


class NotificationService:
    """Notification service"""
    
    def __init__(self):
        self.logger = StructuredLogger("notification_service")
        self.notifications = []
        self.metrics = {"notifications_sent": 0}
        
        # Subscribe to various events
        message_bus.subscribe("order.created", self.on_order_created)
        message_bus.subscribe("order.status_changed", self.on_status_changed)
    
    async def on_order_created(self, data: dict):
        """Send order confirmation"""
        # Simulate notification delay
        await asyncio.sleep(random.uniform(0.05, 0.15))
        
        notification = {
            "type": "order_confirmation",
            "order_id": data["order_id"],
            "message": f"Order {data['order_id']} created! Total: ${data['total_amount']}",
            "sent_at": datetime.now(UTC)
        }
        
        self.notifications.append(notification)
        self.metrics["notifications_sent"] += 1
        
        self.logger.info("Order confirmation sent",
            order_id=data["order_id"],
            notification_type="order_confirmation",
            action="notification_sent"
        )
    
    async def on_status_changed(self, data: dict):
        """Send status update notifications"""
        if data["new_status"] in ["paid", "shipped"]:
            notification = {
                "type": "status_update",
                "order_id": data["order_id"],
                "message": f"Order {data['order_id']} is now {data['new_status']}",
                "sent_at": datetime.now(UTC)
            }
            
            self.notifications.append(notification)
            self.metrics["notifications_sent"] += 1
            
            self.logger.info("Status update sent",
                order_id=data["order_id"],
                new_status=data["new_status"],
                notification_type="status_update",
                action="notification_sent"
            )


class MonitoringService:
    """Monitoring and metrics collection"""
    
    def __init__(self):
        self.logger = StructuredLogger("monitoring_service")
        self.start_time = time.time()
    
    async def show_metrics(self, services: dict):
        """Display current system metrics"""
        uptime = time.time() - self.start_time
        bus_stats = message_bus.get_stats()
        
        print("\n" + "="*60)
        print("📊 CLIFFRACER SYSTEM METRICS")
        print("="*60)
        print(f"⏱️  System Uptime: {uptime:.1f}s")
        print(f"📤 Messages Processed: {bus_stats['messages_processed']}")
        print(f"⚡ Avg Message Latency: {bus_stats['average_latency_ms']:.3f}ms")
        print(f"🔗 Active Subscriptions: {bus_stats['active_subscriptions']}")
        
        print("\n🛒 ORDER SERVICE:")
        order_metrics = services['order'].metrics
        print(f"   Orders Created: {order_metrics['orders_created']}")
        if order_metrics['processing_times']:
            avg_time = sum(order_metrics['processing_times']) / len(order_metrics['processing_times'])
            print(f"   Avg Processing Time: {avg_time*1000:.2f}ms")
        
        print("\n📦 INVENTORY SERVICE:")
        inv_metrics = services['inventory'].metrics
        print(f"   Reservations: {inv_metrics['reservations']}")
        print(f"   Items Reserved: {inv_metrics['items_reserved']}")
        
        print("\n💳 PAYMENT SERVICE:")
        pay_metrics = services['payment'].metrics
        print(f"   Payments Processed: {pay_metrics['payments_processed']}")
        print(f"   Success Rate: {pay_metrics['success_rate']*100:.1f}%")
        
        print("\n📧 NOTIFICATION SERVICE:")
        notif_metrics = services['notification'].metrics
        print(f"   Notifications Sent: {notif_metrics['notifications_sent']}")
        
        print("="*60)


async def generate_orders(order_service: OrderService):
    """Generate realistic e-commerce orders"""
    products = [
        ("laptop-pro", "Professional Laptop", Decimal("1299.99")),
        ("smartphone-x", "Smartphone X", Decimal("899.99")),
        ("tablet-air", "Tablet Air", Decimal("599.99"))
    ]
    
    order_count = 1
    
    while True:
        try:
            # Create random order
            num_items = random.randint(1, 3)
            items = []
            
            for _ in range(num_items):
                product_id, name, price = random.choice(products)
                quantity = random.randint(1, 2)
                items.append(OrderItem(
                    product_id=product_id,
                    name=name,
                    quantity=quantity,
                    price=price
                ))
            
            user_id = f"user_{random.randint(1, 50)}"
            order = await order_service.create_order(user_id, items)
            
            print(f"\n🛒 Order #{order_count} created: {order.order_id} (${order.total_amount})")
            order_count += 1
            
            # Wait before next order
            await asyncio.sleep(random.uniform(3, 8))
            
        except Exception as e:
            print(f"❌ Error generating order: {e}")
            await asyncio.sleep(5)


async def main():
    """Run the simplified demo"""
    print("🚀 Cliffracer NATS Framework - Core Concepts Demo")
    print("=" * 60)
    print()
    print("This demo showcases:")
    print("  ✅ Multi-service event-driven architecture")
    print("  ✅ Sub-millisecond message routing")
    print("  ✅ Structured JSON logging")
    print("  ✅ Type-safe service communication")
    print("  ✅ Real-time metrics and monitoring")
    print()
    print("🔄 Watch the structured logs and metrics!")
    print("💡 Each service processes events in real-time")
    print()
    print("Press Ctrl+C to stop the demo")
    print("=" * 60)
    
    # Create services
    services = {
        'order': OrderService(),
        'inventory': InventoryService(),
        'payment': PaymentService(),
        'notification': NotificationService(),
        'monitoring': MonitoringService()
    }
    
    print("\n🚀 Starting services...")
    await asyncio.sleep(1)
    
    # Start order generation
    order_task = asyncio.create_task(generate_orders(services['order']))
    
    # Start metrics monitoring
    async def show_periodic_metrics():
        await asyncio.sleep(5)  # Wait for first order
        while True:
            await services['monitoring'].show_metrics(services)
            await asyncio.sleep(10)
    
    metrics_task = asyncio.create_task(show_periodic_metrics())
    
    try:
        # Run until interrupted
        await asyncio.gather(order_task, metrics_task)
    except KeyboardInterrupt:
        print("\n\n🛑 Demo stopped by user")
        
        # Show final metrics
        print("\n📊 FINAL METRICS:")
        await services['monitoring'].show_metrics(services)
        
        print("\n💡 Key Takeaways:")
        print("  🚀 Sub-millisecond message routing between services")
        print("  📝 Structured logging makes debugging easy")
        print("  🔄 Event-driven architecture scales naturally")
        print("  📊 Built-in monitoring shows real business metrics")
        print("  🛡️  Type safety prevents runtime errors")
        
        print("\n🌟 This is just the core - full version includes:")
        print("  📈 Zabbix dashboards with advanced metrics")
        print("  🌐 HTTP APIs with FastAPI integration")
        print("  🐳 Production Docker deployment")
        print("  ☁️  AWS cloud integration")
        print("  🔒 Authentication and authorization")


if __name__ == "__main__":
    asyncio.run(main())
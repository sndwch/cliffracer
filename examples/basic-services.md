# Basic Services Example

This example demonstrates the core features of the NATS microservices framework using a simple e-commerce scenario with order processing, inventory management, and notifications.

## Overview

The basic services example (`example_services.py`) showcases:

- **Simple RPC calls** between services
- **Event-driven communication** with publish/subscribe patterns
- **Service orchestration** with multiple microservices
- **Auto-restart capabilities** for resilience

## Services Included

### OrderService

**Purpose**: Manages order creation and status updates

**RPC Methods**:
- `create_order(user_id, items, total)` - Creates a new order
- `get_order(order_id)` - Retrieves order details
- `update_status(order_id, status)` - Updates order status

**Events Published**:
- `orders.created` - When a new order is created
- `orders.status.{status}` - When order status changes

**Events Handled**:
- `payments.completed` - Automatically updates order to "paid" status

### InventoryService

**Purpose**: Manages product inventory and stock levels

**RPC Methods**:
- `check_availability(item, quantity)` - Checks if items are in stock
- `reserve_items(items)` - Reserves items from inventory

**Events Handled**:
- `orders.created` - Reacts to new orders for inventory tracking

### NotificationService

**Purpose**: Handles notifications and alerts

**RPC Methods**:
- `send_notification(user_id, message, channel)` - Sends notifications

**Events Handled**:
- `orders.*` - Listens to all order events for notifications
- `inventory.low` - Handles low inventory alerts

## Running the Example

### Prerequisites

```bash
# Start NATS server
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222
```

### Run All Services

```bash
# Run all services together
python example_services.py
```

### Run Individual Services

```bash
# Run just the order service
python example_services.py order
```

### Test the Services

```bash
# Run the test suite
python example_services.py test
```

## Example Workflow

The test script demonstrates a complete order processing workflow:

1. **Create Order**: User places an order for widgets and gadgets
2. **Check Inventory**: Verify items are available
3. **Reserve Items**: Reserve items from inventory
4. **Update Status**: Change order status to "processing"
5. **Send Notification**: Notify user of order progress
6. **Payment Event**: Simulate payment completion
7. **Auto-Update**: Order status automatically updates to "paid"

## Key Concepts Demonstrated

### 1. RPC Communication

```python
# Synchronous request-response pattern
order = await client.call_rpc(
    "order_service",
    "create_order",
    user_id="user123",
    items=[{"name": "widget", "quantity": 2}],
    total=45.0
)
```

### 2. Event Publishing

```python
# Publish events for other services to react to
await self.publish_event(
    "orders.created",
    order_id=order_id,
    user_id=user_id,
    total=total
)
```

### 3. Event Handling

```python
# React to events from other services
@event_handler("payments.completed")
async def handle_payment_completed(self, order_id: str, **kwargs):
    await self.update_status(order_id, "paid")
```

### 4. Service Configuration

```python
# Configure services with auto-restart
config = ServiceConfig(
    name="order_service",
    nats_url="nats://localhost:4222",
    auto_restart=True
)
```

## Expected Output

When running the test, you should see output like:

```
=== Creating Order ===
Created order: {'id': 'order_1234', 'user_id': 'user123', 'status': 'pending', ...}

=== Checking Inventory ===
Inventory check: {'item': 'widget', 'requested': 5, 'available': 100, 'in_stock': True}

=== Reserving Items ===
Reserved items: [{'name': 'widget', 'quantity': 2, 'reserved': True}, ...]

[Notification] Order event 'created': {'order_id': 'order_1234', ...}
[Notification] Order event 'processing': {'order_id': 'order_1234', ...}
Sending email to user user123: Your order is being processed!
```

## Next Steps

After running this basic example, explore:

1. **[Extended Services](ecommerce-system.md)** - Advanced features with HTTP APIs and validation
2. **[Authentication Patterns](auth-patterns.md)** - Role-based access control
3. **[Async Patterns](async-patterns.md)** - Fire-and-forget vs request-response patterns
4. **[Monitoring Setup](monitoring-setup.md)** - Production monitoring with Zabbix

## Troubleshooting

### Services Won't Connect

```bash
# Check NATS is running
docker ps | grep nats
curl http://localhost:8222/varz
```

### Import Errors

```bash
# Verify framework is installed
python -c "from nats_service import Service; print('✅ Framework ready')"
```

### No Event Delivery

- Events are delivered asynchronously - add `await asyncio.sleep(1)` between operations
- Check service names match exactly in RPC calls
- Verify NATS subject patterns match event handler patterns

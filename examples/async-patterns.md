# Async Patterns Example

This example demonstrates different asynchronous communication patterns in NATS microservices, including synchronous RPC (request-response), asynchronous RPC (fire-and-forget), and event-driven patterns.

## Overview

The async patterns example (`example_async_patterns.py`) showcases:

- **Synchronous RPC** - Request-response pattern with waiting for results
- **Asynchronous RPC** - Fire-and-forget pattern for background processing
- **Event-driven communication** - Publish-subscribe patterns
- **Performance comparisons** between different patterns
- **Error handling** in async scenarios
- **Batch processing** with async patterns

## Communication Patterns

### 1. Synchronous RPC (Request-Response)

**Use Case**: When you need immediate results and confirmation

```python
class OrderService(Service):
    @rpc
    async def process_payment(self, order_id: str, amount: float) -> dict:
        """Synchronous payment processing - caller waits for result"""
        # Simulate payment processing
        await asyncio.sleep(2)  # Simulated payment gateway delay
        
        result = {
            "order_id": order_id,
            "amount": amount,
            "status": "completed",
            "transaction_id": f"txn_{random.randint(10000, 99999)}",
            "processed_at": datetime.utcnow().isoformat()
        }
        
        return result

# Client usage
result = await client.call_rpc(
    "order_service",
    "process_payment",
    order_id="order_123",
    amount=99.99
)
print(f"Payment result: {result}")  # Waits for response
```

### 2. Asynchronous RPC (Fire-and-Forget)

**Use Case**: Background processing, logging, notifications where immediate response isn't needed

```python
class NotificationService(Service):
    @async_rpc
    async def send_email(self, recipient: str, subject: str, body: str):
        """Async email sending - caller doesn't wait"""
        # Simulate email sending delay
        await asyncio.sleep(3)
        
        print(f"Email sent to {recipient}: {subject}")
        
        # Could also publish event when done
        await self.publish_event(
            "emails.sent",
            recipient=recipient,
            subject=subject,
            sent_at=datetime.utcnow().isoformat()
        )

# Client usage - doesn't wait for completion
await client.call_async(
    "notification_service",
    "send_email",
    recipient="user@example.com",
    subject="Order Confirmation",
    body="Your order has been processed."
)
print("Email request sent, continuing...")  # Immediate return
```

### 3. Event-Driven Pattern

**Use Case**: Decoupled communication, multiple services reacting to events

```python
class OrderService(Service):
    @rpc
    async def create_order(self, user_id: str, items: list) -> dict:
        order = {
            "order_id": f"order_{random.randint(1000, 9999)}",
            "user_id": user_id,
            "items": items,
            "status": "created",
            "created_at": datetime.utcnow().isoformat()
        }
        
        # Publish event - multiple services can react
        await self.publish_event(
            "orders.created",
            **order
        )
        
        return order

# Multiple services can listen to the same event
class InventoryService(Service):
    @event_handler("orders.created")
    async def reserve_inventory(self, order_id: str, items: list, **kwargs):
        print(f"Reserving inventory for order {order_id}")
        # Reserve items...

class EmailService(Service):
    @event_handler("orders.created")
    async def send_confirmation(self, order_id: str, user_id: str, **kwargs):
        print(f"Sending confirmation email for order {order_id}")
        # Send email...

class AnalyticsService(Service):
    @event_handler("orders.created")
    async def track_order(self, order_id: str, **kwargs):
        print(f"Tracking order {order_id} in analytics")
        # Update analytics...
```

## Performance Comparison

### Synchronous Pattern Performance

```python
async def test_sync_performance():
    """Test synchronous RPC performance"""
    start_time = time.time()
    
    # Process 5 payments sequentially (each takes 2 seconds)
    for i in range(5):
        result = await client.call_rpc(
            "order_service",
            "process_payment",
            order_id=f"order_{i}",
            amount=10.0 * (i + 1)
        )
        print(f"Payment {i+1} completed: {result['transaction_id']}")
    
    elapsed = time.time() - start_time
    print(f"Sync processing took: {elapsed:.2f} seconds")  # ~10 seconds
```

### Asynchronous Pattern Performance

```python
async def test_async_performance():
    """Test asynchronous RPC performance"""
    start_time = time.time()
    
    # Send 5 emails concurrently (fire-and-forget)
    tasks = []
    for i in range(5):
        task = client.call_async(
            "notification_service",
            "send_email",
            recipient=f"user{i}@example.com",
            subject=f"Notification {i+1}",
            body=f"This is notification number {i+1}"
        )
        tasks.append(task)
    
    # All requests sent immediately
    await asyncio.gather(*tasks)
    
    elapsed = time.time() - start_time
    print(f"Async processing took: {elapsed:.2f} seconds")  # ~0.1 seconds
    print("Note: Actual email processing happens in background")
```

## Mixed Patterns Example

```python
class EcommerceWorkflow(Service):
    """Example combining sync and async patterns"""
    
    @rpc
    async def process_order(self, order_data: dict) -> dict:
        """Main order processing workflow"""
        
        # 1. Create order (sync) - need immediate confirmation
        order = await self.call_rpc(
            "order_service",
            "create_order",
            **order_data
        )
        print(f"Order created: {order['order_id']}")
        
        # 2. Process payment (sync) - need to confirm payment
        payment_result = await self.call_rpc(
            "payment_service",
            "process_payment",
            order_id=order['order_id'],
            amount=order['total']
        )
        
        if payment_result['status'] != 'completed':
            raise Exception("Payment failed")
        
        print(f"Payment completed: {payment_result['transaction_id']}")
        
        # 3. Send confirmation email (async) - don't wait
        await self.call_async(
            "notification_service",
            "send_confirmation_email",
            order_id=order['order_id'],
            user_email=order_data['user_email']
        )
        
        # 4. Update analytics (async) - background processing
        await self.call_async(
            "analytics_service",
            "track_order",
            order_id=order['order_id'],
            amount=order['total']
        )
        
        # 5. Schedule fulfillment (async) - background processing
        await self.call_async(
            "fulfillment_service",
            "schedule_shipping",
            order_id=order['order_id'],
            items=order['items']
        )
        
        # Return immediate response with essential info
        return {
            "order_id": order['order_id'],
            "status": "confirmed",
            "payment_id": payment_result['transaction_id'],
            "message": "Order confirmed. Email confirmation sent."
        }
```

## Error Handling Patterns

### Synchronous Error Handling

```python
async def sync_with_error_handling():
    """Handle errors in synchronous calls"""
    try:
        result = await client.call_rpc(
            "payment_service",
            "process_payment",
            order_id="order_123",
            amount=99.99
        )
        print(f"Payment successful: {result}")
        
    except Exception as e:
        print(f"Payment failed: {e}")
        # Can immediately handle error
        await client.call_rpc(
            "order_service",
            "cancel_order",
            order_id="order_123",
            reason="Payment failed"
        )
```

### Asynchronous Error Handling

```python
class NotificationService(Service):
    @async_rpc
    async def send_email(self, recipient: str, subject: str, body: str):
        try:
            # Simulate email sending
            if "invalid" in recipient:
                raise Exception("Invalid email address")
            
            await asyncio.sleep(1)
            print(f"Email sent to {recipient}")
            
            # Publish success event
            await self.publish_event(
                "emails.sent",
                recipient=recipient,
                subject=subject
            )
            
        except Exception as e:
            print(f"Email failed: {e}")
            
            # Publish error event for error handling service
            await self.publish_event(
                "emails.failed",
                recipient=recipient,
                subject=subject,
                error=str(e)
            )

# Error handling service
class ErrorHandlingService(Service):
    @event_handler("emails.failed")
    async def handle_email_failure(self, recipient: str, error: str, **kwargs):
        print(f"Handling email failure: {recipient} - {error}")
        # Could retry, log, or alert administrators
```

## Batch Processing Patterns

### Batch Synchronous Processing

```python
async def batch_sync_processing():
    """Process multiple items synchronously with batching"""
    items = [f"item_{i}" for i in range(100)]
    batch_size = 10
    results = []
    
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        
        # Process batch synchronously
        batch_result = await client.call_rpc(
            "processing_service",
            "process_batch",
            items=batch
        )
        
        results.extend(batch_result)
        print(f"Processed batch {i//batch_size + 1}: {len(batch)} items")
    
    return results
```

### Batch Asynchronous Processing

```python
async def batch_async_processing():
    """Process multiple items asynchronously"""
    items = [f"item_{i}" for i in range(100)]
    
    # Send all items for async processing
    tasks = []
    for item in items:
        task = client.call_async(
            "processing_service",
            "process_item",
            item=item
        )
        tasks.append(task)
    
    # All requests sent immediately
    await asyncio.gather(*tasks)
    print(f"Sent {len(items)} items for background processing")
    
    # Listen for completion events
    # (Processing service would publish events when done)
```

## Running the Example

### Prerequisites

```bash
# Start NATS server
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222
```

### Run the Demo

```bash
# Run the async patterns demo
python example_async_patterns.py
```

### Run Specific Tests

```bash
# Test synchronous patterns
python example_async_patterns.py sync

# Test asynchronous patterns
python example_async_patterns.py async

# Test mixed patterns
python example_async_patterns.py mixed

# Performance comparison
python example_async_patterns.py performance
```

## Expected Output

```
=== Async Patterns Demo ===

1. SYNCHRONOUS RPC PATTERN
--------------------------
Processing payment for order_123...
Payment completed: txn_54321
Total time: 2.05 seconds

2. ASYNCHRONOUS RPC PATTERN
---------------------------
Email request sent, continuing...
Notification request sent, continuing...
Analytics request sent, continuing...
Total time: 0.02 seconds

[Background] Email sent to user@example.com
[Background] Notification processed
[Background] Analytics updated

3. EVENT-DRIVEN PATTERN
-----------------------
Order created: order_456
[Inventory] Reserving items for order_456
[Email] Sending confirmation for order_456
[Analytics] Tracking order_456

4. MIXED WORKFLOW
-----------------
Order created: order_789
Payment completed: txn_67890
Order confirmed. Background tasks initiated.
Total time: 2.1 seconds (critical path)

[Background] Confirmation email sent
[Background] Analytics updated
[Background] Fulfillment scheduled
```

## When to Use Each Pattern

### Use Synchronous RPC When:

- **Immediate response required** (payment processing, authentication)
- **Need confirmation** before proceeding (order validation, inventory check)
- **Error handling must be immediate** (transaction rollback)
- **Sequential dependencies** (step 2 depends on step 1 result)

### Use Asynchronous RPC When:

- **Background processing** (email sending, file processing)
- **Performance critical** (don't want to wait for non-essential operations)
- **Independent operations** (logging, analytics, notifications)
- **Bulk operations** (batch processing, data imports)

### Use Event-Driven Pattern When:

- **Multiple services need to react** to the same trigger
- **Loose coupling desired** between services
- **Workflow orchestration** with multiple steps
- **Audit trails and logging** (multiple systems tracking events)

## Performance Considerations

### Latency vs Throughput

```python
# Low latency (sync) - immediate response, higher total time
start = time.time()
for i in range(10):
    result = await client.call_rpc("service", "method", data=i)
    process_result(result)  # Immediate processing
print(f"Sync total: {time.time() - start:.2f}s")

# High throughput (async) - delayed response, lower total time
start = time.time()
tasks = []
for i in range(10):
    task = client.call_async("service", "method", data=i)
    tasks.append(task)
await asyncio.gather(*tasks)
print(f"Async send: {time.time() - start:.2f}s")
# Results processed later via events or polling
```

### Resource Utilization

- **Sync**: Lower concurrent connections, predictable resource usage
- **Async**: Higher concurrent connections, better resource utilization
- **Events**: Decoupled resource usage, better scalability

## Best Practices

1. **Critical Path Optimization**: Use sync for critical operations, async for everything else
2. **Error Handling**: Plan for async error handling via events or callbacks
3. **Monitoring**: Track both sync response times and async processing times
4. **Timeouts**: Set appropriate timeouts for sync calls
5. **Circuit Breakers**: Implement circuit breakers for external service calls
6. **Idempotency**: Make async operations idempotent for retry safety

## Next Steps

1. **[Error Handling](error-handling.md)** - Comprehensive error handling strategies
2. **[Circuit Breakers](circuit-breakers.md)** - Fault tolerance patterns
3. **[Monitoring](monitoring-setup.md)** - Performance monitoring and metrics
4. **[Load Testing](load-testing.md)** - Testing async patterns under load
5. **[Production Patterns](../deployment/production-patterns.md)** - Production deployment considerations

# Saga Pattern in Cliffracer

The Saga pattern is a way to manage distributed transactions across multiple microservices. Instead of using traditional ACID transactions that lock resources, sagas use a sequence of local transactions coordinated through either orchestration or choreography.

## Overview

Cliffracer provides comprehensive support for the Saga pattern with:

- **Orchestration-based Sagas**: A central coordinator manages the saga workflow
- **Choreography-based Sagas**: Services coordinate through events
- **Automatic Compensation**: Failed transactions are automatically rolled back
- **State Persistence**: Saga state can be persisted for recovery
- **Retry Logic**: Built-in retry mechanisms for transient failures

## Key Components

### SagaCoordinator

The orchestrator that manages saga execution:

```python
from cliffracer.patterns.saga import SagaCoordinator, SagaStep

coordinator = SagaCoordinator(service)

# Define saga steps
coordinator.define_saga("order_processing", [
    SagaStep(
        name="create_order",
        service="order_service",
        action="create_order",
        compensation="cancel_order",
        timeout=10.0,
        retry_count=3
    ),
    SagaStep(
        name="process_payment",
        service="payment_service",
        action="process_payment",
        compensation="refund_payment"
    )
])
```

### SagaParticipant

Base class for services that participate in sagas:

```python
from cliffracer.patterns.saga import SagaParticipant

class PaymentService(CliffracerService, SagaParticipant):
    def _register_handlers(self):
        @self.rpc
        async def process_payment(self, saga_id: str, data: dict) -> dict:
            # Process payment
            return {"result": {"payment_id": "PAY-123"}}
        
        @self.rpc
        async def refund_payment(self, saga_id: str, data: dict) -> dict:
            # Compensation logic
            return {"result": {"status": "refunded"}}
```

### ChoreographySaga

For event-driven saga coordination:

```python
from cliffracer.patterns.saga import ChoreographySaga

saga = ChoreographySaga(service)

@saga.on_event("order.created")
@saga.emits("payment.requested", "order.failed")
async def handle_order_created(data: dict):
    # Process and emit next event
    return {"order_id": data["order_id"]}
```

## Orchestration Pattern

In the orchestration pattern, a central coordinator manages the entire saga workflow.

### Advantages
- Clear workflow visibility
- Easier to understand and debug
- Centralized error handling
- Simple to add new steps

### Example: E-commerce Order Processing

```python
# Define the saga workflow
coordinator.define_saga("order_processing", [
    SagaStep("create_order", "order_service", "create_order", "cancel_order"),
    SagaStep("reserve_inventory", "inventory_service", "reserve_inventory", "release_inventory"),
    SagaStep("process_payment", "payment_service", "process_payment", "refund_payment"),
    SagaStep("create_shipment", "shipping_service", "create_shipment", "cancel_shipment"),
    SagaStep("confirm_order", "order_service", "confirm_order", None)
])

# Start the saga
result = await coordinator.start_saga("order_processing", {
    "customer_id": "CUST-123",
    "items": [{"product_id": "PROD-1", "quantity": 2}],
    "total_amount": 99.99
})
```

## Choreography Pattern

In the choreography pattern, services coordinate through events without a central coordinator.

### Advantages
- Loose coupling between services
- Services can evolve independently
- Better scalability
- No single point of failure

### Example: Event-Driven Order Processing

```python
@saga.on_event("order.created")
@saga.emits("inventory.reserved", "inventory.insufficient")
async def reserve_inventory(data: dict):
    if check_inventory(data["items"]):
        reserve_items(data["items"])
        return {"status": "reserved"}
    else:
        raise Exception("Insufficient inventory")

@saga.on_event("inventory.reserved")
@saga.emits("payment.completed", "payment.failed")
async def process_payment(data: dict):
    payment_result = await charge_customer(data["total_amount"])
    return {"payment_id": payment_result.id}

@saga.on_event("payment.completed")
async def create_shipment(data: dict):
    shipment = await schedule_delivery(data["order_id"])
    return {"shipment_id": shipment.id}
```

## Compensation Strategies

### 1. Backward Recovery
Compensate in reverse order when a step fails:

```python
# If step 3 fails:
# Execute: compensate_step_2 -> compensate_step_1
```

### 2. Forward Recovery
Try to complete the saga using alternative paths:

```python
SagaStep(
    name="primary_payment",
    service="payment_service",
    action="charge_credit_card",
    compensation="refund_credit_card",
    fallback="charge_backup_method"  # Alternative action
)
```

### 3. Pivot Transactions
Some steps can't be compensated and must be handled specially:

```python
SagaStep(
    name="send_email",
    service="notification_service",
    action="send_order_confirmation",
    compensation=None,  # Can't unsend email
    compensatable=False
)
```

## Error Handling

### Retry Logic

```python
SagaStep(
    name="external_api_call",
    service="integration_service",
    action="call_third_party",
    retry_count=3,
    retry_delay=2.0,  # Exponential backoff
    timeout=30.0
)
```

### State Recovery

```python
# Sagas can be resumed after crashes
saga_state = await coordinator.get_saga_state(saga_id)
if saga_state.state == SagaState.RUNNING:
    await coordinator.resume_saga(saga_id)
```

## Best Practices

### 1. Idempotent Operations
Make all saga actions idempotent:

```python
async def process_payment(self, saga_id: str, data: dict) -> dict:
    # Check if already processed
    existing = await self.get_payment_by_saga(saga_id)
    if existing:
        return {"result": {"payment_id": existing.id}}
    
    # Process payment
    payment = await self.charge_customer(data)
    return {"result": {"payment_id": payment.id}}
```

### 2. Timeout Management
Set appropriate timeouts for each step:

```python
SagaStep(
    name="slow_operation",
    timeout=60.0,  # Longer timeout for slow operations
    retry_count=1  # Fewer retries for slow ops
)
```

### 3. Correlation IDs
Always propagate correlation IDs:

```python
async def handle_saga_step(self, saga_id: str, correlation_id: str, data: dict):
    # Set correlation context
    set_correlation_id(correlation_id)
    
    # Process with correlation
    logger.info(f"Processing saga step: {saga_id}")
```

### 4. State Persistence
Enable persistence for production:

```python
coordinator = SagaCoordinator(
    service,
    persistence_enabled=True,
    state_store=PostgresStateStore()
)
```

## Complete Examples

### Travel Booking Saga

See [examples/saga/travel_saga.py](../../examples/saga/travel_saga.py) for a complete example that books flights, hotels, and car rentals with automatic compensation.

### E-commerce Order Saga

See [examples/saga/order_saga.py](../../examples/saga/order_saga.py) for both orchestration and choreography implementations of order processing.

## Testing Sagas

```python
import pytest
from cliffracer.testing import SagaTestHarness

@pytest.mark.asyncio
async def test_order_saga_compensation():
    harness = SagaTestHarness()
    
    # Configure service to fail
    harness.configure_failure("payment_service", "process_payment")
    
    # Start saga
    result = await harness.start_saga("order_processing", test_data)
    
    # Verify compensation
    assert result.state == SagaState.COMPENSATED
    assert harness.was_called("order_service", "cancel_order")
    assert harness.was_called("inventory_service", "release_inventory")
```

## Monitoring and Observability

### Saga Metrics

```python
# Automatic metrics collection
- saga.started{type="order_processing"}
- saga.completed{type="order_processing"}
- saga.failed{type="order_processing"}
- saga.compensated{type="order_processing"}
- saga.step.duration{step="process_payment"}
```

### Distributed Tracing

All saga operations are automatically traced with correlation IDs:

```
[SAGA-123] Starting saga: order_processing
[SAGA-123] Step 1/5: create_order - SUCCESS
[SAGA-123] Step 2/5: reserve_inventory - SUCCESS
[SAGA-123] Step 3/5: process_payment - FAILED
[SAGA-123] Compensating: release_inventory
[SAGA-123] Compensating: cancel_order
[SAGA-123] Saga compensated successfully
```

## Migration Guide

### From Traditional Transactions

```python
# Before: Traditional transaction
async with db.transaction():
    order = await create_order(data)
    await reserve_inventory(order.items)
    await process_payment(order.total)
    await create_shipment(order)

# After: Saga pattern
result = await coordinator.start_saga("order_processing", data)
```

### From Manual Compensation

```python
# Before: Manual error handling
try:
    order = await create_order(data)
    try:
        payment = await process_payment(order)
        try:
            shipment = await create_shipment(order)
        except:
            await refund_payment(payment)
            await cancel_order(order)
            raise
    except:
        await cancel_order(order)
        raise
except:
    # Handle failure

# After: Automatic compensation
result = await coordinator.start_saga("order_processing", data)
```

## Performance Considerations

- **Orchestration**: Better for complex workflows with many steps
- **Choreography**: Better for simple workflows with high throughput
- **Hybrid**: Use orchestration for critical paths, choreography for notifications

## Troubleshooting

### Common Issues

1. **Compensation Failures**: Ensure compensations are idempotent
2. **Timeout Issues**: Adjust timeouts based on service SLAs
3. **State Corruption**: Enable persistence and implement recovery
4. **Event Ordering**: Use event sourcing for choreography patterns

### Debug Mode

```python
coordinator = SagaCoordinator(
    service,
    debug=True,  # Verbose logging
    trace_enabled=True  # Detailed execution traces
)
```
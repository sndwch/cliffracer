# Correlation ID Propagation in Cliffracer 🔍

This example demonstrates how correlation IDs enable distributed request tracing across microservices.

## What are Correlation IDs? 🤔

Correlation IDs are unique identifiers that follow a request as it flows through multiple services in a distributed system. They're essential for:

- **Distributed Tracing**: Track a single request across multiple services
- **Debugging**: Find all logs related to a specific user request
- **Performance Analysis**: Measure end-to-end request latency
- **Error Investigation**: Trace errors back to their origin

## How It Works in Cliffracer 🚀

Cliffracer automatically propagates correlation IDs through:

1. **RPC Calls**: IDs flow through NATS messaging
2. **HTTP Requests**: IDs in `X-Correlation-ID` headers
3. **WebSocket Connections**: IDs in connection metadata
4. **Events**: IDs included in published events
5. **Logs**: IDs automatically included in all log entries

## Running the Example 🏃‍♂️

1. **Start the services**:
   ```bash
   python correlation_example.py
   ```

2. **Make a request with correlation ID**:
   ```bash
   curl -X POST http://localhost:8081/orders \
     -H "Content-Type: application/json" \
     -H "X-Correlation-ID: my-test-request-123" \
     -d '{
       "product_id": "PROD-001",
       "quantity": 2,
       "customer_id": "CUST-VIP"
     }'
   ```

3. **Watch the logs** - You'll see the same correlation ID across all services:
   ```
   2024-01-15 10:30:45.123 | INFO | order_service | my-test-request-123 | HTTP order request received
   2024-01-15 10:30:45.125 | INFO | order_service | my-test-request-123 | Checking inventory...
   2024-01-15 10:30:45.127 | INFO | inventory_service | my-test-request-123 | Checking availability for PROD-001
   2024-01-15 10:30:45.130 | INFO | order_service | my-test-request-123 | Calculating order price...
   2024-01-15 10:30:45.132 | INFO | pricing_service | my-test-request-123 | Price calculation: total=$53.98
   2024-01-15 10:30:45.135 | INFO | order_service | my-test-request-123 | Processing payment...
   2024-01-15 10:30:45.138 | INFO | payment_service | my-test-request-123 | Payment PAY-0001 completed
   ```

## Features Demonstrated 🎯

### 1. Automatic ID Generation
If no correlation ID is provided, Cliffracer generates one:
```bash
curl -X POST http://localhost:8081/orders \
  -H "Content-Type: application/json" \
  -d '{"product_id": "PROD-001", "quantity": 1, "customer_id": "CUST-001"}'

# Response includes generated ID:
# X-Correlation-ID: corr_a1b2c3d4e5f6g7h8
```

### 2. Service-to-Service Propagation
When OrderService calls InventoryService:
```python
# Correlation ID automatically included
result = await self.call_rpc(
    "inventory_service", 
    "check_availability",
    product_id=product_id,
    quantity=quantity
)
```

### 3. Event Propagation
Events include correlation IDs:
```python
await self.publish_event(
    "orders.confirmed",
    order_id=order_id,
    customer_id=customer_id
)
# Event includes current correlation ID
```

### 4. Structured Logging
All logs automatically include correlation ID:
```python
logger.info(f"Creating order for product {product_id}")
# Output: 2024-01-15 10:30:45 | INFO | order_service | corr_123 | Creating order for product PROD-001
```

## Using Correlation IDs in Your Services 🛠️

### Basic Setup
```python
from cliffracer import (
    NATSService,
    ServiceConfig,
    rpc,
    setup_correlation_logging,
    with_correlation_id
)

# Enable correlation logging
setup_correlation_logging("my_service", "INFO")

class MyService(NATSService):
    @rpc
    async def my_method(self, data: str, correlation_id: str = None):
        # correlation_id is automatically injected
        logger.info(f"Processing {data}")
        return {"result": "success"}
```

### HTTP Services
```python
from cliffracer import HTTPNATSService, post

class MyHTTPService(HTTPNATSService):
    @post("/api/endpoint")
    @with_correlation_id
    async def create_something(self, request_data: dict, correlation_id: str = None):
        # Correlation ID extracted from headers or generated
        result = await self.call_rpc("other_service", "process", **request_data)
        return {"status": "created", "correlation_id": correlation_id}
```

### Manual Correlation ID Management
```python
from cliffracer import get_correlation_id, set_correlation_id, create_correlation_id

# Get current correlation ID
current_id = get_correlation_id()

# Set a specific correlation ID
set_correlation_id("custom-id-123")

# Create a new correlation ID
new_id = create_correlation_id()
```

## Benefits 🎉

1. **Simplified Debugging**: Find all logs for a specific request instantly
2. **Performance Monitoring**: Track request flow and identify bottlenecks
3. **Error Tracking**: Trace errors across service boundaries
4. **Audit Trail**: Complete visibility into request processing
5. **Zero Configuration**: Works out of the box with Cliffracer services

## Best Practices 📚

1. **Always Log with Context**: Include relevant data in your logs
   ```python
   logger.info(f"Processing order {order_id} for customer {customer_id}")
   ```

2. **Preserve IDs in Async Tasks**: Correlation context is preserved in async operations
   ```python
   async def background_task():
       # Correlation ID is maintained
       logger.info("Background task running")
   ```

3. **Include in Error Responses**: Help clients track issues
   ```python
   return {
       "error": "Invalid request",
       "correlation_id": get_correlation_id()
   }
   ```

4. **Use in Monitoring**: Set up alerts based on correlation IDs
   ```python
   metrics.increment("orders.created", tags={"correlation_id": correlation_id})
   ```

## Architecture Overview 🏗️

```
┌─────────────┐     X-Correlation-ID: abc123     ┌──────────────┐
│   Client    │ ──────────────────────────────> │ Order Service │
└─────────────┘                                  └──────┬───────┘
                                                        │
                                                        │ correlation_id: abc123
                                                        ▼
                                                ┌──────────────┐
                                                │  Inventory   │
                                                │   Service    │
                                                └──────┬───────┘
                                                       │
                                                       │ correlation_id: abc123
                                                       ▼
                                                ┌──────────────┐
                                                │   Pricing    │
                                                │   Service    │
                                                └──────┬───────┘
                                                       │
                                                       │ correlation_id: abc123
                                                       ▼
                                                ┌──────────────┐
                                                │   Payment    │
                                                │   Service    │
                                                └──────────────┘
```

## Troubleshooting 🔧

### Missing Correlation IDs
- Check that correlation middleware is added to HTTP services
- Ensure handlers accept `correlation_id` parameter
- Verify logging is configured with `setup_correlation_logging()`

### IDs Not Propagating
- Confirm services are using Cliffracer's `call_rpc()` methods
- Check that receiving services have correlation ID support
- Verify NATS connection is established

### Performance Impact
- Correlation IDs add minimal overhead (<1ms per request)
- Context storage is efficient using Python's contextvars
- No additional network calls required

Enjoy distributed tracing with Cliffracer! 🚀
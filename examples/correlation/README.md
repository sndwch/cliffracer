# Correlation ID propagation

A correlation id follows one request through every service that handles it, so
the logs from a single request can be found together.

`correlation_example.py` runs four services: order (port 8081), inventory
(8082), pricing (8083) and payment (8084). An order request passes through all
four under one id.

## Running it

```bash
python correlation_example.py
```

Send a request with your own id:

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

The same id then appears in every service's log lines for that request:

```
order_service     | my-test-request-123 | HTTP order request received
order_service     | my-test-request-123 | Checking inventory...
inventory_service | my-test-request-123 | Checking availability for PROD-001
pricing_service   | my-test-request-123 | Price calculation: total=$53.98
payment_service   | my-test-request-123 | Payment PAY-0001 completed
```

Send the same request without the header and the middleware generates an id.
Generated ids are the string `corr_` followed by 16 hex characters, for example
`corr_a1b2c3d4e5f6a7b8`. It comes back on the response as `X-Correlation-ID`.

## Where the id comes from and where it goes

`CorrelationMiddleware` reads the incoming header, and `HttpExtension` adds that
middleware to its app at setup.

From there the id travels on the NATS message headers, so `call_rpc`,
`call_async` and `publish_event` all carry it to the next service.

## Reading the id in a handler

A handler that declares a `correlation_id` parameter is passed the current id:

```python
class MyService(CliffracerService):
    @rpc
    async def my_method(self, data: str, correlation_id: str = "") -> dict[str, str]:
        logger.info(f"Processing {data}")
        return {"result": "success"}
```

The container checks the handler's signature and passes `correlation_id` to the
handlers that declare it.

Any code can read the id directly:

```python
from cliffracer import get_correlation_id, set_correlation_id, create_correlation_id

current_id = get_correlation_id()
set_correlation_id("custom-id-123")
new_id = create_correlation_id()
```

Returning it in an error response gives the caller something to quote:

```python
return {
    "error": "Invalid request",
    "correlation_id": get_correlation_id(),
}
```

## Logging

`setup_correlation_logging` from `cliffracer_logging` adds the id to every log
record:

```python
from cliffracer_logging import setup_correlation_logging

setup_correlation_logging("my_service", "INFO")
```

The id is held in a `contextvars` context variable, so it stays with the request
across `await` points and into tasks started from the handler.

## Request flow

```
Client --X-Correlation-ID: abc123--> Order (8081)
                                       |
                                       +--> Inventory (8082)
                                       +--> Pricing   (8083)
                                       +--> Payment   (8084)

Every hop carries correlation_id: abc123.
```

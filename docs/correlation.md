# Correlation ID Tracking

A correlation ID travels with a request: generated on ingress or extracted from an incoming header, propagated across service boundaries via NATS message headers, and accessible in any handler through `CorrelationContext.get()`.

## How It Works

A correlation ID travels with a request through its entire lifecycle:
1.  **Generation:** An ID is generated on the way in, or extracted from an incoming header.
2.  **Propagation:** It is automatically propagated across service boundaries via NATS message headers.
3.  **Context:** It is available in any handler using `CorrelationContext.get()`.
4.  **Logging:** `cliffracer-logging` automatically attaches the ID to log lines.

## HTTP Ingress

If you are using `cliffracer-http`, the `HttpExtension` automatically injects `CorrelationMiddleware` into the FastAPI application.

When an HTTP request arrives, the middleware checks for a standard correlation header (e.g., `X-Correlation-ID`). If it doesn't exist, it generates a new one.

```python
from cliffracer_http import HttpExtension
from cliffracer import CliffracerService, rpc

class GatewayService(CliffracerService):
    http = HttpExtension(port=8080)
    
    @http.get("/order/{order_id}")
    async def get_order(self, order_id: str):
        # The correlation ID is already established for this async context
        return await self.call_rpc("orders", "fetch", order_id=order_id)
```

## Accessing the Context

Any handler (RPC, Listener, or HTTP) can access the current correlation ID without passing it manually through function arguments:

```python
from cliffracer.core.correlation import CorrelationContext, get_correlation_id

@rpc
async def fetch(self, order_id: str) -> dict[str, str]:
    # Retrieve the correlation ID for the current execution context
    corr_id = get_correlation_id() 
    # or CorrelationContext.get()
    
    self.logger.info(f"[{corr_id}] Fetching order {order_id}")
```

## Logging Integration

If you use `cliffracer-logging`, you don't even need to format the ID into your log messages manually. 

When you use `get_correlation_logger()` or setup the context logger, every log emission automatically pulls the correlation ID from the current `asyncio` context and attaches it to the log record (and injects it into structured JSON logs).

This ensures that when you search your log aggregator (like Datadog, ELK, or Grafana Loki) for a specific correlation ID, you see the full trace of the request across the gateway, the RPC handlers, and the background event listeners.

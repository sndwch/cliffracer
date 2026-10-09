# Correlation ID Tracking

A correlation ID travels with a request: generated on ingress or extracted from an incoming header, propagated across service boundaries via NATS message headers, and accessible in any handler through `CorrelationContext.get()`.

## How It Works

A correlation ID travels with a request through its entire lifecycle:
1.  **Generation:** An ID is generated on the way in, or extracted from an incoming header.
2.  **Propagation:** It is automatically propagated across service boundaries via NATS message headers.
3.  **Context:** It is available in any handler using `CorrelationContext.get()`.
4.  **Logging:** `cliffracer-logging` automatically attaches the ID to log lines.

An ID taken from a header or a payload field is printable text of at most 256 characters. A value
with a control character (an ANSI escape, a tab, a vertical tab, a form feed, NEL, U+2028 or
U+2029, or a CR or LF) or one longer than that is not used: the next header or the payload field
is tried, and when none is left a new ID is generated. A UUID, a W3C `traceparent`, and an ID with
spaces, punctuation or letters of any script are accepted.

An ID a service sends is held to the same rule. The ID passed as `correlation_id=`, the one in a
`ServiceClient`'s `headers=` and the ambient one set with `CorrelationContext.set()` are each used
only if they pass; one that fails is treated as absent, a warning says so without repeating it
raw, and the next source is tried, ending in a new ID. The message and the log line carry the ID
that was used, and the receiver finds the same ID.

## Accessing the Context

Any handler (RPC or Listener) can access the current correlation ID without passing it manually through function arguments:

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

This ensures that when you search your log aggregator (like Datadog, ELK, or Grafana Loki) for a specific correlation ID, you see the full trace of the request across the RPC handlers and the background event listeners.

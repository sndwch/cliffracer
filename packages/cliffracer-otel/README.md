# cliffracer-otel

Distributed Tracing and OpenTelemetry instrumentation for cliffracer services.

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_otel import OtelExtension

class OrderService(CliffracerService):
    otel = OtelExtension()
```

`OtelExtension` automatically instruments inbound message handling and outbound RPC / event calls with OpenTelemetry spans:

- **Inbound Spans**:
  - Extracts W3C `traceparent` and `tracestate` context from incoming NATS headers in `worker_setup`.
  - Creates a server span (`SpanKind.SERVER`) and attaches the active context so handler execution and child spans inherit the trace.
  - Links `cliffracer.kind`, `cliffracer.subject`, and `cliffracer.correlation_id` as span attributes.
  - Marks status as `ERROR` and records exceptions on handler failure or `RejectMessage`.
  - Ends the span and detaches the context token in `worker_teardown`.

- **Outbound Spans**:
  - Intercepts outgoing `call_rpc`, `call_async`, `call_rpc_no_wait`, `publish_event`, and `broadcast` in `before_call`.
  - Starts a client span (`SpanKind.CLIENT` for RPCs, `SpanKind.PRODUCER` for events/broadcasts).
  - Injects W3C `traceparent` into outgoing headers so downstream microservices continue the distributed trace waterfall.
  - Completes the outbound span and records errors in `after_call`.

- **Health Details**:
  - Exposes telemetry counters (`spans_total`, `errors_total`, `active_spans`) on `/health`.

## Custom Tracer Provider

You can pass a custom `TracerProvider` or `Tracer` directly:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

class OrderService(CliffracerService):
    otel = OtelExtension(tracer_provider=provider)
```

If no provider is supplied, `OtelExtension` uses or initializes the global OpenTelemetry tracer provider.

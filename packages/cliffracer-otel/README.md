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
  - Starts the span and attaches the active context so handler execution and child spans inherit the trace. The span is named `{kind} {handler}` (`rpc get_order`, `async_rpc reindex`, `event on_order`, `timer sweep`), so every subject that reaches one handler is one group in a tracing backend; a context that names no handler is named for its kind alone. The subject is kept in `messaging.destination.name` and `cliffracer.subject`.
  - An `rpc` or `async_rpc` span is `SpanKind.SERVER`. An `event` delivery, from core NATS or JetStream, is `SpanKind.CONSUMER`: a handler that consumes a message without answering it is not a request being served. A `timer` span is `SpanKind.INTERNAL`: it fires from the service's own clock, with no message and no peer.
  - A `describe` request is introspection, not application traffic: it starts no span, so neither `spans_total` nor `errors_total` counts it, whether it is answered or fails.
  - Links `cliffracer.kind`, `cliffracer.subject`, and `cliffracer.correlation_id` as span attributes.
  - For a message the broker delivered (`rpc`, `async_rpc`, `event`), sets `messaging.system` (`nats`), `messaging.destination.name` (the subject) and `messaging.operation.type` (`process`). A timer's span carries none of them.
  - Marks status as `ERROR` and records exceptions on handler failure or `RejectMessage`.
  - Ends the span and detaches the context token in `worker_teardown`.

- **Outbound Spans**:
  - Intercepts outgoing `call_rpc`, `call_async`, `call_rpc_no_wait`, `stream_rpc`, `publish_event`, and `broadcast` in `before_call`. A `stream_rpc` span ends when the stream is opened, not when its last item arrives.
  - Starts a client span (`SpanKind.CLIENT` for RPCs, `SpanKind.PRODUCER` for events/broadcasts), named `{kind} {subject}` (`call_rpc billing.charge`): a caller has no handler of its own to name it by.
  - Sets `messaging.system` (`nats`), `messaging.destination.name` (the subject) and `messaging.operation.type` (`send`) on every outbound span.
  - Injects W3C `traceparent` into outgoing headers so downstream microservices continue the distributed trace waterfall.
  - Completes the outbound span and records errors in `after_call`.

- **Attributes**: the names written on a span are `cliffracer.kind`, `cliffracer.subject`, `cliffracer.correlation_id`, `messaging.system`, `messaging.destination.name` and `messaging.operation.type`. No version of the OpenTelemetry semantic conventions is pinned, and no `rpc.*` attribute is set.

- **Health Details**:
  - Exposes telemetry counters (`spans_total`, `errors_total`, `active_spans`) on `/health`. They count every dispatch the extension started a span for, and every failure of one, whether or not the sampler records the span, so a sampler that drops spans changes what is exported and not these numbers. A `describe` request starts no span and is in neither count.

## Custom Tracer Provider

You can pass a custom `TracerProvider` or `Tracer` directly:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from cliffracer import SharedDependency

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

class OrderService(CliffracerService):
    otel = OtelExtension(tracer_provider=SharedDependency(provider))
```

One provider is shared by every instance of the service, which is what
`SharedDependency` declares. Passing the provider bare is refused, because an
extension argument is otherwise rebuilt per instance and a tracer provider
cannot be.

If no provider is supplied, `OtelExtension` uses the global OpenTelemetry tracer provider, or
installs one when the process has none. The one it installs has no span processor, so its spans are
recorded and not exported until a processor is added (the form above, or your own OpenTelemetry
setup before the service starts); setup logs a warning saying so. Its Resource carries the name of
the service that installed it, and a process has one global provider, so a second service in the
same process that supplies none shares it: its spans carry the first service's `service.name`, and
setup logs a warning naming both. Give each service its own `tracer_provider` to report each under
its own name.

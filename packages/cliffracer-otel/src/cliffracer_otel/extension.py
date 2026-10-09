"""OpenTelemetry distributed tracing extension."""

from __future__ import annotations

from typing import Any

from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from cliffracer.core.extension import (
    Extension,
    ExtensionSetupContext,
    SharedDependency,
    WorkerContext,
)

# Entrypoint kinds delivered by the broker. A timer fires from the service's own clock, so a span for
# it names no messaging system and no destination.
_BROKER_KINDS = frozenset({"rpc", "async_rpc", "event"})

# Entrypoint kinds that get no span: a `describe` request is introspection, not application traffic.
_SPANLESS_KINDS = frozenset({"describe"})

# The span kind of an inbound dispatch, by entrypoint kind. An RPC answers a request, so it is SERVER,
# the default. An event is consumed without an answer. A timer fires from the service's own clock, with
# no message and no peer.
_INBOUND_SPAN_KINDS = {"event": trace.SpanKind.CONSUMER, "timer": trace.SpanKind.INTERNAL}


def _inbound_span_name(ctx: WorkerContext) -> str:
    """The handler's name after the kind, so every subject that reaches it is one group.

    A context that names no handler is named for its kind alone: falling back to the subject would
    bring back one group per subject.
    """
    handler = ctx.data.get("handler_name")
    return f"{ctx.kind} {handler}" if handler else ctx.kind


def _set_messaging_attributes(span: trace.Span, ctx: WorkerContext, operation: str) -> None:
    """Add the OpenTelemetry messaging attributes a tracing backend groups NATS traffic by."""
    span.set_attribute("messaging.system", "nats")
    span.set_attribute("messaging.operation.type", operation)
    if ctx.subject:
        span.set_attribute("messaging.destination.name", ctx.subject)


# The provider `OtelExtension.setup` installed as the process-global one when none was supplied, and
# the service whose name its Resource carries. OpenTelemetry allows one global provider per process,
# so a second service in the process that supplies none shares it, and its spans carry that name.
_installed: tuple[TracerProvider, str] | None = None


def _record_installed(provider: TracerProvider, service_name: str) -> None:
    global _installed
    _installed = (provider, service_name)


class OtelExtension(Extension):
    """Distributed tracing extension wrapping message dispatch and calls in OTel spans.

    Extracts W3C trace context from incoming headers to record spans across worker hooks:
    SERVER for an RPC, CONSUMER for an event, INTERNAL for a timer, named for the handler that ran,
    and none for a `describe` request. Injects outbound W3C traceparent headers into the CLIENT or
    PRODUCER spans of outgoing RPC and event publications.
    """

    def __init__(
        self,
        service_name: str | None = None,
        tracer_provider: trace.TracerProvider
        | SharedDependency[trace.TracerProvider]
        | None = None,
        tracer: trace.Tracer | None = None,
    ) -> None:
        self._service_name = service_name
        self._tracer_name = service_name
        self._custom_tracer_provider = (
            tracer_provider.value
            if isinstance(tracer_provider, SharedDependency)
            else tracer_provider
        )
        self._custom_tracer = tracer
        self._tracer: trace.Tracer | None = tracer
        self._propagator = TraceContextTextMapPropagator()
        self._span_count: int = 0
        self._error_count: int = 0
        self._active_spans: int = 0

    @property
    def tracer(self) -> trace.Tracer:
        """Return the active tracer instance, initializing a fallback if not yet set."""
        if self._tracer is None:
            name = self._tracer_name or "cliffracer"
            if self._custom_tracer is not None:
                self._tracer = self._custom_tracer
            elif self._custom_tracer_provider is not None:
                self._tracer = self._custom_tracer_provider.get_tracer(name)
            else:
                self._tracer = trace.get_tracer(name)
        return self._tracer

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Initialize OpenTelemetry tracer provider if not set and resolve named tracer."""
        # Per-service counters start in setup(), which runs once for each bound instance.
        self._span_count = 0
        self._error_count = 0
        self._active_spans = 0

        if self._tracer_name is None:
            service_config = getattr(ctx, "service_config", None)
            name = getattr(service_config, "name", None)
            self._tracer_name = name or "cliffracer"

        if self._custom_tracer is not None:
            self._tracer = self._custom_tracer
        elif self._custom_tracer_provider is not None:
            self._tracer = self._custom_tracer_provider.get_tracer(self._tracer_name)
        else:
            current_provider = trace.get_tracer_provider()
            # If default proxy provider without backing implementation, create standard provider
            if (
                isinstance(current_provider, trace.ProxyTracerProvider)
                and getattr(current_provider, "_provider", None) is None
            ):
                provider = TracerProvider(
                    resource=Resource.create({"service.name": self._tracer_name})
                )
                trace.set_tracer_provider(provider)
                _record_installed(provider, self._tracer_name)
                self._service_log.warning(
                    f"OtelExtension for {self._tracer_name!r} was given no tracer provider, so it "
                    f"installed a process-wide one with no span processor: spans are recorded and "
                    f"not exported unless one is added. Pass tracer_provider=SharedDependency(...) "
                    f"with an exporter, or configure OpenTelemetry before the service starts."
                )
                self._tracer = provider.get_tracer(self._tracer_name)
            else:
                if _installed is not None and current_provider is _installed[0]:
                    owner = _installed[1]
                    if owner != self._tracer_name:
                        self._service_log.warning(
                            f"OtelExtension for {self._tracer_name!r} shares the tracer provider "
                            f"installed for {owner!r}, so its spans carry service.name={owner!r}. "
                            f"Pass tracer_provider=SharedDependency(...) with its own Resource to "
                            f"report it under its own name."
                        )
                self._tracer = trace.get_tracer(self._tracer_name)

    async def worker_setup(self, ctx: WorkerContext) -> None:
        """Extract W3C trace context, start the inbound span, and attach context token."""
        if ctx.kind in _SPANLESS_KINDS:
            return

        # Normalize header keys to lowercase for robust W3C extraction
        carrier = {k.lower(): str(v) for k, v in ctx.headers.items()} if ctx.headers else {}
        extracted_ctx = self._propagator.extract(carrier=carrier)

        span = self.tracer.start_span(
            name=_inbound_span_name(ctx),
            context=extracted_ctx,
            kind=_INBOUND_SPAN_KINDS.get(ctx.kind, trace.SpanKind.SERVER),
        )

        span.set_attribute("cliffracer.kind", ctx.kind)
        if ctx.subject:
            span.set_attribute("cliffracer.subject", ctx.subject)
        if ctx.correlation_id:
            span.set_attribute("cliffracer.correlation_id", ctx.correlation_id)
        if ctx.kind in _BROKER_KINDS:
            _set_messaging_attributes(span, ctx, "process")

        active_ctx = trace.set_span_in_context(span, extracted_ctx)
        token = context.attach(active_ctx)

        # Store in ctx.data dictionary to isolate across concurrent coroutines
        ctx.data["_otel_inbound_span"] = span
        ctx.data["_otel_inbound_token"] = token
        self._active_spans += 1

    async def worker_result(
        self, ctx: WorkerContext, result: object | None, exc: BaseException | None
    ) -> None:
        """Record exception (including RejectMessage) and set span status."""
        span: trace.Span | None = ctx.data.get("_otel_inbound_span")
        if span is not None:
            # Counted whether or not the sampler is recording this span: the counters say what
            # was dispatched and what failed, which a sampler does not change.
            self._span_count += 1
            # An event the dispatcher found invalid raises nothing: it dead-letters the payload and
            # marks the dispatch `ctx.data["outcome"] = "invalid"`, where an invalid RPC raises a
            # refusal. Both end in error.
            invalid = exc is None and ctx.data.get("outcome") == "invalid"
            if exc is not None or invalid:
                self._error_count += 1
            if span.is_recording():
                if exc is not None:
                    span.record_exception(exc)
                    span.set_status(trace.StatusCode.ERROR, str(exc))
                elif invalid:
                    span.set_status(trace.StatusCode.ERROR, "invalid payload")
                else:
                    span.set_status(trace.StatusCode.OK)

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        """End the inbound span and detach context token."""
        token = ctx.data.pop("_otel_inbound_token", None)
        if token is not None:
            context.detach(token)

        span: trace.Span | None = ctx.data.pop("_otel_inbound_span", None)
        if span is not None:
            span.end()
            self._active_spans = max(0, self._active_spans - 1)

    async def before_call(self, ctx: WorkerContext) -> None:
        """Start client/producer span and inject W3C traceparent into ctx.headers."""
        if ctx.kind in ("publish_event", "broadcast", "event"):
            span_kind = trace.SpanKind.PRODUCER
        else:
            span_kind = trace.SpanKind.CLIENT

        span_name = f"{ctx.kind} {ctx.subject}" if ctx.subject else ctx.kind
        span = self.tracer.start_span(
            name=span_name,
            kind=span_kind,
        )

        span.set_attribute("cliffracer.kind", ctx.kind)
        if ctx.subject:
            span.set_attribute("cliffracer.subject", ctx.subject)
        if ctx.correlation_id:
            span.set_attribute("cliffracer.correlation_id", ctx.correlation_id)
        _set_messaging_attributes(span, ctx, "send")

        active_ctx = trace.set_span_in_context(span)
        token = context.attach(active_ctx)

        self._propagator.inject(carrier=ctx.headers, context=active_ctx)

        ctx.data["_otel_outbound_span"] = span
        ctx.data["_otel_outbound_token"] = token
        self._active_spans += 1

    async def after_call(self, ctx: WorkerContext, result: Any, exc: BaseException | None) -> None:
        """Complete outbound span, record error if present, and detach token."""
        span: trace.Span | None = ctx.data.pop("_otel_outbound_span", None)
        token = ctx.data.pop("_otel_outbound_token", None)
        try:
            if span is not None:
                self._span_count += 1
                if exc is not None:
                    self._error_count += 1
                if span.is_recording():
                    if exc is not None:
                        span.record_exception(exc)
                        span.set_status(trace.StatusCode.ERROR, str(exc))
                    else:
                        span.set_status(trace.StatusCode.OK)
        finally:
            if token is not None:
                context.detach(token)
            if span is not None:
                span.end()
                self._active_spans = max(0, self._active_spans - 1)

    def health_details(self) -> dict[str, Any] | None:
        """Provide telemetry metrics for the /health endpoint."""
        if self._tracer is None:
            return None
        return {
            "active_spans": self._active_spans,
            "spans_total": self._span_count,
            "errors_total": self._error_count,
            "tracer_name": self._tracer_name or "cliffracer",
        }

    def info_details(self) -> dict[str, Any] | None:
        """Provide static telemetry metadata."""
        return {
            "tracer_name": self._tracer_name or "cliffracer",
            "propagator": "tracecontext",
        }

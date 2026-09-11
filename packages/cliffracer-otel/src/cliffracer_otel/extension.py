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


class OtelExtension(Extension):
    """Distributed tracing extension wrapping message dispatch and calls in OTel spans.

    Extracts W3C trace context from incoming headers to record SERVER spans across
    worker hooks, and injects outbound W3C traceparent headers into CLIENT spans
    during outgoing RPC and event publications.
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
        # Per-instance state reset in setup() per Extension shallow copy lifecycle
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
                self._tracer = provider.get_tracer(self._tracer_name)
            else:
                self._tracer = trace.get_tracer(self._tracer_name)

    async def worker_setup(self, ctx: WorkerContext) -> None:
        """Extract W3C trace context, start server span, and attach context token."""
        # Normalize header keys to lowercase for robust W3C extraction
        carrier = {k.lower(): str(v) for k, v in ctx.headers.items()} if ctx.headers else {}
        extracted_ctx = self._propagator.extract(carrier=carrier)

        span_name = f"{ctx.kind} {ctx.subject}" if ctx.subject else ctx.kind
        span = self.tracer.start_span(
            name=span_name,
            context=extracted_ctx,
            kind=trace.SpanKind.SERVER,
        )

        span.set_attribute("cliffracer.kind", ctx.kind)
        if ctx.subject:
            span.set_attribute("cliffracer.subject", ctx.subject)
        cid = ctx.correlation_id or (ctx.headers.get("correlation_id") if ctx.headers else None)
        if cid:
            span.set_attribute("cliffracer.correlation_id", cid)

        active_ctx = trace.set_span_in_context(span, extracted_ctx)
        token = context.attach(active_ctx)

        # Store in ctx.data dictionary to isolate across concurrent coroutines
        ctx.data["_otel_inbound_span"] = span
        ctx.data["_otel_inbound_token"] = token
        ctx.data["_otel_span"] = span
        ctx.data["_otel_token"] = token
        self._active_spans += 1

    async def worker_result(
        self, ctx: WorkerContext, result: object | None, exc: BaseException | None
    ) -> None:
        """Record exception (including RejectMessage) and set span status."""
        span: trace.Span | None = ctx.data.get("_otel_inbound_span") or ctx.data.get("_otel_span")
        if span is not None and span.is_recording():
            if exc is not None:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                self._error_count += 1
            else:
                span.set_status(trace.StatusCode.OK)
            self._span_count += 1

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        """End server span and detach context token."""
        token = ctx.data.pop("_otel_inbound_token", None)
        if token is None:
            token = ctx.data.pop("_otel_token", None)
        if token is not None:
            context.detach(token)

        span: trace.Span | None = ctx.data.pop("_otel_inbound_span", None)
        if span is None:
            span = ctx.data.pop("_otel_span", None)
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
        cid = ctx.correlation_id or (ctx.headers.get("correlation_id") if ctx.headers else None)
        if cid:
            span.set_attribute("cliffracer.correlation_id", cid)

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
            if span is not None and span.is_recording():
                if exc is not None:
                    span.record_exception(exc)
                    span.set_status(trace.StatusCode.ERROR, str(exc))
                    self._error_count += 1
                else:
                    span.set_status(trace.StatusCode.OK)
                self._span_count += 1
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

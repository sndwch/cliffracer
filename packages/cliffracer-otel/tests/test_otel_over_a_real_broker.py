"""OtelExtension over a real NATS round trip: the trace crosses the broker in headers.

The other end-to-end test simulates the wire with `dict(outbound.headers)` and calls the
receiver's worker directly, inside the caller's attached context. Here the caller's `call_rpc`
publishes through a real connection and the receiver's subscription hands its handler the headers
the broker delivered, so what is tested is that `before_call`'s header reaches the request and an
inbound message's headers reach `worker_setup`. Marked `nats_required`: it is skipped unless a
broker is named, and runs on a disposable one.
"""

import uuid

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import SharedDependency
from conftest import broker_url

pytestmark = [pytest.mark.unit, pytest.mark.nats_required, pytest.mark.asyncio]


def _config(name: str) -> ServiceConfig:
    return ServiceConfig(
        name=name, nats_url=broker_url(), health_port=0, health_listener=False, auto_restart=False
    )


@pytest.fixture
async def pair():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    suffix = uuid.uuid4().hex[:8]
    callee_name = f"otel_callee_{suffix}"

    class Callee(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

        @rpc
        async def echo(self, value: int) -> int:
            return value

    class Caller(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    callee, caller = Callee(_config(callee_name)), Caller(_config(f"otel_caller_{suffix}"))
    await callee.start()
    await caller.start()
    try:
        yield caller, callee_name, exporter
    finally:
        await caller.stop()
        await callee.stop()


async def test_a_real_call_continues_the_callers_trace_on_the_receiver(pair):
    caller, callee_name, exporter = pair

    assert await caller.call_rpc(callee_name, "echo", value=7) == 7

    spans = exporter.get_finished_spans()
    client = next(s for s in spans if s.kind == trace.SpanKind.CLIENT)
    server = next(s for s in spans if s.kind == trace.SpanKind.SERVER)
    assert server.context.trace_id == client.context.trace_id != 0
    assert server.parent is not None and server.parent.span_id == client.context.span_id
    assert server.parent.is_remote is True


async def test_a_tracestate_set_on_the_callers_context_reaches_the_receiver(pair):
    caller, callee_name, exporter = pair
    remote = SpanContext(
        trace_id=int("0af7651916cd43dd8448eb211c80319c", 16),
        span_id=int("b7ad6b7169203331", 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState([("vendor", "opaque")]),
    )

    with trace.use_span(NonRecordingSpan(remote)):
        await caller.call_rpc(callee_name, "echo", value=1)

    server = next(s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER)
    assert server.parent is not None
    assert dict(server.parent.trace_state.items()) == {"vendor": "opaque"}
    assert format(server.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"

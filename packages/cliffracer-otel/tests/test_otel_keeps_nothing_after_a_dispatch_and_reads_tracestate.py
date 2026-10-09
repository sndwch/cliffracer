"""OtelExtension leaves nothing in the context after a dispatch, and reads `tracestate` as well.

The inbound hooks stored the span and the context token under two pairs of keys and popped one
pair, so a finished dispatch kept a reference to an ended span and a detached token under the
other; the other pair is gone, and a test reads `ctx.data` after each pair of hooks. Before setup
the extension has no tracer and reports nothing to /health. And the documentation claims that
`worker_setup` extracts `tracestate` with `traceparent`: this puts a `tracestate` on an incoming
request and reads it off the server span's remote parent.
"""

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"


def _service() -> tuple[CliffracerService, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    return Svc(ServiceConfig(name="otel-probe")), exporter


def _context(kind: str, subject: str, headers: dict[str, str] | None = None) -> WorkerContext:
    return WorkerContext(
        kind=kind, subject=subject, headers=headers or {}, correlation_id="c-1", payload={}
    )


async def test_an_inbound_dispatch_leaves_no_span_or_token_in_the_context_data():
    service, _ = _service()
    await service.container._setup_extensions()
    ctx = _context("rpc", "otel_probe.rpc.go")

    async def handler():
        assert any(key.startswith("_otel") for key in ctx.data), "setup stored nothing"

    await service.container._run_worker(ctx, handler)

    assert [key for key in ctx.data if key.startswith("_otel")] == []
    assert service.otel.health_details()["active_spans"] == 0


async def test_an_outbound_call_leaves_no_span_or_token_in_the_context_data():
    service, _ = _service()
    await service.container._setup_extensions()
    ctx = _context("call_rpc", "peer.rpc.go")

    async def send():
        return "sent"

    await service.container._run_send_hooks(ctx, send)

    assert [key for key in ctx.data if key.startswith("_otel")] == []
    assert service.otel.health_details()["active_spans"] == 0


async def test_a_dispatch_that_raises_still_leaves_the_context_clean():
    service, exporter = _service()
    await service.container._setup_extensions()
    ctx = _context("rpc", "otel_probe.rpc.go")

    async def handler():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await service.container._run_worker(ctx, handler)

    assert [key for key in ctx.data if key.startswith("_otel")] == []
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"


def test_the_extension_reports_nothing_to_health_before_setup():
    service, _ = _service()

    assert service.otel.health_details() is None


async def test_it_reports_after_setup():
    service, _ = _service()
    await service.container._setup_extensions()

    assert service.otel.health_details()["spans_total"] == 0


async def test_tracestate_arrives_with_traceparent_on_the_servers_remote_parent():
    service, exporter = _service()
    await service.container._setup_extensions()
    ctx = _context(
        "rpc",
        "otel_probe.rpc.go",
        headers={"traceparent": TRACEPARENT, "tracestate": "vendor=opaque,other=value"},
    )

    async def handler():
        return None

    await service.container._run_worker(ctx, handler)

    (span,) = exporter.get_finished_spans()
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert span.parent is not None and format(span.parent.span_id, "016x") == PARENT_ID
    assert dict(span.parent.trace_state.items()) == {"vendor": "opaque", "other": "value"}


async def test_CONTROL_without_a_tracestate_header_the_remote_parent_has_none():
    service, exporter = _service()
    await service.container._setup_extensions()
    ctx = _context("rpc", "otel_probe.rpc.go", headers={"traceparent": TRACEPARENT})

    async def handler():
        return None

    await service.container._run_worker(ctx, handler)

    (span,) = exporter.get_finished_spans()
    assert span.parent is not None and dict(span.parent.trace_state.items()) == {}

"""The span an outbound send starts is a PRODUCER for a publish and a CLIENT for a call.

A tracing backend draws producer/consumer edges from the span kind, so a broadcast labelled a
client span shows up as a broken link in the trace graph and as no local error at all. The
classification is a tuple in `before_call`; only `publish_event` and `call_rpc` were read.
"""

import ast
from pathlib import Path

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]

# Written out: a send kind added to the library has to be classified here on purpose. `event` is
# not a kind the library sends, but the classifier names it, so it is held to its answer too.
EXPECTED_KIND = {
    "publish_event": SpanKind.PRODUCER,
    "broadcast": SpanKind.PRODUCER,
    "event": SpanKind.PRODUCER,
    "call_rpc": SpanKind.CLIENT,
    "call_async": SpanKind.CLIENT,
    "call_rpc_no_wait": SpanKind.CLIENT,
    "stream_rpc": SpanKind.CLIENT,
}


async def _span_for(kind: str):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="kinds-svc", health_port=0))
    await svc.container._setup_extensions()
    ctx = WorkerContext(
        kind=kind,
        subject="orders.created",
        headers={},
        correlation_id="corr_kinds",
        payload={},
    )

    async def send():
        assert "traceparent" in ctx.headers
        return None

    await svc.container._run_send_hooks(ctx, send)

    (span,) = exporter.get_finished_spans()
    return span


@pytest.mark.parametrize(("kind", "expected"), sorted(EXPECTED_KIND.items()))
async def test_a_send_starts_the_span_kind_its_kind_calls_for(kind, expected):
    span = await _span_for(kind)

    assert span.kind == expected, (kind, span.kind)
    assert span.name == f"{kind} orders.created"
    assert span.attributes["cliffracer.kind"] == kind


def _send_kinds_the_library_creates() -> set[str]:
    """Every literal kind handed to `_send_context` under `src/cliffracer`, read from the source."""
    kinds: set[str] = set()
    for path in (REPO / "src" / "cliffracer").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_send_context"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                kinds.add(node.args[0].value)
    return kinds


def test_every_send_kind_the_library_creates_is_classified_here():
    found = _send_kinds_the_library_creates()

    assert found >= {"call_rpc", "call_async", "call_rpc_no_wait", "publish_event", "broadcast"}
    assert found - set(EXPECTED_KIND) == set(), (
        f"a send kind with no expected span kind: {sorted(found - set(EXPECTED_KIND))}"
    )

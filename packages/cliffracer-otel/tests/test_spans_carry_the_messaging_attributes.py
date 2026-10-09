"""A span for NATS traffic carries the OpenTelemetry messaging attributes.

Tracing backends build their messaging views, and tail-sampling rules key on `messaging.system`,
`messaging.destination.name` and `messaging.operation.type`. A span that carried only `cliffracer.*`
attributes was invisible to them. A timer is not delivered by the broker, so its span says none of it.
"""

import ast
from pathlib import Path

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]

# Written out: an entrypoint kind added to the library has to be classified here on purpose.
BROKER_KINDS = {"rpc", "async_rpc", "event"}
LOCAL_KINDS = {"timer"}
SPANLESS_KINDS = {"describe"}  # introspection, not traffic: no span at all
SEND_KINDS = {"call_rpc", "call_async", "call_rpc_no_wait", "publish_event", "broadcast"}


async def _service():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="semconv-svc", health_port=0))
    await svc.container._setup_extensions()
    return svc, exporter


def _ctx(kind: str, subject: str = "orders.created") -> WorkerContext:
    return WorkerContext(
        kind=kind, subject=subject, headers={}, correlation_id="corr_sc", payload={}
    )


async def _inbound_span(kind: str, subject: str = "orders.created"):
    svc, exporter = await _service()

    async def handler():
        return None

    await svc.container._run_worker(_ctx(kind, subject), handler)
    (span,) = exporter.get_finished_spans()
    return span


async def _outbound_span(kind: str, subject: str = "orders.created"):
    svc, exporter = await _service()

    async def send():
        return None

    await svc.container._run_send_hooks(_ctx(kind, subject), send)
    (span,) = exporter.get_finished_spans()
    return span


@pytest.mark.parametrize("kind", sorted(BROKER_KINDS))
async def test_a_handled_message_names_nats_its_subject_and_that_it_was_processed(kind):
    span = await _inbound_span(kind, "orders.created")

    assert span.attributes["messaging.system"] == "nats"
    assert span.attributes["messaging.destination.name"] == "orders.created"
    assert span.attributes["messaging.operation.type"] == "process"
    # The cliffracer attributes stay: this adds to them.
    assert span.attributes["cliffracer.kind"] == kind
    assert span.attributes["cliffracer.subject"] == "orders.created"


@pytest.mark.parametrize("kind", sorted(SEND_KINDS))
async def test_a_send_names_nats_its_subject_and_that_it_was_sent(kind):
    span = await _outbound_span(kind, "billing.rpc.charge")

    assert span.attributes["messaging.system"] == "nats"
    assert span.attributes["messaging.destination.name"] == "billing.rpc.charge"
    assert span.attributes["messaging.operation.type"] == "send"
    assert span.attributes["cliffracer.subject"] == "billing.rpc.charge"


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
async def test_a_span_for_work_the_broker_did_not_deliver_carries_no_messaging_attributes(kind):
    span = await _inbound_span(kind, "")

    assert not [key for key in span.attributes if key.startswith("messaging.")]
    assert span.attributes["cliffracer.kind"] == kind


async def test_a_span_with_no_subject_names_no_destination():
    span = await _inbound_span("rpc", "")

    assert span.attributes["messaging.system"] == "nats"
    assert "messaging.destination.name" not in span.attributes


def _kinds_the_library_creates(literal_arg_of: str) -> set[str]:
    """Every literal `kind=` given to `WorkerContext(...)` under `src/cliffracer`."""
    kinds: set[str] = set()
    for path in (REPO / "src" / "cliffracer").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == literal_arg_of
            ):
                for kw in node.keywords:
                    if (
                        kw.arg == "kind"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        kinds.add(kw.value.value)
    return kinds


def test_every_entrypoint_kind_the_library_dispatches_is_classified_here():
    found = _kinds_the_library_creates("WorkerContext")

    assert found >= {"rpc", "event", "timer"}
    classified = BROKER_KINDS | LOCAL_KINDS | SPANLESS_KINDS
    assert found - classified == set(), (
        f"an entrypoint kind with no messaging classification: {sorted(found - classified)}"
    )

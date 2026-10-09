"""`spans_total`, `errors_total` and `active_spans` count the same dispatches, sampled or not.

`active_spans` counted every span the extension started while `spans_total` and `errors_total` counted
only the spans a sampler recorded, so under any sampler but always-on the two numbers on `/health`
disagreed and the errors of a sampled-out message were left out.
"""

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit


async def _drive(sampler):
    provider = TracerProvider(sampler=sampler)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="sampled", health_port=0))
    await svc.container._setup_extensions()

    def inbound() -> WorkerContext:
        return WorkerContext(
            kind="rpc", subject="sampled.rpc.x", headers={}, correlation_id="c", payload={}
        )

    def outbound() -> WorkerContext:
        return WorkerContext(
            kind="call_rpc", subject="other.rpc.y", headers={}, correlation_id="c", payload={}
        )

    async def fine():
        return "ok"

    async def boom():
        raise RuntimeError("failed")

    await svc.container._run_worker(inbound(), fine)
    try:
        await svc.container._run_worker(inbound(), boom)
    except RuntimeError:
        pass
    await svc.container._run_send_hooks(outbound(), fine)
    try:
        await svc.container._run_send_hooks(outbound(), boom)
    except RuntimeError:
        pass
    return svc.otel.health_details(), exporter.get_finished_spans()


async def test_a_sampler_that_records_nothing_leaves_the_counters_as_they_were():
    health, spans = await _drive(ALWAYS_OFF)

    assert spans == (), "the premise: nothing was recorded"
    assert (health["spans_total"], health["errors_total"], health["active_spans"]) == (4, 2, 0)


async def test_the_counters_are_the_same_with_the_sampler_on_and_off():
    sampled, spans = await _drive(ALWAYS_ON)
    unsampled, _ = await _drive(ALWAYS_OFF)

    assert len(spans) == 4, "the premise: everything was recorded"
    counters = ("spans_total", "errors_total", "active_spans")
    assert {k: sampled[k] for k in counters} == {k: unsampled[k] for k in counters}
    assert (sampled["spans_total"], sampled["errors_total"], sampled["active_spans"]) == (4, 2, 0)


async def test_a_dispatch_the_extension_never_started_a_span_for_is_not_counted():
    provider = TracerProvider(sampler=ALWAYS_OFF)

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="unstarted", health_port=0))
    await svc.container._setup_extensions()

    health = svc.otel.health_details()

    assert (health["spans_total"], health["errors_total"], health["active_spans"]) == (0, 0, 0)

"""The default `OtelExtension()`, the `tracer=` argument, and the `active_spans` gauge.

Every other test here builds the extension with a provider it supplies and reads
`active_spans` only when nothing is in flight, so two things could break without
a red test:

- THE DEFAULT. `otel = OtelExtension()` is the form every document shows. Its
  setup builds a `TracerProvider` carrying the service's name and installs it as
  the process-global provider, the one place this package changes global
  OpenTelemetry state. It runs in a subprocess here because it can only be
  observed on a process that has not installed a provider already, and doing it
  in this one would make every later test depend on collection order.
- THE GAUGE. `active_spans` is advertised on `/health` as work in flight. It is
  zero when nothing runs and zero for a gauge that never counts, and the
  decrement clamps at zero, so deleting the increments was invisible. These read
  it while a dispatch is in progress, inbound and outbound, and with two at once.
"""

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _ctx(kind: str, subject: str) -> WorkerContext:
    return WorkerContext(kind=kind, subject=subject, headers={}, correlation_id=None, payload={})


async def _service(**extension_args) -> CliffracerService:
    class Svc(CliffracerService):
        otel = OtelExtension(**extension_args)

    svc = Svc(ServiceConfig(name="gauge-svc", health_port=0))
    await svc.container._setup_extensions()
    return svc


def _active(svc: CliffracerService) -> int:
    details = svc.otel.health_details()
    assert details is not None
    return details["active_spans"]


async def test_active_spans_counts_an_inbound_dispatch_while_it_runs():
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))
    seen: list[int] = []

    async def handler():
        seen.append(_active(svc))

    assert _active(svc) == 0
    await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler)

    assert seen == [1], "the gauge did not count the dispatch that was running"
    assert _active(svc) == 0


async def test_active_spans_counts_an_outbound_call_while_it_runs():
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))
    seen: list[int] = []

    async def send():
        seen.append(_active(svc))
        return "reply"

    await svc.container._run_send_hooks(_ctx("call_rpc", "other.rpc.work"), send)

    assert seen == [1]
    assert _active(svc) == 0


async def test_active_spans_counts_dispatches_in_flight_at_the_same_time():
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))
    both_started, both_read = asyncio.Event(), asyncio.Event()
    started = read = 0
    seen: list[int] = []

    async def handler():
        nonlocal started, read
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        seen.append(_active(svc))
        read += 1
        if read == 2:
            both_read.set()
        # Neither finishes until both have read, or the first to finish would
        # lower the gauge before the second looked.
        await both_read.wait()

    await asyncio.gather(
        svc.container._run_worker(_ctx("rpc", "svc.rpc.a"), handler),
        svc.container._run_worker(_ctx("rpc", "svc.rpc.b"), handler),
    )

    assert seen == [2, 2]
    assert _active(svc) == 0


async def test_the_tracer_argument_is_the_tracer_the_spans_come_from():
    provider, exporter = _provider()
    svc = await _service(tracer=SharedDependency(provider.get_tracer("the-supplied-tracer")))

    async def noop():
        return "ok"

    await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), noop)

    (span,) = exporter.get_finished_spans()
    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "the-supplied-tracer"


async def test_the_tracer_is_usable_before_setup_under_the_default_name():
    provider, _ = _provider()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="not-set-up", health_port=0))

    span = svc.otel.tracer.start_span("before setup")
    span.end()

    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "cliffracer"


_DEFAULT_PROBE = textwrap.dedent(
    """
    import asyncio, json
    from opentelemetry import trace
    from cliffracer import CliffracerService, ServiceConfig
    from cliffracer.core.extension import WorkerContext
    from cliffracer_otel import OtelExtension

    class Svc(CliffracerService):
        otel = OtelExtension()

    async def main():
        before = type(trace.get_tracer_provider()).__name__
        svc = Svc(ServiceConfig(name="orders-svc", health_port=0))
        await svc.container._setup_extensions()
        provider = trace.get_tracer_provider()

        async def noop():
            return "ok"

        ctx = WorkerContext(kind="rpc", subject="orders-svc.rpc.x", headers={},
                            correlation_id=None, payload={})
        await svc.container._run_worker(ctx, noop)
        print(json.dumps({
            "before": before,
            "after": type(provider).__name__,
            "service_name": dict(provider.resource.attributes).get("service.name"),
            "spans_total": svc.otel.health_details()["spans_total"],
        }))

    asyncio.run(main())
    """
)


def test_the_default_extension_installs_a_provider_named_for_the_service():
    done = subprocess.run(
        [sys.executable, "-c", _DEFAULT_PROBE], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-1500:]
    reported = json.loads(done.stdout.strip().splitlines()[-1])

    assert reported["before"] == "ProxyTracerProvider", (
        "the probe did not start from a clean process"
    )
    assert reported["after"] == "TracerProvider", reported
    assert reported["service_name"] == "orders-svc", reported
    assert reported["spans_total"] == 1, reported

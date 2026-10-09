"""A failing tracer costs a span, never a dispatch.

`OtelExtension` does not fail closed: a hook that raises is logged by the pipeline and the handler
runs anyway, with no span. Tracing must not take down dispatch, so the loss shows up as a missing
span, and these tests pin both halves for each hook of an inbound dispatch and for the outbound
hooks: the handler's result, or its own exception, reaches the caller unchanged, and the gauges and
the exporter say what was lost.
"""

from __future__ import annotations

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit


class FlakyTracer:
    """A real tracer whose failures are switched on per test, at the call that decides each hook."""

    def __init__(self, real: trace.Tracer) -> None:
        self._real = real
        self.start_span_fails = False
        self.set_status_fails = False
        self.end_fails = False

    def start_span(self, *args, **kwargs):
        if self.start_span_fails:
            raise RuntimeError("the tracer is down")
        span = self._real.start_span(*args, **kwargs)
        if self.set_status_fails:

            def refuse(*_a, **_k):
                raise RuntimeError("the span would not take a status")

            span.set_status = refuse  # type: ignore[method-assign]
        if self.end_fails:
            end = span.end

            def end_then_raise(*a, **k):
                end(*a, **k)
                raise RuntimeError("the span would not end cleanly")

            span.end = end_then_raise  # type: ignore[method-assign]
        return span


class FlakyProvider:
    """What the extension asks for a tracer: the one test-controlled tracer, whatever the name."""

    def __init__(self, tracer: FlakyTracer) -> None:
        self._tracer = tracer

    def get_tracer(self, *_args, **_kwargs):
        return self._tracer


async def _service() -> tuple[CliffracerService, FlakyTracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FlakyTracer(provider.get_tracer("flaky"))

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(FlakyProvider(tracer)))  # type: ignore[arg-type]

    svc = Svc(ServiceConfig(name="flaky-svc", health_port=0))
    await svc.container._setup_extensions()
    return svc, tracer, exporter


def _ctx(kind: str, subject: str) -> WorkerContext:
    return WorkerContext(kind=kind, subject=subject, headers={}, correlation_id=None, payload={})


def _health(svc: CliffracerService) -> dict:
    details = svc.otel.health_details()
    assert details is not None
    return details


def test_the_extension_does_not_fail_closed():
    """The default that lets a hook failure through to the handler, stated where it is relied on."""
    assert OtelExtension().fails_closed is False


async def test_a_tracer_that_cannot_start_a_span_leaves_the_handler_running():
    svc, tracer, exporter = await _service()
    tracer.start_span_fails = True
    seen: list[bool] = []

    async def handler():
        seen.append(trace.get_current_span().is_recording())
        return "handled"

    result = await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler)

    assert result == "handled"
    assert seen == [False], "the handler ran under a span that was never started"
    # The loss is visible as a missing span, and the gauges do not count what never began.
    assert exporter.get_finished_spans() == ()
    health = _health(svc)
    assert (health["spans_total"], health["errors_total"], health["active_spans"]) == (0, 0, 0)


async def test_a_handler_that_fails_still_fails_as_itself_when_the_tracer_is_down():
    svc, tracer, exporter = await _service()
    tracer.start_span_fails = True

    async def handler():
        raise ValueError("the handler's own failure")

    with pytest.raises(ValueError, match="the handler's own failure"):
        await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler)

    assert exporter.get_finished_spans() == ()
    assert _health(svc)["errors_total"] == 0


async def test_the_next_dispatch_is_traced_once_the_tracer_is_back():
    """A failure is per dispatch: nothing about it sticks to the extension."""
    svc, tracer, exporter = await _service()

    async def handler():
        return "handled"

    tracer.start_span_fails = True
    assert await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler) == "handled"
    tracer.start_span_fails = False
    assert await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler) == "handled"

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["rpc"]
    assert _health(svc)["active_spans"] == 0


async def test_a_span_that_will_not_take_its_result_is_still_ended_and_the_result_returned():
    svc, tracer, exporter = await _service()
    tracer.set_status_fails = True

    async def handler():
        return "handled"

    result = await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler)

    assert result == "handled"
    # worker_result failed, worker_teardown still ran: the span was ended, and so exported.
    assert [s.name for s in exporter.get_finished_spans()] == ["rpc"]
    assert _health(svc)["active_spans"] == 0


async def test_a_span_that_will_not_end_cleanly_still_returns_the_result():
    svc, tracer, exporter = await _service()
    tracer.end_fails = True

    async def handler():
        return "handled"

    result = await svc.container._run_worker(_ctx("rpc", "svc.rpc.work"), handler)

    assert result == "handled"
    assert [s.name for s in exporter.get_finished_spans()] == ["rpc"]


async def test_a_tracer_that_cannot_start_an_outbound_span_still_sends():
    svc, tracer, exporter = await _service()
    tracer.start_span_fails = True
    ctx = _ctx("call_rpc", "other.rpc.work")
    sent: list[dict] = []

    async def send():
        sent.append(dict(ctx.headers))
        return "reply"

    result = await svc.container._run_send_hooks(ctx, send)

    assert result == "reply"
    # What the receiver is missing is the trace context, not the message.
    assert "traceparent" not in sent[0]
    assert exporter.get_finished_spans() == ()
    assert _health(svc)["active_spans"] == 0

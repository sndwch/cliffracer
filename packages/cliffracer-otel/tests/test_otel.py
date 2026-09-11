"""Comprehensive unit tests for cliffracer-otel."""

from __future__ import annotations

import asyncio

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.extension import Extension, RejectMessage, SharedDependency, WorkerContext


def _create_test_tracer() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Create an isolated TracerProvider with an InMemorySpanExporter for testing."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _make_worker_ctx(
    kind: str,
    subject: str,
    headers: dict[str, str] | None = None,
    correlation_id: str | None = None,
) -> WorkerContext:
    return WorkerContext(
        kind=kind,
        subject=subject,
        headers=dict(headers) if headers is not None else {},
        correlation_id=correlation_id,
        payload={},
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle and Configuration Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_tracer_configuration_and_lifecycle():
    """Verify default tracer provider configuration and health details lifecycle."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="test-service"))
    await svc.container._setup_extensions()

    assert svc.otel.tracer is not None
    info = svc.otel.info_details()
    assert info is not None
    assert info["tracer_name"] == "test-service"
    assert info["propagator"] == "tracecontext"

    health = svc.otel.health_details()
    assert health is not None
    assert health["spans_total"] == 0
    assert health["errors_total"] == 0
    assert health["active_spans"] == 0
    assert health["tracer_name"] == "test-service"


@pytest.mark.unit
async def test_two_services_do_not_share_state():
    """Verify bound copy isolation between two service instances."""
    provider, exporter = _create_test_tracer()

    class SvcA(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    class SvcB(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svcA = SvcA(ServiceConfig(name="svc-a"))
    svcB = SvcB(ServiceConfig(name="svc-b"))
    await svcA.container._setup_extensions()
    await svcB.container._setup_extensions()

    async def noop():
        return "ok"

    await svcA.container._run_worker(_make_worker_ctx("rpc", "a.rpc.ping"), noop)

    health_a = svcA.otel.health_details()
    health_b = svcB.otel.health_details()
    assert health_a["spans_total"] == 1
    assert health_b["spans_total"] == 0


# ---------------------------------------------------------------------------
# 2. Inbound Span Extraction Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_inbound_span_extraction_with_valid_traceparent():
    """Verify inbound span extracts trace ID and parent span ID from W3C traceparent."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="inbound-svc"))
    await svc.container._setup_extensions()

    trace_id_hex = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id_hex = "00f067aa0ba902b7"
    traceparent = f"00-{trace_id_hex}-{parent_span_id_hex}-01"

    ctx = _make_worker_ctx(
        kind="rpc",
        subject="inbound.rpc.echo",
        headers={"traceparent": traceparent},
        correlation_id="corr_test_123",
    )

    async def handler():
        # Verify ambient active span inside handler
        current = trace.get_current_span()
        assert current.is_recording()
        return "handled"

    result = await svc.container._run_worker(ctx, handler)
    assert result == "handled"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.kind == SpanKind.SERVER
    assert hex(span.context.trace_id)[2:].rjust(32, "0") == trace_id_hex
    assert span.parent is not None
    assert hex(span.parent.span_id)[2:].rjust(16, "0") == parent_span_id_hex
    assert span.attributes["cliffracer.kind"] == "rpc"
    assert span.attributes["cliffracer.subject"] == "inbound.rpc.echo"
    assert span.attributes["cliffracer.correlation_id"] == "corr_test_123"
    assert span.status.status_code == StatusCode.OK


@pytest.mark.unit
async def test_inbound_span_extraction_with_missing_header():
    """Verify inbound span without traceparent creates a fresh root span."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="root-svc"))
    await svc.container._setup_extensions()

    ctx = _make_worker_ctx(
        kind="event",
        subject="orders.created",
        headers={},
        correlation_id="corr_root_456",
    )

    async def handler():
        return True

    await svc.container._run_worker(ctx, handler)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.kind == SpanKind.SERVER
    assert span.context.trace_id != 0
    assert span.parent is None
    assert span.attributes["cliffracer.kind"] == "event"
    assert span.attributes["cliffracer.subject"] == "orders.created"
    assert span.attributes["cliffracer.correlation_id"] == "corr_root_456"
    assert span.status.status_code == StatusCode.OK


@pytest.mark.unit
async def test_inbound_span_extraction_with_case_insensitive_header():
    """Verify inbound span extracts traceparent regardless of header casing."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="casing-svc"))
    await svc.container._setup_extensions()

    trace_id_hex = "1234567890abcdef1234567890abcdef"
    parent_span_id_hex = "abcdef1234567890"
    traceparent = f"00-{trace_id_hex}-{parent_span_id_hex}-01"

    # Mixed-case header
    ctx = _make_worker_ctx(
        kind="rpc",
        subject="casing.check",
        headers={"Traceparent": traceparent},
    )

    async def handler():
        return 42

    await svc.container._run_worker(ctx, handler)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert hex(span.context.trace_id)[2:].rjust(32, "0") == trace_id_hex
    assert span.parent is not None
    assert hex(span.parent.span_id)[2:].rjust(16, "0") == parent_span_id_hex


@pytest.mark.unit
async def test_inbound_span_extraction_with_malformed_header():
    """Verify inbound span with malformed traceparent falls back to root trace gracefully."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="malformed-svc"))
    await svc.container._setup_extensions()

    ctx = _make_worker_ctx(
        kind="rpc",
        subject="malformed.check",
        headers={"traceparent": "invalid-garbage-value"},
    )

    async def handler():
        return "fallback"

    await svc.container._run_worker(ctx, handler)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.context.trace_id != 0
    assert span.parent is None
    assert span.status.status_code == StatusCode.OK


# ---------------------------------------------------------------------------
# 3. Exception Recording Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_span_exception_recording_on_handler_failure():
    """Verify unhandled handler exception is recorded and marks span as ERROR."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="error-svc"))
    await svc.container._setup_extensions()

    ctx = _make_worker_ctx(kind="rpc", subject="error.rpc.fail")

    async def failing_handler():
        raise RuntimeError("database connection crashed")

    with pytest.raises(RuntimeError, match="database connection crashed"):
        await svc.container._run_worker(ctx, failing_handler)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.status.status_code == StatusCode.ERROR
    assert "database connection crashed" in span.status.description
    assert any(event.name == "exception" for event in span.events)

    health = svc.otel.health_details()
    assert health["errors_total"] == 1
    assert health["spans_total"] == 1
    assert health["active_spans"] == 0


class _PolicyRefuser(Extension):
    async def worker_setup(self, ctx: WorkerContext) -> None:
        raise RejectMessage("unauthorized request")


@pytest.mark.unit
async def test_span_exception_recording_on_reject_message():
    """Verify RejectMessage from worker_setup records exception and marks span as ERROR."""
    provider, exporter = _create_test_tracer()

    class RefusingSvc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))
        refuser = _PolicyRefuser()

    svc = RefusingSvc(ServiceConfig(name="reject-svc"))
    await svc.container._setup_extensions()

    ctx = _make_worker_ctx(kind="rpc", subject="reject.rpc.endpoint")

    async def handler():
        return "should not be reached"

    with pytest.raises(RejectMessage, match="unauthorized request"):
        await svc.container._run_worker(ctx, handler)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.status.status_code == StatusCode.ERROR
    assert "unauthorized request" in span.status.description
    assert any(event.name == "exception" for event in span.events)

    health = svc.otel.health_details()
    assert health["errors_total"] == 1
    assert health["spans_total"] == 1
    assert health["active_spans"] == 0


# ---------------------------------------------------------------------------
# 4. Outbound Span Creation & Propagation Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_outbound_span_creation_and_traceparent_injection_rpc():
    """Verify outbound RPC starts CLIENT span and injects W3C traceparent into headers."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="client-svc"))
    await svc.container._setup_extensions()

    headers: dict[str, str] = {}
    ctx = WorkerContext(
        kind="call_rpc",
        subject="remote.rpc.calculate",
        headers=headers,
        correlation_id="corr_out_1",
        payload={"x": 10},
    )

    async def fake_send():
        # Verify headers were mutated during before_call
        assert "traceparent" in ctx.headers
        return {"result": 20}

    result = await svc.container._run_send_hooks(ctx, fake_send)
    assert result == {"result": 20}

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.kind == SpanKind.CLIENT
    assert span.attributes["cliffracer.kind"] == "call_rpc"
    assert span.attributes["cliffracer.subject"] == "remote.rpc.calculate"
    assert span.attributes["cliffracer.correlation_id"] == "corr_out_1"
    assert span.status.status_code == StatusCode.OK

    # Validate injected header matches finished span
    injected = ctx.headers["traceparent"]
    parts = injected.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert parts[1] == hex(span.context.trace_id)[2:].rjust(32, "0")
    assert parts[2] == hex(span.context.span_id)[2:].rjust(16, "0")


@pytest.mark.unit
async def test_outbound_span_creation_for_event_producer():
    """Verify outbound event publish starts PRODUCER span."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="producer-svc"))
    await svc.container._setup_extensions()

    ctx = WorkerContext(
        kind="publish_event",
        subject="telemetry.metric",
        headers={},
        correlation_id="corr_producer_1",
        payload={"v": 99},
    )

    async def fake_publish():
        assert "traceparent" in ctx.headers
        return None

    await svc.container._run_send_hooks(ctx, fake_publish)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.kind == SpanKind.PRODUCER
    assert span.attributes["cliffracer.kind"] == "publish_event"
    assert span.attributes["cliffracer.subject"] == "telemetry.metric"


@pytest.mark.unit
async def test_outbound_span_exception_recording():
    """Verify outbound call error is recorded on span and status set to ERROR."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="outbound-err-svc"))
    await svc.container._setup_extensions()

    ctx = WorkerContext(
        kind="call_rpc",
        subject="remote.timeout",
        headers={},
        correlation_id="corr_err_1",
        payload={},
    )

    async def fake_send():
        raise TimeoutError("NATS request timed out")

    with pytest.raises(TimeoutError, match="timed out"):
        await svc.container._run_send_hooks(ctx, fake_send)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.status.status_code == StatusCode.ERROR
    assert "timed out" in span.status.description
    assert any(e.name == "exception" for e in span.events)

    health = svc.otel.health_details()
    assert health["errors_total"] == 1
    assert health["spans_total"] == 1
    assert health["active_spans"] == 0


# ---------------------------------------------------------------------------
# 5. End-to-End Distributed Trace Propagation Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_end_to_end_trace_propagation_across_mock_caller_and_receiver():
    """Verify trace context propagates seamlessly from caller to receiver."""
    provider, exporter = _create_test_tracer()

    class ServiceA(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    class ServiceB(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svcA = ServiceA(ServiceConfig(name="service-a"))
    svcB = ServiceB(ServiceConfig(name="service-b"))
    await svcA.container._setup_extensions()
    await svcB.container._setup_extensions()

    # Step 1: Inbound request arrives at ServiceA (initial root span)
    inbound_ctx_a = _make_worker_ctx(kind="rpc", subject="service_a.start")

    async def service_a_handler():
        # Step 2: ServiceA makes outbound RPC call to ServiceB
        outbound_ctx_a = WorkerContext(
            kind="call_rpc",
            subject="service_b.process",
            headers={},
            correlation_id=CorrelationContext.get() or "corr_e2e",
            payload={"task": 1},
        )

        async def send_to_service_b():
            # Step 3: Wire transmission - ServiceB receives headers from ServiceA
            wire_headers = dict(outbound_ctx_a.headers)
            inbound_ctx_b = _make_worker_ctx(
                kind="rpc",
                subject="service_b.process",
                headers=wire_headers,
                correlation_id=outbound_ctx_a.correlation_id,
            )

            async def service_b_handler():
                return "processed_by_b"

            return await svcB.container._run_worker(inbound_ctx_b, service_b_handler)

        return await svcA.container._run_send_hooks(outbound_ctx_a, send_to_service_b)

    result = await svcA.container._run_worker(inbound_ctx_a, service_a_handler)
    assert result == "processed_by_b"

    spans = exporter.get_finished_spans()
    # Expect 3 spans: ServiceB server span, ServiceA client span, ServiceA root server span
    assert len(spans) == 3

    span_b_server = next(s for s in spans if s.name == "rpc service_b.process")
    span_a_client = next(s for s in spans if s.name == "call_rpc service_b.process")
    span_a_server = next(s for s in spans if s.name == "rpc service_a.start")

    # All 3 spans must share the exact same trace ID
    root_trace_id = span_a_server.context.trace_id
    assert root_trace_id != 0
    assert span_a_client.context.trace_id == root_trace_id
    assert span_b_server.context.trace_id == root_trace_id

    # Span lineage:
    # span_a_server (root, parent=None)
    #   -> span_a_client (parent = span_a_server)
    #        -> span_b_server (parent = span_a_client)
    assert span_a_server.parent is None
    assert span_a_client.parent is not None
    assert span_a_client.parent.span_id == span_a_server.context.span_id
    assert span_b_server.parent is not None
    assert span_b_server.parent.span_id == span_a_client.context.span_id


# ---------------------------------------------------------------------------
# 6. Concurrency and Context Isolation Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_concurrent_dispatch_context_isolation():
    """Verify concurrent async dispatches maintain separate span contexts without leakage."""
    provider, exporter = _create_test_tracer()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    svc = Svc(ServiceConfig(name="concurrent-svc"))
    await svc.container._setup_extensions()

    seen_spans: dict[int, trace.Span] = {}

    async def worker_job(worker_id: int):
        trace_id_hex = f"{worker_id:032x}"
        parent_span_id_hex = f"{worker_id:016x}"
        traceparent = f"00-{trace_id_hex}-{parent_span_id_hex}-01"

        ctx = _make_worker_ctx(
            kind="rpc",
            subject=f"concurrent.task.{worker_id}",
            headers={"traceparent": traceparent},
            correlation_id=f"corr_concurrent_{worker_id}",
        )

        async def handler():
            current = trace.get_current_span()
            seen_spans[worker_id] = current
            # Small yield to let event loop interleave tasks
            await asyncio.sleep(0.01)
            # Verify context remained unchanged across the yield
            assert trace.get_current_span() is current
            return worker_id

        return await svc.container._run_worker(ctx, handler)

    tasks = [asyncio.create_task(worker_job(i)) for i in range(1, 11)]
    results = await asyncio.gather(*tasks)
    assert results == list(range(1, 11))

    spans = exporter.get_finished_spans()
    assert len(spans) == 10

    # Ensure each worker saw its own unique trace ID
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 10

    # Ensure no token leaked into ambient task context after completion
    assert not trace.get_current_span().is_recording()
    health = svc.otel.health_details()
    assert health["active_spans"] == 0
    assert health["spans_total"] == 10
    assert health["errors_total"] == 0

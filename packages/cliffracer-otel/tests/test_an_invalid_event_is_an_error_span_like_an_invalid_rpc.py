"""An event refused for its schema ends its span in error and counts as an error, as an RPC does.

The RPC path raises a refusal for an invalid payload, so its span records the exception, ends in
error, and counts in `errors_total`. The event path returns without raising (it dead-letters the
payload), so `worker_result` saw no exception and the span ended OK. The event path marks the
dispatch in `ctx.data["outcome"]`, and the extension ends that span in error with the reason.
"""

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, SharedDependency, validated_listener
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Order(BaseModel):
    n: int


def _service() -> tuple[type[CliffracerService], InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Service(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

        @validated_listener("orders.created", Order, fanout=True)
        async def on_order(self, message: Order) -> None:
            return None

    return Service, exporter


def _harness(service: type[CliffracerService]) -> ServiceTestHarness:
    return ServiceTestHarness(
        service, config=ServiceConfig(name="s", health_port=0, default_on_invalid="drop")
    )


async def test_an_invalid_event_ends_its_span_in_error_and_counts_as_an_error():
    service, exporter = _service()
    async with _harness(service) as harness:
        await harness.emit_event("orders.created", n="not-a-number")

        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert "invalid" in (span.status.description or "")
        health = harness.service.otel.health_details()
        assert (health["spans_total"], health["errors_total"]) == (1, 1)


async def test_CONTROL_a_valid_event_ends_its_span_ok_and_counts_no_error():
    service, exporter = _service()
    async with _harness(service) as harness:
        await harness.emit_event("orders.created", n=3)

        (span,) = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.OK
        health = harness.service.otel.health_details()
        assert (health["spans_total"], health["errors_total"]) == (1, 0)


async def test_an_invalid_event_among_valid_ones_is_one_error():
    service, exporter = _service()
    async with _harness(service) as harness:
        await harness.emit_event("orders.created", n=3)
        await harness.emit_event("orders.created", n="x")
        await harness.emit_event("orders.created", n=5)

        statuses = [s.status.status_code for s in exporter.get_finished_spans()]
        assert statuses.count(StatusCode.ERROR) == 1 and statuses.count(StatusCode.OK) == 2
        assert harness.service.otel.health_details()["errors_total"] == 1

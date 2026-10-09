"""Exported validation exception chains obey the RPC diagnostic policy."""

import json
import re

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import SharedDependency
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

# Self-contained on purpose: this file lives with the package and runs on its own, where the root
# `tests` package is not importable. The root suite has the same order-entry service for its own
# diagnostics tests (`tests/fixtures/rpc_validation_diagnostics.py`); this is the part this needs.
CANARY = "credential_canary_orders"


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    units: int
    authorization: str = "approved"

    @field_validator("authorization")
    @classmethod
    def check_authorization(cls, value):
        mode, _, detail = value.partition(":")
        if mode == "reject":
            raise ValueError(detail)
        if mode == "crash":
            raise RuntimeError(detail)
        if mode == "custom":
            raise PydanticCustomError(detail, "Rejected {credential}", {"credential": detail})
        return value


class OrderService(CliffracerService):
    @rpc
    async def place(self, order: PurchaseOrder) -> int:
        self.accepted.append(order.units)
        return order.units


def diagnostic_canaries(value):
    """Recognize complete synthetic credential tokens in serialized diagnostics."""
    return set(re.findall(r"\bcredential_canary_[a-z]+\b", json.dumps(value, default=str)))


@pytest.mark.parametrize("policy", ["full", "redacted"])
@pytest.mark.parametrize("mode", ["reject", "crash", "custom"])
async def test_validation_exception_export_obeys_policy(policy, mode):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class TracedOrders(OrderService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

    service = TracedOrders(ServiceConfig(name="orders", rpc_validation_errors=policy))
    service.accepted = []
    service._discover_handlers()
    await service.container._setup_extensions()
    # A span already active when validation begins must be safe to export.
    extensions = service.container.extensions
    extensions.remove(service.otel)
    extensions.insert(0, service.otel)
    message = MockMessage(
        "orders.rpc.place",
        json.dumps({"order": {"units": 1, "authorization": f"{mode}:{CANARY}"}}).encode(),
    )
    try:
        await service.container._handle_rpc_request(message)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert len(spans[0].events) == 1
        event = spans[0].events[0]
        assert event.name == "exception"
        assert event.attributes["exception.stacktrace"]
        assert diagnostic_canaries(dict(event.attributes)) == (
            {CANARY} if policy == "full" else set()
        )
        assert service.accepted == []
    finally:
        await service.container._stop_extensions()
        provider.shutdown()

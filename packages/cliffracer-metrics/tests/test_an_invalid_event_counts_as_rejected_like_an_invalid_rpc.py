"""An event refused for its schema is counted as rejected, as an RPC refused for its arguments is.

The RPC path raises a refusal for an invalid payload, which the metrics extension counts as
`rejected`. The event path returns without raising: it hands the payload to the dead-letter
publisher, so `worker_result` saw no exception and the dispatch counted as a plain success, with
`rejected` at zero while the dead-letter stream filled. The event path now marks the dispatch in
`ctx.data["outcome"]`, and the extension counts the mark.
"""

import pytest
from cliffracer_metrics import MetricsExtension
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, validated_listener
from cliffracer.testing import MockMessage, ServiceTestHarness

pytestmark = pytest.mark.unit


class Order(BaseModel):
    n: int


class Service(CliffracerService):
    metrics_ext = MetricsExtension()

    @rpc
    async def ok(self, x: int) -> int:
        return x

    @validated_listener("orders.created", Order, fanout=True)
    async def on_order(self, message: Order) -> None:
        return None

    @listener("orders.typed", fanout=True)
    async def on_typed(self, n: int) -> None:
        return None


def _harness() -> ServiceTestHarness:
    return ServiceTestHarness(
        Service, config=ServiceConfig(name="s", health_port=0, default_on_invalid="drop")
    )


def _counted(harness: ServiceTestHarness, kind: str) -> dict:
    details = harness.service.metrics_ext.health_details()
    assert details is not None
    return {k: details[kind][k] for k in ("count", "errors", "rejected")}


async def test_an_invalid_event_on_a_validated_listener_is_rejected():
    async with _harness() as harness:
        await harness.emit_event("orders.created", n="not-a-number")

        assert _counted(harness, "event") == {"count": 1, "errors": 0, "rejected": 1}


async def test_an_invalid_event_on_a_typed_listener_is_rejected():
    async with _harness() as harness:
        await harness.emit_event("orders.typed", n="not-a-number")

        assert _counted(harness, "event") == {"count": 1, "errors": 0, "rejected": 1}


async def test_an_invalid_rpc_is_rejected_the_same_way():
    async with _harness() as harness:
        msg = MockMessage(
            subject="s.rpc.ok", data=b'{"x":"a"}', headers={"Content-Type": "application/json"}
        )
        await harness.container.dispatcher.handle_rpc_request(msg)

        assert _counted(harness, "rpc") == {"count": 1, "errors": 0, "rejected": 1}


async def test_CONTROL_a_valid_event_is_counted_and_not_rejected():
    async with _harness() as harness:
        await harness.emit_event("orders.created", n=3)
        await harness.emit_event("orders.typed", n=4)

        assert _counted(harness, "event") == {"count": 2, "errors": 0, "rejected": 0}


async def test_one_invalid_event_among_valid_ones_is_one_rejection():
    async with _harness() as harness:
        await harness.emit_event("orders.created", n=3)
        await harness.emit_event("orders.created", n="x")
        await harness.emit_event("orders.created", n=5)

        assert _counted(harness, "event") == {"count": 3, "errors": 0, "rejected": 1}

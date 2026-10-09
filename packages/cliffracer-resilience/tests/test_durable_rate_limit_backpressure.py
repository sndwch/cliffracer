"""Durable order work waits for capacity instead of disappearing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class OrderProcessor(CliffracerService):
    resilience = ResilienceExtension()

    def __init__(self) -> None:
        super().__init__(
            ServiceConfig(
                name="orders",
                jetstream_enabled=True,
                jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.created"])],
            )
        )
        self.processed: list[str] = []

    @listener("orders.created", durable="order_processor")
    @rate_limit(calls=1, window=10.0)
    async def process(self, order_id: str) -> None:
        self.processed.append(order_id)


def order_message(order_id: str) -> AsyncMock:
    message = AsyncMock()
    message.subject = "orders.created"
    message.data = f'{{"order_id":"{order_id}"}}'.encode()
    message.headers = None
    message.metadata = SimpleNamespace(num_delivered=1)
    return message


async def test_work_arriving_over_capacity_is_deferred_until_a_permit_is_available():
    service = OrderProcessor()
    await service.container._setup_extensions()
    service._discover_handlers()

    first = order_message("order-1")
    await service.container._handle_jetstream_event(first)
    first.ack.assert_awaited_once()

    waiting = order_message("order-2")
    await service.container._handle_jetstream_event(waiting)

    assert service.processed == ["order-1"]
    waiting.ack.assert_not_awaited()
    waiting.term.assert_not_awaited()
    waiting.nak.assert_awaited_once()
    assert waiting.nak.await_args.kwargs["delay"] > 9.0

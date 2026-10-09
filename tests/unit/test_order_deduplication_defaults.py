"""Order streams recognize every NATS representation of its dedup default."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.api import StreamConfig

from cliffracer.core.jetstream import StreamSpec, ensure_streams, validate_bound_streams

pytestmark = pytest.mark.unit


def order_stream(*, duplicate_window: float | None) -> StreamConfig:
    return StreamConfig(
        name="ORDERS",
        subjects=["orders.submitted"],
        duplicate_window=duplicate_window,
    )


def broker_with(config: StreamConfig) -> AsyncMock:
    broker = AsyncMock()

    class Page:
        total = 1

        def __iter__(self):
            return iter([SimpleNamespace(config=config)])

    broker.streams_info_iterator.return_value = Page()
    return broker


@pytest.mark.parametrize("server_window", [None, 0, 120.0])
@pytest.mark.parametrize("allow_update", [False, True])
async def test_order_startup_accepts_every_shape_of_the_nats_default_window(
    server_window: float | None,
    allow_update: bool,
):
    broker = broker_with(order_stream(duplicate_window=server_window))
    declaration = StreamSpec(name="ORDERS", subjects=["orders.submitted"])

    await ensure_streams(broker, [declaration], allow_update=allow_update)

    broker.update_stream.assert_not_awaited()


@pytest.mark.parametrize("allow_update", [False, True])
async def test_zero_declaration_does_not_drift_after_nats_stores_its_default(
    allow_update: bool,
):
    broker = broker_with(order_stream(duplicate_window=120.0))
    declaration = StreamSpec(
        name="ORDERS",
        subjects=["orders.submitted"],
        duplicate_window_seconds=0,
    )

    await ensure_streams(broker, [declaration], allow_update=allow_update)

    broker.update_stream.assert_not_awaited()


@pytest.mark.parametrize("server_window", [None, 0, 120.0])
async def test_bound_order_stream_accepts_every_shape_of_the_nats_default_window(
    server_window: float | None,
):
    config = order_stream(duplicate_window=server_window)
    broker = broker_with(config)
    broker.stream_info.return_value = SimpleNamespace(config=config)

    await validate_bound_streams(
        broker,
        [StreamSpec(name="ORDERS", subjects=["orders.submitted"])],
    )

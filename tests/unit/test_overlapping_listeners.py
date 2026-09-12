"""Unit tests for overlapping listener patterns.

Verifies that when a service registers multiple patterns that overlap (e.g.
'orders.*' and 'orders.created'), an event matching both patterns causes each
listener to execute exactly once, rather than duplicating execution across
overlapping subscriptions.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener

pytestmark = pytest.mark.unit


class _MockMsg:
    def __init__(self, subject: str, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers = None


@pytest.mark.asyncio
async def test_overlapping_listeners_execute_once_per_callback():
    """Each subscription callback dispatches only to its bound pattern handler."""
    orders_wildcard_count = 0
    orders_created_count = 0

    class OrderService(CliffracerService):
        @listener("orders.*", fanout=True)
        async def on_order_wildcard(self, id: str = "") -> None:
            nonlocal orders_wildcard_count
            orders_wildcard_count += 1

        @listener("orders.created", fanout=True)
        async def on_order_created(self, id: str = "") -> None:
            nonlocal orders_created_count
            orders_created_count += 1

    svc = OrderService(ServiceConfig(name="order_service"))
    svc._discover_handlers()

    # Create bound callbacks as setup_subscriptions does
    cb_wildcard = svc.container._make_event_callback("orders.*")
    cb_created = svc.container._make_event_callback("orders.created")

    msg = _MockMsg("orders.created", {"id": "123"})

    # When NATS receives an event on "orders.created", both subscriptions match,
    # so NATS delivers the message to both registered callbacks:
    await cb_wildcard(msg)
    await cb_created(msg)
    # Allow background dispatch tasks to run
    await asyncio.sleep(0)

    # With pattern filtering, each handler executed exactly once (total 2 executions)
    assert orders_wildcard_count == 1
    assert orders_created_count == 1


@pytest.mark.asyncio
async def test_unbound_dispatch_matches_all():
    """Calling _dispatch_event without a pattern preserves broadcast fallback."""
    orders_wildcard_count = 0
    orders_created_count = 0

    class OrderService(CliffracerService):
        @listener("orders.*", fanout=True)
        async def on_order_wildcard(self, id: str = "") -> None:
            nonlocal orders_wildcard_count
            orders_wildcard_count += 1

        @listener("orders.created", fanout=True)
        async def on_order_created(self, id: str = "") -> None:
            nonlocal orders_created_count
            orders_created_count += 1

    svc = OrderService(ServiceConfig(name="order_service"))
    svc._discover_handlers()

    msg = _MockMsg("orders.created", {"id": "123"})
    await svc.container._dispatch_event(msg, pattern=None)

    assert orders_wildcard_count == 1
    assert orders_created_count == 1


@pytest.mark.asyncio
async def test_setup_subscriptions_binds_distinct_callbacks():
    """setup_subscriptions passes distinct callbacks bound to each pattern to nc.subscribe."""

    class MultiListenerService(CliffracerService):
        @listener("events.a", fanout=True)
        async def on_a(self) -> None:
            pass

        @listener("events.b", fanout=True)
        async def on_b(self) -> None:
            pass

    svc = MultiListenerService(ServiceConfig(name="multi_sub"))
    svc._discover_handlers()

    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False
    mock_nc.is_connecting = False
    mock_nc.is_reconnecting = False
    mock_nc.subscribe = AsyncMock(return_value=AsyncMock())
    svc.container.nc = mock_nc

    await svc.container._setup_subscriptions()

    # Verify nc.subscribe was called for both patterns with their respective callbacks
    calls = mock_nc.subscribe.call_args_list
    patterns_subscribed = [c.args[0] for c in calls]
    assert "events.a" in patterns_subscribed
    assert "events.b" in patterns_subscribed

    # Verify callbacks are distinct callable wrappers
    cb_a = next(c.kwargs["cb"] for c in calls if c.args[0] == "events.a")
    cb_b = next(c.kwargs["cb"] for c in calls if c.args[0] == "events.b")
    assert cb_a is not cb_b
    assert callable(cb_a)
    assert callable(cb_b)

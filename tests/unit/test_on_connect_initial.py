"""Tests verifying config.on_connect executes on initial broker connection."""

from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def _mock_nats():
    """Mock NATS connection without invoking callbacks."""
    return patch("cliffracer.core.container.nats.connect", new=AsyncMock(return_value=AsyncMock()))


async def test_on_connect_fires_on_the_initial_connection():
    fired = []

    async def on_connect():
        fired.append("initial")

    svc = CliffracerService(ServiceConfig(name="a", on_connect=on_connect))
    with _mock_nats():
        await svc.container.connect()

    assert fired == ["initial"], "on_connect did not fire on the initial connection"


async def test_a_sync_on_connect_is_awaited_through_maybe_await():
    """Verify synchronous on_connect callbacks are properly awaited."""
    fired = []

    def on_connect():  # deliberately not async
        fired.append("sync")

    svc = CliffracerService(ServiceConfig(name="a", on_connect=on_connect))
    with _mock_nats():
        await svc.container.connect()

    assert fired == ["sync"]


async def test_it_fires_once_per_connection_not_once_per_callback_path():
    """Verify on_connect fires exactly once per initial connection and reconnection."""
    fired = []

    async def on_connect():
        fired.append(len(fired))

    svc = CliffracerService(ServiceConfig(name="a", on_connect=on_connect))
    with _mock_nats():
        await svc.container.connect()
    assert fired == [0]

    await svc.container.connection._reconnected_callback()
    assert fired == [0, 1]


async def test_CONTROL_no_on_connect_still_connects():
    """Verify connection succeeds when no on_connect callback is specified."""
    svc = CliffracerService(ServiceConfig(name="a"))
    assert svc.config.on_connect is None

    with _mock_nats():
        await svc.container.connect()  # must not raise

    assert svc.container.nc is not None


async def test_CONTROL_the_mock_does_not_fire_the_callback_itself():
    """Verify the mocked NATS connection does not invoke the callback directly."""
    fired = []

    async def on_connect():
        fired.append("should not happen")

    with _mock_nats():
        import nats

        await nats.connect("nats://x:4222", on_connect=on_connect)

    assert fired == [], "mock connection invoked callback unexpectedly"

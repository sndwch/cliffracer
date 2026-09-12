"""Tests verifying that JetStream pull consumers unsubscribe upon cancellation and shutdown."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer.core.container import BrokerConnectionState, Container
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit


class DummyService:
    def __init__(self) -> None:
        self.config = ServiceConfig(name="dummy_svc", health_port=0)
        self._running = True


async def test_pull_loop_unsubscribes_on_cancel():
    """Cancelling a task running _pull_loop invokes sub.unsubscribe()."""
    svc = DummyService()
    container = Container(svc, svc.config)

    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False
    container.nc = mock_nc
    container._broker_state = BrokerConnectionState.CONNECTED

    pull_sub = AsyncMock()
    pull_sub.fetch = AsyncMock(side_effect=asyncio.CancelledError)
    pull_sub.unsubscribe = AsyncMock()

    loop_task = asyncio.create_task(container._pull_loop(pull_sub, "test_durable"))

    # Give loop a cycle to run
    await asyncio.sleep(0.01)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    pull_sub.unsubscribe.assert_awaited_once()


async def test_pull_loop_skips_unsubscribe_when_closed_or_draining():
    """Pull loop does not invoke unsubscribe if broker connection is closed or draining."""
    svc = DummyService()
    container = Container(svc, svc.config)

    # Case 1: broker is CLOSED
    mock_nc = AsyncMock()
    container.nc = mock_nc
    container._broker_state = BrokerConnectionState.CLOSED

    pull_sub1 = AsyncMock()
    pull_sub1.fetch = AsyncMock(side_effect=asyncio.CancelledError)

    task1 = asyncio.create_task(container._pull_loop(pull_sub1, "durable_1"))
    await asyncio.sleep(0.01)
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1

    pull_sub1.unsubscribe.assert_not_called()

    # Case 2: broker is draining
    container._broker_state = BrokerConnectionState.CONNECTED
    mock_nc.is_draining = True

    pull_sub2 = AsyncMock()
    pull_sub2.fetch = AsyncMock(side_effect=asyncio.CancelledError)

    task2 = asyncio.create_task(container._pull_loop(pull_sub2, "durable_2"))
    await asyncio.sleep(0.01)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2

    pull_sub2.unsubscribe.assert_not_called()


async def test_pull_consumer_unsubscribes_during_service_stop():
    """Stopping a service cancels pull loops and unsubscribes all pull consumers."""
    from cliffracer.core.decorators import listener
    from cliffracer.core.service import CliffracerService

    class PullConsumerService(CliffracerService):
        @listener("pull.sub.test", durable="my_pull_durable", pull=True)
        async def on_event(self, data: str) -> None:
            pass

    from cliffracer.core.jetstream import StreamSpec

    cfg = ServiceConfig(
        name="pull_cleanup_svc",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="test_stream", subjects=["dlq.>", "pull.>"])],
    )
    svc = PullConsumerService(cfg)

    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.is_closed = False
    mock_nc.is_draining = False
    svc.container.nc = mock_nc
    svc.container._broker_state = BrokerConnectionState.CONNECTED

    pull_sub = AsyncMock()

    # fetch idles with TimeoutError
    async def delayed_timeout(*args, **kwargs):
        await asyncio.sleep(0.01)
        raise TimeoutError()

    pull_sub.fetch = AsyncMock(side_effect=delayed_timeout)
    pull_sub.consumer_info = AsyncMock()

    mock_js = AsyncMock()
    mock_js.pull_subscribe = AsyncMock(return_value=pull_sub)
    svc.container.js = mock_js

    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
        patch("cliffracer.core.container.ensure_streams", new_callable=AsyncMock),
    ):
        await svc.start()
        assert len(svc.container._subscriptions) == 4  # rpc, describe, async, and pull_loop

        await asyncio.sleep(0)
        await svc.stop()

        pull_sub.unsubscribe.assert_awaited_once()


async def test_pull_loop_sleeps_on_zero_messages():
    """When _pull_once returns 0, _pull_loop sleeps 0.05s to prevent 100% CPU spin."""
    svc = DummyService()
    container = Container(svc, svc.config)

    pull_sub = AsyncMock()
    pull_sub.fetch = AsyncMock(side_effect=TimeoutError)
    pull_sub.unsubscribe = AsyncMock()

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Stop service after one sleep to terminate loop
        mock_sleep.side_effect = lambda duration: setattr(svc, "_running", False)

        await container._pull_loop(pull_sub, "test_durable")

        mock_sleep.assert_awaited_with(0.05)

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


@pytest.mark.parametrize("parked_in", ["fetch", "idle_sleep"])
async def test_an_external_cancel_of_a_running_pull_loop_unsubscribes(parked_in):
    """The scenario the test above only imitates: the loop is RUNNING, parked in a fetch or in the
    sleep after an empty one, when something cancels its task from outside. There, the
    CancelledError arrives from the awaited call and not from the fetch's own side effect."""
    svc = DummyService()
    container = Container(svc, svc.config)

    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_nc.is_draining = False
    container.nc = mock_nc

    in_fetch = asyncio.Event()

    async def block_in_fetch(*args, **kwargs):
        in_fetch.set()
        await asyncio.Event().wait()  # never set: only a cancel ends this

    pull_sub = AsyncMock()
    pull_sub.unsubscribe = AsyncMock()
    if parked_in == "fetch":
        pull_sub.fetch = AsyncMock(side_effect=block_in_fetch)
    else:
        pull_sub.fetch = AsyncMock(side_effect=TimeoutError)  # an empty batch, then a 0.05s sleep

    loop_task = asyncio.create_task(container._pull_loop(pull_sub, "test_durable"))
    if parked_in == "fetch":
        await asyncio.wait_for(in_fetch.wait(), timeout=2)
    else:
        await asyncio.sleep(0.01)  # past the first empty fetch, inside the 0.05s sleep
        assert pull_sub.fetch.await_count == 1
    assert not loop_task.done(), "the loop must still be running when it is cancelled"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(loop_task, timeout=2)

    assert loop_task.cancelled()
    pull_sub.unsubscribe.assert_awaited_once()


@pytest.mark.parametrize(
    ("is_closed", "is_draining", "unsubscribes"),
    [(True, False, False), (False, True, False), (False, False, True)],
    ids=["closed", "draining", "open"],
)
async def test_pull_loop_unsubscribes_only_while_the_connection_is_usable(
    is_closed, is_draining, unsubscribes
):
    """The pull loop unsubscribes unless the connection is closed or draining.

    The two flags the loop reads are set explicitly on the connection double: an
    unset attribute of an `AsyncMock` is a truthy child mock, which would read as
    "draining" and skip the unsubscribe whichever case is meant.
    """
    svc = DummyService()
    container = Container(svc, svc.config)

    mock_nc = AsyncMock()
    mock_nc.is_closed = is_closed
    mock_nc.is_draining = is_draining
    container.nc = mock_nc

    pull_sub = AsyncMock()
    pull_sub.fetch = AsyncMock(side_effect=asyncio.CancelledError)

    task = asyncio.create_task(container._pull_loop(pull_sub, "durable"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pull_sub.unsubscribe.await_count == (1 if unsubscribes else 0)


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

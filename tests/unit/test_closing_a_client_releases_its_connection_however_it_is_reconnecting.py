"""Closing a client releases its connection when the broker is away, and never replaces the block's error.

`close()` drained and only then set its flag. nats-py refuses a drain while it redials
(`ConnectionReconnectingError`), which is when a shutdown path runs, so the flag was never set, the
connection stayed open and usable, and the nats-py error replaced the application's own exception
in `async with`. A `close()` during the first dial found no connection to release; the dial then
completed, assigned the connection, and nothing ever closed it.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from nats.errors import ConnectionDrainingError, ConnectionReconnectingError

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcConnectionError

pytestmark = pytest.mark.unit


def _nc(*, reconnecting: bool = False, close_fails: bool = False) -> AsyncMock:
    nc = AsyncMock()
    nc.is_closed = False
    if reconnecting:
        nc.drain.side_effect = ConnectionReconnectingError()
    if close_fails:
        nc.close.side_effect = RuntimeError("the socket is already gone")
    return nc


def _owned(nc: AsyncMock | None) -> ServiceClient:
    client = ServiceClient(service="svc", verify=False)
    client._nc = nc
    return client


async def test_close_while_reconnecting_closes_the_connection_and_refuses_later_use():
    nc = _nc(reconnecting=True)
    client = _owned(nc)

    await client.close()

    nc.drain.assert_awaited_once()
    nc.close.assert_awaited_once()
    with pytest.raises(RpcConnectionError, match="was closed"):
        await client._connection()


@pytest.mark.parametrize(
    "drain_error",
    [
        pytest.param(ConnectionDrainingError(), id="already-draining"),
        pytest.param(OSError("the socket was reset"), id="os-error"),
        pytest.param(RuntimeError("anything else"), id="anything-else"),
    ],
)
async def test_a_drain_that_fails_for_any_reason_still_closes_the_connection(drain_error):
    """The fallback is not specific to a redial: whatever stops the drain, the connection is closed."""
    nc = _nc()
    nc.drain.side_effect = drain_error
    client = _owned(nc)

    await client.close()

    nc.drain.assert_awaited_once()
    nc.close.assert_awaited_once()
    assert client._closed


async def test_a_block_that_fails_while_the_broker_is_reconnecting_keeps_its_own_exception():
    nc = _nc(reconnecting=True)
    client = _owned(nc)

    with pytest.raises(RuntimeError, match="the application's own failure") as caught:
        async with client:
            raise RuntimeError("the application's own failure")

    assert caught.value.__context__ is None, "a nats-py error replaced or wrapped the block's own"
    nc.close.assert_awaited_once()


async def test_a_close_that_cannot_even_close_is_logged_and_does_not_raise():
    nc = _nc(reconnecting=True, close_fails=True)
    client = _owned(nc)

    await client.close()

    assert client._closed


async def test_a_block_that_ends_well_while_the_broker_is_away_ends_well():
    client = _owned(_nc(reconnecting=True))

    async with client:
        pass

    assert client._closed


async def test_close_during_the_first_dial_closes_the_connection_that_dial_returns():
    client = ServiceClient(service="svc", verify=False)
    dialled = _nc()
    release = asyncio.Event()

    async def slow_dial():
        await release.wait()
        return dialled

    client._dial = slow_dial  # type: ignore[method-assign]
    first_call = asyncio.create_task(client._connection())
    await asyncio.sleep(0)  # the dial is in flight and there is no connection yet

    await client.close()
    release.set()

    with pytest.raises(RpcConnectionError, match="was closed"):
        await first_call
    dialled.drain.assert_awaited_once()
    assert client._nc is None, "the closed client kept the connection it should have released"


async def test_the_connection_dialled_after_a_close_is_closed_when_it_cannot_be_drained():
    client = ServiceClient(service="svc", verify=False)
    dialled = _nc(reconnecting=True)
    release = asyncio.Event()

    async def slow_dial():
        await release.wait()
        return dialled

    client._dial = slow_dial  # type: ignore[method-assign]
    first_call = asyncio.create_task(client._connection())
    await asyncio.sleep(0)
    await client.close()
    release.set()

    with pytest.raises(RpcConnectionError):
        await first_call
    dialled.close.assert_awaited_once()


async def test_CONTROL_a_healthy_owned_connection_is_drained_once_and_the_client_refuses_reuse():
    nc = _nc()
    client = _owned(nc)

    await client.close()

    nc.drain.assert_awaited_once()
    nc.close.assert_not_awaited()
    with pytest.raises(RpcConnectionError, match="was closed"):
        await client._connection()


async def test_CONTROL_a_connection_that_is_already_closed_is_not_drained_again():
    nc = _nc()
    nc.is_closed = True
    client = _owned(nc)

    await client.close()

    nc.drain.assert_not_awaited()
    assert client._closed


async def test_CONTROL_a_borrowed_connection_is_left_alone_and_the_client_keeps_working():
    nc = _nc()
    client = ServiceClient(service="svc", verify=False, nc=nc)

    await client.close()

    nc.drain.assert_not_awaited()
    nc.close.assert_not_awaited()
    assert not client._closed
    assert await client._connection() is nc


async def test_CONTROL_a_dial_that_finishes_before_close_is_released_by_close():
    client = ServiceClient(service="svc", verify=False)
    dialled = _nc()

    async def dial():
        return dialled

    client._dial = dial  # type: ignore[method-assign]
    assert await client._connection() is dialled

    await client.close()

    dialled.drain.assert_awaited_once()


async def test_a_call_that_arrives_while_the_drain_is_under_way_is_already_refused():
    nc = _nc()
    drain_started, finish = asyncio.Event(), asyncio.Event()

    async def drain():
        drain_started.set()
        await finish.wait()

    nc.drain.side_effect = drain
    client = _owned(nc)
    closing = asyncio.create_task(client.close())
    await drain_started.wait()

    with pytest.raises(RpcConnectionError, match="was closed"):
        await client._connection()
    finish.set()
    await closing


async def test_a_close_cancelled_during_the_drain_still_leaves_the_client_closed():
    nc = _nc()
    drain_started = asyncio.Event()

    async def drain():
        drain_started.set()
        await asyncio.Event().wait()

    nc.drain.side_effect = drain
    client = _owned(nc)
    closing = asyncio.create_task(client.close())
    await drain_started.wait()

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert client._closed

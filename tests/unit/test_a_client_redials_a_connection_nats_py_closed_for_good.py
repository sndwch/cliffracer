"""A client dials again when the connection it opened has been closed for good.

nats-py closes a connection for good on an authentication change or a terminal error from the server,
and the object stays assigned and refuses every request. `_connection` dialled only when none had ever
been assigned, so a long-lived client was dead for the rest of its life after, for example, a
credential rotation that ended its session: every later call raised `RpcConnectionError`, with no
dial attempt. A connection the client was handed is its owner's to replace, and a client that was
closed on purpose stays closed.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcConnectionError

pytestmark = pytest.mark.unit


def _nc(*, closed: bool = False) -> AsyncMock:
    nc = AsyncMock()
    nc.is_closed = closed
    return nc


class Dials:
    """A `_dial` that hands out numbered connections and counts how often it is asked."""

    def __init__(self) -> None:
        self.made: list[AsyncMock] = []

    async def __call__(self) -> AsyncMock:
        # A dial takes time. Without a suspension here the callers gathered in a test run one after
        # another, never overlapping, and a check that is missing a lock cannot be seen to race.
        await asyncio.sleep(0)
        nc = _nc()
        self.made.append(nc)
        return nc


def _client(dials: Dials, nc: AsyncMock | None = None) -> ServiceClient:
    client = ServiceClient(service="svc", verify=False)
    client._dial = dials  # type: ignore[method-assign]
    client._nc = nc
    return client


async def test_a_connection_nats_py_closed_for_good_is_replaced_by_the_next_call():
    dials = Dials()
    dead = _nc(closed=True)
    client = _client(dials, dead)

    connection = await client._connection()

    assert connection is not dead and connection is dials.made[0]
    assert len(dials.made) == 1


async def test_the_replacement_is_kept_and_used_by_the_calls_after_it():
    dials = Dials()
    client = _client(dials, _nc(closed=True))

    first = await client._connection()
    second = await client._connection()

    assert first is second and len(dials.made) == 1


async def test_a_replacement_asks_for_the_drift_check_again():
    dials = Dials()
    client = _client(dials, _nc(closed=True))
    client._verified = True

    await client._connection()

    assert client._verified is False


async def test_a_connection_that_dies_a_second_time_is_replaced_a_second_time():
    dials = Dials()
    client = _client(dials, _nc(closed=True))

    await client._connection()
    dials.made[0].is_closed = True
    await client._connection()

    assert len(dials.made) == 2


async def test_callers_that_arrive_together_share_one_dial():
    dials = Dials()
    client = _client(dials, _nc(closed=True))

    results = await asyncio.gather(*(client._connection() for _ in range(5)))

    assert len(dials.made) == 1 and all(r is dials.made[0] for r in results)


async def test_a_closed_client_is_not_redialled():
    dials = Dials()
    client = _client(dials, _nc(closed=True))
    await client.close()

    with pytest.raises(RpcConnectionError, match="was closed"):
        await client._connection()

    assert dials.made == []


class GatedDials(Dials):
    """A dial that waits for the test to let it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def __call__(self) -> AsyncMock:
        await self.gate.wait()
        return await super().__call__()


async def test_a_client_closed_while_a_redial_is_in_flight_drains_the_new_connection():
    dials = GatedDials()
    client = _client(dials, _nc(closed=True))
    calling = asyncio.create_task(client._connection())
    for _ in range(3):
        await asyncio.sleep(0)  # inside the dial, holding the lock

    await client.close()
    dials.gate.set()

    with pytest.raises(RpcConnectionError, match="was closed"):
        await calling
    assert len(dials.made) == 1
    assert client._nc is not dials.made[0]
    dials.made[0].drain.assert_awaited_once()


async def test_CONTROL_a_connection_the_client_was_handed_is_not_replaced():
    dials = Dials()
    handed = _nc(closed=True)
    client = ServiceClient(service="svc", verify=False, nc=handed)
    client._dial = dials  # type: ignore[method-assign]

    assert await client._connection() is handed
    assert dials.made == []


async def test_CONTROL_a_healthy_connection_is_kept_and_not_dialled_again():
    dials = Dials()
    healthy = _nc()
    client = _client(dials, healthy)

    assert await client._connection() is healthy
    assert dials.made == []


async def test_CONTROL_the_first_dial_does_not_reset_the_drift_check():
    dials = Dials()
    client = _client(dials, None)
    client._verified = True

    await client._connection()

    assert client._verified is True

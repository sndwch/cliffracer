"""The connection pool hands out connections in rotation, pins subscriptions, and cleans up.

`OptimizedNATSConnection` was tested only for how many times it called `nats.connect` and with
what. Nothing read what it does with the connections: a pool that always returned the first would
serialise every pooled request and pass, `subscribe()` is pinned to the first connection to keep
message ordering and nothing said so, and a `connect()` that failed part-way left the connections
it had opened for a caller that might never call `close()`.
"""

from unittest.mock import AsyncMock, patch

import pytest
from cliffracer_metrics.connection_pool import OptimizedNATSConnection

pytestmark = pytest.mark.unit


def _connections(count: int) -> list[AsyncMock]:
    made = []
    for n in range(count):
        conn = AsyncMock(name=f"connection-{n}")
        conn.is_connected = True
        made.append(conn)
    return made


async def _pool(count: int) -> tuple[OptimizedNATSConnection, list[AsyncMock]]:
    made = _connections(count)
    pool = OptimizedNATSConnection(max_connections=count)
    with patch("cliffracer.core.dial.connect", new=AsyncMock(side_effect=made)):
        await pool.connect()
    return pool, made


async def test_successive_requests_rotate_through_every_connection_and_wrap():
    pool, made = await _pool(3)

    handed = [await pool.get_connection() for _ in range(7)]

    assert handed == [made[0], made[1], made[2], made[0], made[1], made[2], made[0]]


async def test_publish_and_request_each_use_the_next_connection():
    pool, made = await _pool(2)

    await pool.publish("a", b"1")
    await pool.request("b", b"2", timeout=1.0)
    await pool.publish("c", b"3")

    assert [c.publish.await_count for c in made] == [2, 0]
    assert [c.request.await_count for c in made] == [0, 1]
    made[1].request.assert_awaited_once_with("b", b"2", timeout=1.0)


async def test_subscribe_is_pinned_to_the_first_connection_whatever_the_rotation():
    pool, made = await _pool(3)
    await pool.get_connection()
    await pool.get_connection()

    await pool.subscribe("events.>", queue="q", cb=None)
    await pool.subscribe("other.>")

    assert [c.subscribe.await_count for c in made] == [2, 0, 0]
    made[0].subscribe.assert_any_await("events.>", queue="q", cb=None)


async def test_an_empty_pool_refuses_to_hand_out_or_subscribe():
    pool = OptimizedNATSConnection(max_connections=2)

    with pytest.raises(RuntimeError, match="connect"):
        await pool.get_connection()
    with pytest.raises(RuntimeError, match="No connections"):
        await pool.subscribe("x")


async def test_a_connect_that_fails_part_way_closes_the_connections_it_opened():
    opened = _connections(2)
    pool = OptimizedNATSConnection(max_connections=3)
    sequence = [opened[0], opened[1], ConnectionRefusedError("third refused")]

    with (
        patch("cliffracer.core.dial.connect", new=AsyncMock(side_effect=sequence)),
        pytest.raises(ConnectionRefusedError),
    ):
        await pool.connect()

    assert [c.close.await_count for c in opened] == [1, 1]
    assert pool._connections == []


async def test_close_closes_every_connection_and_empties_the_pool():
    pool, made = await _pool(3)

    await pool.close()

    assert [c.drain.await_count for c in made] == [1, 1, 1]
    assert [c.close.await_count for c in made] == [0, 0, 0], (
        "a drained connection is not closed again"
    )
    assert pool._connections == []
    with pytest.raises(RuntimeError):
        await pool.get_connection()


async def test_close_keeps_closing_when_one_connection_fails_to_drain_or_to_close():
    pool, made = await _pool(3)
    made[1].drain.side_effect = RuntimeError("already gone")
    made[1].close.side_effect = RuntimeError("already gone")

    await pool.close()

    assert [c.drain.await_count for c in made] == [1, 1, 1]
    assert [c.close.await_count for c in made] == [0, 1, 0]
    assert pool._connections == []

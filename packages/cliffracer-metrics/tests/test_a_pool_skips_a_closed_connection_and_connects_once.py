"""A pool does not serve a connection that has closed for good, and is never larger than asked.

nats-py closes a connection for good once its reconnect attempts run out, and the pool rotated
over it anyway, so every Nth publish or request raised `ConnectionClosedError` for the life of
the process while `is_connected` still read true. A second `connect()` appended another
`max_connections` clients, and a permanent close was not logged anywhere.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cliffracer_metrics import OptimizedNATSConnection
from loguru import logger

pytestmark = pytest.mark.unit


def _conn(*, closed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        is_closed=closed, is_connected=not closed, publish=AsyncMock(), close=AsyncMock()
    )


def _pool(*members: SimpleNamespace) -> OptimizedNATSConnection:
    pool = OptimizedNATSConnection(max_connections=len(members))
    pool._connections = list(members)
    return pool


async def test_a_closed_connection_is_skipped_in_the_rotation():
    first, dead, last = _conn(), _conn(closed=True), _conn()
    pool = _pool(first, dead, last)

    handed_out = [await pool.get_connection() for _ in range(6)]

    assert handed_out == [first, last] * 3


async def test_publishes_never_reach_a_closed_connection():
    live, dead = _conn(), _conn(closed=True)
    pool = _pool(dead, live)

    for _ in range(4):
        await pool.publish("subject", b"{}")

    assert live.publish.await_count == 4
    assert dead.publish.await_count == 0


async def test_a_pool_whose_connections_are_all_closed_says_so():
    pool = _pool(_conn(closed=True), _conn(closed=True))

    with pytest.raises(RuntimeError, match=r"Every one of the pool's 2 connections is closed"):
        await pool.get_connection()


async def test_the_rotation_resumes_over_a_connection_that_is_back():
    flapping = _conn(closed=True)
    steady = _conn()
    pool = _pool(flapping, steady)
    assert await pool.get_connection() is steady

    flapping.is_closed = False

    assert [await pool.get_connection() for _ in range(2)] == [flapping, steady]


async def test_a_second_connect_does_not_add_connections():
    pool = OptimizedNATSConnection(max_connections=2)
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await pool.connect()
        await pool.connect()

    assert connect.call_count == 2
    assert len(pool._connections) == 2


async def test_connect_after_close_connects_the_whole_pool_again():
    pool = OptimizedNATSConnection(max_connections=2)
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await pool.connect()
        await pool.close()
        await pool.connect()

    assert connect.call_count == 4
    assert len(pool._connections) == 2


async def test_each_connection_logs_a_permanent_close_and_an_error():
    pool = OptimizedNATSConnection(max_connections=2)
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await pool.connect()
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    try:
        second = connect.call_args_list[1].kwargs
        await second["closed_cb"]()
        await second["error_cb"](ConnectionResetError("reset"))
    finally:
        logger.remove(sink)

    assert any(line.startswith("WARNING Pooled connection 2 is closed") for line in lines), lines
    assert any(
        line.startswith("ERROR Pooled connection 2 reported an error: ConnectionResetError")
        for line in lines
    ), lines


async def test_the_stats_count_the_connections_closed_for_good_and_the_errors():
    pool = OptimizedNATSConnection(max_connections=2)
    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as connect:
        await pool.connect()
    assert (pool.get_stats()["closed_connections"], pool.get_stats()["connection_errors"]) == (0, 0)

    await connect.call_args_list[0].kwargs["closed_cb"]()
    await connect.call_args_list[1].kwargs["error_cb"](ConnectionResetError("reset"))
    await connect.call_args_list[1].kwargs["error_cb"](ConnectionResetError("reset"))

    stats = pool.get_stats()
    assert (stats["closed_connections"], stats["connection_errors"]) == (1, 2)

    await pool.close()
    assert pool.get_stats()["closed_connections"] == 0


async def _connect_pool_whose_close_calls_closed_cb(
    size: int, pool: OptimizedNATSConnection | None = None
) -> OptimizedNATSConnection:
    """A pool of connections that, like nats-py's, await `closed_cb` when they are closed."""

    async def connect(url, **kwargs):
        conn = SimpleNamespace(is_closed=False)

        async def close():
            conn.is_closed = True
            await kwargs["closed_cb"]()

        conn.close = close
        conn.drain = close  # nats-py's drain ends by closing, and awaits closed_cb as close does
        return conn

    pool = pool or OptimizedNATSConnection(max_connections=size)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()
    return pool


async def test_a_clean_close_logs_no_warning_and_counts_no_connection_closed_for_good():
    pool = await _connect_pool_whose_close_calls_closed_cb(2)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    try:
        await pool.close()
    finally:
        logger.remove(sink)

    assert not [line for line in lines if line.startswith(("WARNING", "ERROR"))], lines
    assert pool.get_stats()["closed_connections"] == 0


async def test_a_connection_that_closes_after_a_reconnect_of_the_pool_is_warned_of_again():
    pool = await _connect_pool_whose_close_calls_closed_cb(1)
    await pool.close()
    await _connect_pool_whose_close_calls_closed_cb(1, pool)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    try:
        # A permanent close nats-py forces, not one `close()` asked for.
        await pool._connections[0].close()
    finally:
        logger.remove(sink)

    assert any(line.startswith("WARNING Pooled connection 1 is closed") for line in lines), lines

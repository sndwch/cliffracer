"""A pool's `connect()` opens `max_connections` once, and a cut-off `connect()` leaves none open.

Two ways the pool ended up other than it says. A `connect()` cut off by the caller's own timeout,
or cancelled, raised `CancelledError`, which is not an `Exception`, so the cleanup that closes the
connections already opened did not run: the pool kept the partial set, and the next `connect()`
saw a non-empty list and returned, leaving it partial for good. And two tasks calling `connect()`
at once on a fresh pool both passed the "already connected" check before the first had appended
anything, so both opened a full set: twice `max_connections`, `utilization_percent` at 200.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cliffracer_metrics import OptimizedNATSConnection

pytestmark = pytest.mark.unit


def _conn() -> SimpleNamespace:
    return SimpleNamespace(
        is_closed=False,
        is_connected=True,
        close=AsyncMock(),
        drain=AsyncMock(),
    )


class Dialer:
    """A stand-in for `dial.connect`: each call takes `delay` seconds and returns a new client."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.made: list[SimpleNamespace] = []

    async def __call__(self, *args, **kwargs) -> SimpleNamespace:
        await asyncio.sleep(self.delay)
        conn = _conn()
        self.made.append(conn)
        return conn


async def test_a_connect_cut_off_by_the_callers_timeout_closes_what_it_opened():
    dial = Dialer(delay=0.05)
    pool = OptimizedNATSConnection(max_connections=20)

    with patch("cliffracer.core.dial.connect", new=dial):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pool.connect(), 0.22)

    assert dial.made, "the dial never ran, so this cut nothing off"
    assert len(dial.made) < 20, "the connect finished before the timeout"
    assert pool._connections == []
    assert all(c.close.await_count == 1 for c in dial.made), "a connection opened was left open"


async def test_a_cancelled_connect_leaves_the_pool_able_to_connect_in_full():
    dial = Dialer(delay=0.05)
    pool = OptimizedNATSConnection(max_connections=6)

    with patch("cliffracer.core.dial.connect", new=dial):
        task = asyncio.create_task(pool.connect())
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        dial.delay = 0.0
        await pool.connect()

    assert len(pool._connections) == 6, "a retried connect() stayed at the partial pool"
    assert pool.get_stats()["total_connections"] == 6


async def test_two_concurrent_connects_open_one_pool():
    dial = Dialer(delay=0.01)
    pool = OptimizedNATSConnection(max_connections=3)

    with patch("cliffracer.core.dial.connect", new=dial):
        await asyncio.gather(pool.connect(), pool.connect())

    assert len(dial.made) == 3, f"opened {len(dial.made)} connections for a pool of 3"
    assert len(pool._connections) == 3
    assert pool.get_stats()["utilization_percent"] == 100


async def test_a_connect_that_fails_for_the_second_caller_too_reports_the_failure():
    """The concurrent caller does not hang or succeed on a pool whose first connect failed."""
    calls = 0

    async def failing(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        raise OSError("refused")

    pool = OptimizedNATSConnection(max_connections=2)
    with patch("cliffracer.core.dial.connect", new=failing):
        results = await asyncio.gather(pool.connect(), pool.connect(), return_exceptions=True)

    assert all(isinstance(r, OSError) for r in results), results
    assert pool._connections == []


async def test_CONTROL_a_failed_dial_still_closes_what_was_opened():
    """The `Exception` path the pool already handled: a later dial fails, earlier ones close."""
    made: list[SimpleNamespace] = []

    async def second_fails(*args, **kwargs):
        if len(made) == 1:
            raise OSError("refused")
        conn = _conn()
        made.append(conn)
        return conn

    pool = OptimizedNATSConnection(max_connections=3)
    with patch("cliffracer.core.dial.connect", new=second_fails):
        with pytest.raises(OSError):
            await pool.connect()

    assert pool._connections == []
    assert made[0].close.await_count == 1

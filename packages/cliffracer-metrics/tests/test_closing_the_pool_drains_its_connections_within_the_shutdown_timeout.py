"""Closing the pool lets the work in flight on its connections finish, within a bound.

`close()` closed each pooled connection, so a request awaiting its reply when the service stopped
failed with `ConnectionClosedError` where the service's own connection is drained last, after its
handlers. nats-py's drain stops a connection's reply subscription, so a reply still on its way is
lost; the pool therefore waits for its own requests in flight, then drains each connection, all at the
same time, within one `shutdown_timeout` between the two steps; one that has not drained by then is closed
with what it still holds, and no new request is taken once the close has begun.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cliffracer_metrics import OptimizedNATSConnection, PoolExtension
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


class FakeConnection:
    """A client that, like nats-py's, finishes what is in flight when drained, then closes."""

    def __init__(self, closed_cb, drain_seconds: float = 0.0, hang: bool = False) -> None:
        self.closed_cb = closed_cb
        self.drain_seconds = drain_seconds
        self.hang = hang
        self.is_closed = False
        self.is_connected = True
        self.close_calls = 0
        self.drain_calls = 0
        self.reply: asyncio.Event | None = None

    async def request(self, subject, payload, timeout=5.0):
        """Waits for `self.reply` to be set, then answers; a drained connection loses the reply."""
        if self.is_closed:
            raise ConnectionError("ConnectionClosedError")
        if self.reply is not None:
            await self.reply.wait()
        if self.is_closed:
            raise ConnectionError("ConnectionClosedError: the reply arrived after the drain")
        return b"reply"

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.hang:
            await asyncio.sleep(60)
        await asyncio.sleep(self.drain_seconds)
        await self._finish()

    async def close(self) -> None:
        self.close_calls += 1
        await self._finish()

    async def _finish(self) -> None:
        if not self.is_closed:
            self.is_closed = True
            await self.closed_cb()


async def _pool(size: int, **connection) -> tuple[OptimizedNATSConnection, list[FakeConnection]]:
    made: list[FakeConnection] = []

    async def connect(url, **kwargs):
        made.append(FakeConnection(kwargs["closed_cb"], **connection))
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=size, drain_timeout=1.0)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()
    return pool, made


async def test_a_request_in_flight_when_the_pool_closes_gets_its_reply():
    pool, made = await _pool(1)
    release = asyncio.Event()
    made[0].reply = release
    request = asyncio.create_task(pool.request("s", b"{}"))
    await asyncio.sleep(0)

    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0.05)
    assert not closing.done(), "close() returned while a request was still in flight"

    release.set()
    assert await asyncio.wait_for(request, 2) == b"reply"
    # The bound is a second: a close that finished only by reaching it did not notice the reply.
    await asyncio.wait_for(closing, 0.5)
    assert made[0].close_calls == 0 and made[0].is_closed


async def test_a_request_the_pool_gives_up_waiting_for_is_left_and_the_pool_still_closes():
    pool, made = await _pool(1)
    made[0].reply = asyncio.Event()  # never set: no reply is coming
    pool.drain_timeout = 0.1
    request = asyncio.create_task(pool.request("s", b"{}"))
    await asyncio.sleep(0)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    try:
        await asyncio.wait_for(pool.close(), 2)
    finally:
        logger.remove(sink)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

    assert made[0].is_closed
    assert any("1 request(s) still waiting for a reply" in line for line in lines), lines


async def test_the_wait_for_requests_and_the_drains_share_one_bound():
    """A request that never answers and a connection that never drains cost one bound, not two."""
    made: list[FakeConnection] = []

    async def connect(url, **kwargs):
        made.append(FakeConnection(kwargs["closed_cb"], hang=True))
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=1, drain_timeout=0.3)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()
    made[0].reply = asyncio.Event()  # never set: no reply is coming
    request = asyncio.create_task(pool.request("s", b"{}"))
    await asyncio.sleep(0)

    started = time.perf_counter()
    try:
        await asyncio.wait_for(pool.close(), 2)
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    elapsed = time.perf_counter() - started

    assert made[0].is_closed and made[0].close_calls == 1
    # Upper bound. CI p99 0.302 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 96x the overshoot; below 0.6 s (the wait for requests and the drain each given 0.3 s, in turn).
    assert elapsed < 0.45, (
        f"close() took {elapsed:.2f}s against a 0.3 s bound: each step had its own"
    )


async def test_the_drains_get_what_the_wait_for_requests_left_of_the_bound():
    """A request that answers late leaves the drain the rest of the bound, and no more."""
    made: list[FakeConnection] = []

    async def connect(url, **kwargs):
        made.append(FakeConnection(kwargs["closed_cb"], hang=True))
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=1, drain_timeout=0.4)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()
    release = asyncio.Event()
    made[0].reply = release
    request = asyncio.create_task(pool.request("s", b"{}"))
    await asyncio.sleep(0)
    asyncio.get_running_loop().call_later(0.2, release.set)

    started = time.perf_counter()
    await asyncio.wait_for(pool.close(), 2)
    elapsed = time.perf_counter() - started

    assert await request == b"reply"
    # Upper bound. CI p99 0.402 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.4 s,
    # 77x the overshoot; below 0.6 s (0.4 counted from the release at 0.2).
    # Lower bound: the 0.4 s bound less slack; a close that ended at the 0.2 s reply falls under it.
    # Load can only lengthen it.
    assert 0.35 < elapsed < 0.55, f"close() took {elapsed:.2f}s: the bound is 0.4 s from the start"


async def test_no_request_or_publish_is_taken_once_the_close_has_begun():
    pool, made = await _pool(1)
    release = asyncio.Event()
    made[0].reply = release
    in_flight = asyncio.create_task(pool.request("s", b"{}"))
    await asyncio.sleep(0)
    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0.02)

    with pytest.raises(RuntimeError, match="is closing and takes no new requests"):
        await pool.request("s", b"{}")
    with pytest.raises(RuntimeError, match="is closing and takes no new requests"):
        await pool.publish("s", b"{}")

    release.set()
    await asyncio.wait_for(asyncio.gather(in_flight, closing), 2)


async def test_a_pool_reconnected_after_a_close_takes_requests_again():
    pool, made = await _pool(1)
    await pool.close()

    async def connect(url, **kwargs):
        made.append(FakeConnection(kwargs["closed_cb"]))
        return made[-1]

    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()

    assert await pool.request("s", b"{}") == b"reply"


async def test_the_connections_drain_at_the_same_time_not_one_after_another():
    pool, made = await _pool(4, drain_seconds=0.15)

    started = time.perf_counter()
    await pool.close()
    elapsed = time.perf_counter() - started

    assert all(c.is_closed for c in made)
    # Upper bound. CI p99 0.152 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.15 s,
    # 183x the overshoot; below 0.6 s (four 0.15 drains in turn).
    assert elapsed < 0.45, f"four 0.15 s drains took {elapsed:.2f}s: they ran in turn"


async def test_a_connection_that_does_not_drain_in_time_is_closed_and_the_others_are_not_held_up():
    made: list[FakeConnection] = []

    async def connect(url, **kwargs):
        made.append(FakeConnection(kwargs["closed_cb"], hang=len(made) == 1))
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=3, drain_timeout=0.1)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    try:
        await pool.close()
    finally:
        logger.remove(sink)

    assert [c.close_calls for c in made] == [0, 1, 0]
    assert all(c.is_closed for c in made)
    assert any(line.startswith("WARNING Connection 2 did not drain") for line in lines), lines


async def test_with_no_drain_timeout_a_slow_drain_is_waited_for_not_cut_short():
    made: list[FakeConnection] = []

    async def connect(url, **kwargs):
        made.append(FakeConnection(kwargs["closed_cb"], drain_seconds=0.2))
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=1, drain_timeout=None)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()

    await pool.close()

    assert made[0].close_calls == 0 and made[0].is_closed


async def test_a_clean_close_by_draining_is_silent():
    pool, _ = await _pool(2)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    try:
        await pool.close()
    finally:
        logger.remove(sink)

    assert not [line for line in lines if line.startswith(("WARNING", "ERROR"))], lines
    assert pool.get_stats()["closed_connections"] == 0


async def test_a_connection_already_closed_for_good_is_neither_drained_nor_closed_again():
    pool, made = await _pool(2)
    await made[0].close()
    made[0].close_calls = 0

    await pool.close()

    assert (made[0].drain_calls, made[0].close_calls) == (0, 0)
    assert made[1].is_closed


async def test_a_connect_that_fails_part_way_closes_what_it_opened_without_draining_it():
    made: list[FakeConnection] = []

    async def connect(url, **kwargs):
        if len(made) == 2:
            raise ConnectionRefusedError("refused")
        made.append(FakeConnection(kwargs["closed_cb"]))
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=3)
    with patch("cliffracer.core.dial.connect", new=connect), pytest.raises(ConnectionRefusedError):
        await pool.connect()

    assert [(c.drain_calls, c.close_calls) for c in made] == [(0, 1), (0, 1)]


@pytest.mark.parametrize("configured", [12.0, None])
async def test_the_extension_gives_the_pool_the_services_shutdown_timeout(configured):
    class Svc(CliffracerService):
        pooled = PoolExtension(max_connections=1)

    svc = Svc(ServiceConfig(name="stops", health_port=0, shutdown_timeout=configured))
    await svc.container._setup_extensions()

    assert svc.pooled.pool is not None
    assert svc.pooled.pool.drain_timeout == configured


async def test_stopping_the_extension_drains_the_pool():
    class Svc(CliffracerService):
        pooled = PoolExtension(max_connections=1)

    svc = Svc(ServiceConfig(name="stops", health_port=0))
    await svc.container._setup_extensions()
    conn = SimpleNamespace(is_closed=False, is_connected=True, drain=AsyncMock(), close=AsyncMock())
    svc.pooled.pool._connections = [conn]

    await svc.pooled.stop()

    conn.drain.assert_awaited_once()
    conn.close.assert_not_awaited()

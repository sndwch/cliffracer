"""`stop()` against a broker that has gone silent is bounded by `shutdown_timeout` and does not fail.

A path to the broker that drops packets still reads as connected for minutes, so `disconnect` drains,
and the drain flushes and waits on a PONG that never comes. It waited nats-py's own 10 seconds
whatever `shutdown_timeout` was, and then `FlushTimeoutError` escaped: the lifecycle recorded it as a
teardown error and `stop()` raised it, reporting a failed stop although every other step had finished.
The drain is bounded by `shutdown_timeout`, and one that times out is a warning followed by the close.

The broker here is an in-process server that completes the NATS handshake and then, when told to,
reads and ignores everything: no PONG.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from nats.errors import FlushTimeoutError

from cliffracer import CliffracerService, ServiceConfig, listener

pytestmark = pytest.mark.unit

LOOPBACK = "127.0.0.1"


class Silent:
    """A fake broker; `go_silent()` is the partition: bytes are read and nothing is answered."""

    def __init__(self) -> None:
        self.silent = False
        self.server: asyncio.Server | None = None
        #: Every PING read, answered or not. A drain flushes with a PING and waits on the PONG, so a
        #: stop that drained is a stop that sent one more PING than the start did.
        self.pings = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        info = {
            "server_id": "fake",
            "version": "2.10.0",
            "proto": 1,
            "max_payload": 1048576,
            "headers": True,
            "host": LOOPBACK,
            "port": 0,
        }
        writer.write(b"INFO " + json.dumps(info).encode() + b"\r\n")
        await writer.drain()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if line.startswith(b"PING"):
                    self.pings += 1
                if self.silent:
                    continue
                if line.startswith(b"PING"):
                    writer.write(b"PONG\r\n")
                    await writer.drain()
        except ConnectionError:
            return
        finally:
            # A connection the handler leaves open holds `Server.wait_closed()` on Python 3.12.
            writer.close()

    async def start(self) -> str:
        self.server = await asyncio.start_server(self.handle, LOOPBACK, 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"nats://{LOOPBACK}:{port}"

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


class Svc(CliffracerService):
    @listener("things.happened", fanout=True)
    async def on_thing(self, subject: str) -> None: ...


@pytest.fixture
async def broker():
    fake = Silent()
    url = await fake.start()
    yield fake, url
    await fake.close()


def _warnings() -> tuple[list[str], int]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING", format="{message}")
    return lines, sink


async def _started(url: str, **config) -> Svc:
    svc = Svc(
        ServiceConfig(name="silent", nats_url=url, health_port=0, connect_timeout=5, **config)
    )
    await svc.start()
    return svc


async def test_a_stop_against_a_silent_broker_returns_within_shutdown_timeout_and_does_not_raise(
    broker,
):
    fake, url = broker
    svc = await _started(url, shutdown_timeout=1.0)
    fake.silent = True
    await asyncio.sleep(0.2)
    assert svc.container.broker_state.value == "connected", "the partition is not yet noticed"
    lines, sink = _warnings()

    started = time.monotonic()
    try:
        await asyncio.wait_for(svc.stop(), 30)
    finally:
        logger.remove(sink)
    took = time.monotonic() - started

    # Upper bound. CI p99 1 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 1 s, 398x
    # the overshoot.
    assert took < 2.5, f"stop took {took:.1f}s with shutdown_timeout=1.0"
    assert svc.container.is_stopped
    assert svc.container.nc is not None and svc.container.nc.is_closed
    assert any("could not drain its NATS connection" in line for line in lines), lines


async def test_the_warning_says_what_may_have_been_lost(broker):
    fake, url = broker
    svc = await _started(url, shutdown_timeout=0.5)
    fake.silent = True
    lines, sink = _warnings()

    try:
        await asyncio.wait_for(svc.stop(), 30)
    finally:
        logger.remove(sink)

    (line,) = [line for line in lines if "could not drain" in line]
    assert "messages still buffered may not have been sent" in line


async def test_CONTROL_a_stop_against_a_broker_that_answers_drains_and_does_not_warn(broker):
    _, url = broker
    svc = await _started(url, shutdown_timeout=1.0)
    lines, sink = _warnings()

    try:
        await asyncio.wait_for(svc.stop(), 30)
    finally:
        logger.remove(sink)

    assert svc.container.is_stopped
    assert not [line for line in lines if "could not drain" in line]


async def test_CONTROL_with_no_deadline_a_broker_that_answers_is_still_drained(broker):
    """With no deadline the drain still happens: the broker sees the PING a drain flushes with,
    the stop logs no warning, and the container is stopped. `is_stopped` alone is set by the stop
    whatever the drain did, so the PING count is what shows the drain."""
    fake, url = broker
    svc = await _started(url, shutdown_timeout=None)
    pings_before_stop = fake.pings
    lines, sink = _warnings()

    try:
        await asyncio.wait_for(svc.stop(), 30)
    finally:
        logger.remove(sink)

    assert svc.container.is_stopped
    assert fake.pings > pings_before_stop, (
        "no PING reached the broker during the stop: it did not drain"
    )
    assert not [line for line in lines if "could not drain" in line]


async def test_a_flush_timeout_from_nats_py_itself_is_a_warning_and_the_connection_is_closed():
    # `shutdown_timeout` is longer than nats-py's own flush timeout, so it is nats-py that gives up.
    svc = Svc(ServiceConfig(name="flush", health_port=0, shutdown_timeout=30.0))
    connection = svc.container.connection
    connection.nc = AsyncMock()
    connection.nc.is_connecting = False
    connection.nc.is_reconnecting = False
    connection.nc.is_closed = False
    connection.nc.is_connected = True
    connection.nc.is_draining = False
    connection.nc.drain = AsyncMock(side_effect=FlushTimeoutError())
    lines, sink = _warnings()

    try:
        await connection.disconnect()
    finally:
        logger.remove(sink)

    connection.nc.close.assert_awaited_once()
    assert any("FlushTimeoutError" in line for line in lines), lines


async def test_CONTROL_a_drain_that_is_refused_because_the_connection_is_closed_is_still_quiet():
    from nats.errors import ConnectionClosedError

    svc = Svc(ServiceConfig(name="closed", health_port=0, shutdown_timeout=30.0))
    connection = svc.container.connection
    connection.nc = AsyncMock()
    connection.nc.is_connecting = False
    connection.nc.is_reconnecting = False
    connection.nc.is_closed = False
    connection.nc.is_connected = True
    connection.nc.is_draining = False
    connection.nc.drain = AsyncMock(side_effect=ConnectionClosedError())
    lines, sink = _warnings()

    try:
        await connection.disconnect()
    finally:
        logger.remove(sink)

    connection.nc.close.assert_awaited_once()
    assert not [line for line in lines if "could not drain" in line]

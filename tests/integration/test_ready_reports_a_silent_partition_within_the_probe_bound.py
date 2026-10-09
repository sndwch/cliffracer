"""Readiness goes down within the probe bound when the connection goes silent, and comes back with it.

A connection that stops answering without being reset is invisible to the kernel and, for minutes,
to nats-py: `is_connected` stays True until the ping loop gives up (240 to 360 seconds at the
defaults). Readiness read only that flag, so a pod behind such a partition stayed in rotation. The
readiness check now sends the broker a round trip, bounded by `broker_probe_timeout`.

This runs the real service through a proxy that stops forwarding in both directions and closes
nothing, with nats-py's default pings, so the flag stays True throughout and only the round trip can
tell. The bound here is 1 second and the cache 0.2.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig
from tests.integration.test_a_silent_partition_is_noticed_within_the_ping_window import (
    SilentProxy,
    proxy_tasks,
)


class HoldingProxy(SilentProxy):
    """A partition that holds the bytes instead of discarding them, as TCP does.

    `SilentProxy` discards what arrives while it is silent. Inside an established TCP session
    nothing is discarded: the sender retransmits, and when the path heals every PING gets its
    PONG, in order. A proxy that discards leaves a future queued in nats-py for a PING that is
    never answered, and every later flush on that connection (the drain at stop, say) is then one
    PONG short. So while silent this one stops reading, lets the bytes wait, and delivers them
    when the path returns.
    """

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        super().__init__()

    @property
    def silent(self) -> bool:  # type: ignore[override]
        return not self._open.is_set()

    @silent.setter
    def silent(self, value: bool) -> None:
        if value:
            self._open.clear()
        else:
            self._open.set()

    async def _pump(self, source, sink) -> None:
        try:
            while data := await source.read(65536):
                await self._open.wait()
                sink.write(data)
                await sink.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sink.close()


pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

BOUND = 1.0
CACHE = 0.2
SLACK = 0.8


class DelayingProxy(HoldingProxy):
    """A path that delivers everything, in order, `delay` seconds late in each direction."""

    delay = 0.0

    async def _pump(self, source, sink) -> None:
        try:
            while data := await source.read(65536):
                await self._open.wait()
                if self.delay:
                    await asyncio.sleep(self.delay)
                sink.write(data)
                await sink.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sink.close()


async def _status_changes_from(svc: CliffracerService, status: str, within: float):
    """The first health payload whose status is not `status`, and how long it took."""
    began = time.monotonic()
    while time.monotonic() - began < within:
        health = await svc.health_check()
        if health["status"] != status:
            return health, time.monotonic() - began
        await asyncio.sleep(0.05)
    raise AssertionError(f"status was still {status!r} after {within}s")


async def test_a_silent_partition_makes_readiness_disconnected_within_the_bound_and_it_recovers():
    async with HoldingProxy() as proxy:
        svc = CliffracerService(
            ServiceConfig(
                name="ready_silent",
                nats_url=proxy.url,
                health_port=0,
                broker_probe_timeout=BOUND,
                broker_probe_cache=CACHE,
                reconnect_time_wait=0,
                max_reconnect_attempts=-1,
            )
        )
        await svc.start()
        try:
            healthy = await svc.health_check()
            assert healthy["status"] == "healthy" and healthy["nats_connected"] is True
            assert isinstance(healthy["nats_rtt_ms"], float)

            proxy.silent = True
            down, took = await _status_changes_from(
                svc, "healthy", within=BOUND + CACHE + SLACK + 2
            )

            assert down["status"] == "disconnected" and down["nats_connected"] is False
            assert down["nats_rtt_ms"] is None
            # Upper bound. CI p99 1.2 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait
            # 1.2 s, 194x the overshoot.
            assert took <= BOUND + CACHE + SLACK, f"readiness took {took:.2f}s to notice"
            assert svc.nc.is_connected, (
                "nats-py's own flag had already noticed: this proves nothing"
            )
            assert down["broker_state"] == "connected"

            proxy.silent = False
            back, recovered_in = await _status_changes_from(svc, "disconnected", within=BOUND + 3)
            assert back["status"] == "healthy" and back["nats_connected"] is True, back
        finally:
            proxy.silent = False
            await asyncio.wait_for(svc.stop(), timeout=20)

    assert not proxy_tasks(), f"the proxy left tasks running: {proxy_tasks()}"


async def test_with_the_probe_turned_off_a_silent_partition_is_not_seen_by_readiness():
    """The control that says the round trip is what does the work, and the off switch works."""
    async with HoldingProxy() as proxy:
        svc = CliffracerService(
            ServiceConfig(
                name="ready_silent_off",
                nats_url=proxy.url,
                health_port=0,
                broker_probe_timeout=None,
                reconnect_time_wait=0,
                max_reconnect_attempts=-1,
            )
        )
        await svc.start()
        try:
            proxy.silent = True
            await asyncio.sleep(BOUND + CACHE + SLACK + 1)

            health = await svc.health_check()

            assert health["status"] == "healthy", "readiness saw the partition without a round trip"
            assert health["nats_rtt_ms"] is None
        finally:
            proxy.silent = False
            await asyncio.wait_for(svc.stop(), timeout=20)

    assert not proxy_tasks(), f"the proxy left tasks running: {proxy_tasks()}"


async def test_after_the_path_heals_the_clients_reader_is_alive_and_messages_arrive(
    nats_connection,
):
    """Probing a partition must not damage the connection it probes.

    A round trip that is abandoned by cancelling the PONG future nats-py is waiting on leaves a
    cancelled future at the head of the client's queue. After the path heals the first PONG
    resolves it, `set_result` raises `InvalidStateError` inside nats-py's read loop, and the loop
    ends without telling the client: `is_connected` stays True, readiness reads a round trip that
    short-circuits as healthy, and nothing the broker sends is ever read again. Several probes in
    one partition leave several such futures.
    """
    received: list[bytes] = []

    async def on_message(msg) -> None:
        received.append(msg.data)

    async with HoldingProxy() as proxy:
        svc = CliffracerService(
            ServiceConfig(
                name="ready_heal",
                nats_url=proxy.url,
                health_port=0,
                broker_probe_timeout=0.3,
                broker_probe_cache=0,
                reconnect_time_wait=0,
                max_reconnect_attempts=-1,
            )
        )
        await svc.start()
        try:
            await svc.nc.subscribe("ready.heal.probe", cb=on_message)
            await svc.nc.flush()

            proxy.silent = True
            for _ in range(6):
                assert (await svc.health_check())["status"] == "disconnected"

            proxy.silent = False
            deadline = time.monotonic() + 10
            while (await svc.health_check())["status"] != "healthy":
                assert time.monotonic() < deadline, "readiness never recovered after the heal"
                await asyncio.sleep(0.1)

            await nats_connection.publish("ready.heal.probe", b"after the heal")
            await nats_connection.flush()
            for _ in range(60):
                if received:
                    break
                await asyncio.sleep(0.05)

            assert received == [b"after the heal"], (
                "a message published after the heal never reached the service: its read loop died"
            )
            assert svc.nc.is_connected
        finally:
            proxy.silent = False
            await asyncio.wait_for(svc.stop(), timeout=20)

    assert not proxy_tasks(), f"the proxy left tasks running: {proxy_tasks()}"


async def test_a_broker_slower_than_the_bound_is_never_reported_healthy_and_recovers(
    nats_connection,
):
    """The broker answers every PING, in order, but always later than the bound.

    Each round trip takes 0.6 s against a 0.4 s bound, so the PONG for one probe lands inside the
    NEXT probe's window. That PONG is not the next probe's answer: counting it made this read
    healthy part of the time. Once the delay goes the status recovers and the reader is alive.
    """
    received: list[bytes] = []

    async def on_message(msg) -> None:
        received.append(msg.data)

    async with DelayingProxy() as proxy:
        svc = CliffracerService(
            ServiceConfig(
                name="ready_slow",
                nats_url=proxy.url,
                health_port=0,
                broker_probe_timeout=0.4,
                broker_probe_cache=0,
                reconnect_time_wait=0,
                max_reconnect_attempts=-1,
            )
        )
        await svc.start()
        try:
            await svc.nc.subscribe("ready.slow.probe", cb=on_message)
            await svc.nc.flush()
            assert (await svc.health_check())["status"] == "healthy"

            proxy.delay = 0.3
            statuses = [(await svc.health_check())["status"] for _ in range(8)]
            assert statuses == ["disconnected"] * 8, statuses

            proxy.delay = 0.0
            deadline = time.monotonic() + 10
            while (await svc.health_check())["status"] != "healthy":
                assert time.monotonic() < deadline, "readiness never recovered once the delay went"
                await asyncio.sleep(0.1)

            await nats_connection.publish("ready.slow.probe", b"after the delay")
            await nats_connection.flush()
            for _ in range(60):
                if received:
                    break
                await asyncio.sleep(0.05)
            assert received == [b"after the delay"]
        finally:
            proxy.delay = 0.0
            proxy.silent = False
            await asyncio.wait_for(svc.stop(), timeout=20)

    assert not proxy_tasks(), f"the proxy left tasks running: {proxy_tasks()}"

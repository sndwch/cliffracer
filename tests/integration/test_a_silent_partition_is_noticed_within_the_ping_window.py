"""With the ping settings set, a connection that goes silent is noticed inside the window they give.

A partition that drops packets without resetting the socket is not seen by the kernel; only nats-py's
ping loop sees it. It counts one outstanding ping per `ping_interval` (a PONG resets the count) and
calls the connection stale when the count passes `max_outstanding_pings`. If the partition begins
just after a ping was answered, the count reaches `max_outstanding_pings + 1` on tick
`max_outstanding_pings + 1` after that one, so detection falls between
`max_outstanding_pings * ping_interval` and `(max_outstanding_pings + 1) * ping_interval` seconds
after the partition begins, depending where in the interval it began.

This measures that on the suite's broker through a proxy that stops forwarding in both directions
and keeps every socket open, at settings small enough to measure, and reads the moment the service
reports the loss from the `on_disconnect` hook. The default (120 s, 2) gives the same formula, 240
to 360 seconds; this is the part that can be observed in a test.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, SharedDependency

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

LOOPBACK = "127.0.0.1"  # this test's own proxy, which is no suite address

# Scheduling and the proxy's own hops add a little to what the formula gives.
SLACK = 0.4


def proxy_tasks() -> list[asyncio.Task]:
    """Every task alive that belongs to a proxy of this family: an accept handler or a pump."""
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and "Proxy" in getattr(task.get_coro(), "__qualname__", "")
    ]


class SilentProxy:
    """A TCP proxy to the suite's broker that can go silent without closing anything.

    It leaves nothing running when it closes, whatever was in flight. Each connection is served by a
    task the server starts, which opens the upstream connection and then starts two pump tasks, so a
    client that arrives while the proxy is closing (one reconnecting during a partition does) has its
    pumps started after a teardown that only cancelled the pumps it already knew about. The accept
    task is therefore tracked too, anything arriving once closing has begun is closed at once, and
    `__aexit__` cancels and awaits until no task or writer is left.
    """

    def __init__(self) -> None:
        upstream = urlparse(ServiceConfig.model_fields["nats_url"].default)
        assert upstream.hostname and upstream.port, "the suite's broker URL names no host and port"
        self._upstream = (upstream.hostname, upstream.port)
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.silent = False

    async def __aenter__(self) -> SilentProxy:
        self._server = await asyncio.start_server(self._accept, LOOPBACK, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._closing = True
        self._server.close()
        # A connection the server had already accepted has its handler started within a loop turn or
        # two; let those turns pass so the sweep below sees every task there is going to be.
        for _ in range(3):
            await asyncio.sleep(0)
        while self._tasks or self._writers:
            writers, self._writers = self._writers, []
            for writer in writers:
                writer.close()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await asyncio.sleep(0)  # the done callbacks that drop each task from the set
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"nats://{LOOPBACK}:{self.port}"

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _accept(self, reader, writer) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._track(current)
        if self._closing:
            writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(*self._upstream)
        except BaseException:
            writer.close()  # the upstream refused, or this task was cancelled while it dialled
            raise
        if self._closing:
            writer.close()
            up_writer.close()
            return
        self._writers += [writer, up_writer]
        for source, sink in ((reader, up_writer), (up_reader, writer)):
            self._track(asyncio.create_task(self._pump(source, sink)))

    async def _pump(self, source, sink) -> None:
        try:
            while data := await source.read(65536):
                if self.silent:
                    continue  # read it, drop it, close nothing
                sink.write(data)
                await sink.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sink.close()


class Stamp(Extension):
    def __init__(self, losses: list[float]):
        self.losses = losses

    async def on_disconnect(self) -> None:
        self.losses.append(time.monotonic())


@pytest.mark.parametrize("interval, outstanding", [(0.5, 2), (1.0, 1)])
async def test_a_silent_partition_is_noticed_between_the_two_ends_of_the_window(
    interval, outstanding
):
    losses: list[float] = []

    async with SilentProxy() as proxy:

        class Svc(CliffracerService):
            stamp = Stamp(SharedDependency(losses))

        svc = Svc(
            ServiceConfig(
                name="silent_partition",
                nats_url=proxy.url,
                health_port=0,
                ping_interval=interval,
                max_outstanding_pings=outstanding,
                reconnect_time_wait=0,
                max_reconnect_attempts=-1,
            )
        )
        await svc.start()
        try:
            # Let a ping be answered, so the partition begins on a clean count.
            await asyncio.sleep(interval * 1.5)
            assert not losses, "the connection was reported lost before anything was wrong"

            began = time.monotonic()
            proxy.silent = True
            deadline = began + (outstanding + 1) * interval + 3
            while not losses and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert losses, "the partition was never noticed"
            elapsed = losses[0] - began

            # Lower bound: the window's low end less SLACK; earlier means a ping was counted before
            # it was due. Load can only lengthen it.
            assert elapsed > outstanding * interval - SLACK, (
                f"noticed after {elapsed:.2f}s, before the window "
                f"{outstanding * interval:g}s to {(outstanding + 1) * interval:g}s"
            )
            # Upper bound. CI p99 at (0.5, 2) 1.25 s; at (1.0, 1) 1.5 s (run 4712: eric-7, CPython
            # 3.12.15, n=20 each, p99 = max); at (0.5, 2) p99 inside the 1.5 s designed window; at
            # (1.0, 1) p99 inside the 2 s designed window.
            assert elapsed <= (outstanding + 1) * interval + SLACK, (
                f"noticed after {elapsed:.2f}s, after the window "
                f"{outstanding * interval:g}s to {(outstanding + 1) * interval:g}s"
            )
        finally:
            proxy.silent = False
            await asyncio.wait_for(svc.stop(), timeout=20)

    assert not proxy_tasks(), f"the proxy left tasks running: {proxy_tasks()}"

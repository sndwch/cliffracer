"""An extension hears a real connection loss and reconnect, and what it does inside the hook counts.

The connection runs through a proxy this test owns, between the service and the suite's broker, and
the test cuts the proxy's sockets. The client sees a dropped connection, reconnects through the
proxy and replays its subscriptions exactly as it does when a broker restarts, without anything
being done to the broker the suite shares.

What is pinned is what the actors design needs: the disconnect hook runs while the client is
disconnected and before the reconnect completes, the reconnect hook runs after, a subscription an
extension drops inside `on_disconnect` is not replayed, and a hook that waits delays the reconnect.
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


async def until(condition, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


class Proxy:
    """A TCP proxy to the suite's broker whose connections the test can cut.

    It leaves nothing running when it closes, whatever was in flight. Each connection is served by
    a task the server starts, which opens the upstream connection and then starts two pump tasks, so
    a client that arrives while the proxy is closing (one reconnecting after a cut does) has its
    pumps started after a teardown that only cancelled the pumps it already knew about. The accept
    task is therefore tracked too, anything arriving once closing has begun is closed at once, and
    `__aexit__` cancels and awaits until no task or writer is left.
    """

    def __init__(self) -> None:
        upstream = urlparse(ServiceConfig.model_fields["nats_url"].default)
        assert upstream.hostname and upstream.port, "the suite's broker URL names no host and port"
        self._upstream = (upstream.hostname, upstream.port)
        self._pairs: list[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.connections = 0

    async def __aenter__(self) -> Proxy:
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
        while self._tasks or self._pairs:
            await self.sever()
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
        self.connections += 1
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
        self._pairs.append((writer, up_writer))
        for source, sink in ((reader, up_writer), (up_reader, writer)):
            self._track(asyncio.create_task(self._pump(source, sink)))

    @staticmethod
    async def _pump(source, sink) -> None:
        try:
            while data := await source.read(65536):
                sink.write(data)
                await sink.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sink.close()

    async def sever(self) -> None:
        """Cut every connection through the proxy; the proxy keeps accepting new ones."""
        pairs, self._pairs = self._pairs, []
        for client_side, upstream_side in pairs:
            client_side.close()
            upstream_side.close()


class Hooks(Extension):
    """Records what it saw, and the connection's own state at that moment."""

    def __init__(
        self,
        events: list,
        *,
        drop_on_disconnect: bool = False,
        wait: float = 0.0,
        wait_on_reconnect: float = 0.0,
    ):
        self.events = events
        self.drop_on_disconnect = drop_on_disconnect
        self.wait = wait
        self.wait_on_reconnect = wait_on_reconnect
        self.dropped = None

    def _nc(self):
        return self.service.container.connection.nc

    async def start(self) -> None:
        nc = self._nc()
        self.dropped = await nc.subscribe("hooks.drop", cb=self._on_drop)
        await nc.subscribe("hooks.keep", cb=self._on_keep)
        await nc.flush()

    async def _on_drop(self, msg) -> None:
        self.events.append(("received", "drop", msg.data.decode()))

    async def _on_keep(self, msg) -> None:
        self.events.append(("received", "keep", msg.data.decode()))

    async def on_disconnect(self) -> None:
        self.events.append(("on_disconnect", time.monotonic(), self._nc().is_connected))
        if self.drop_on_disconnect and self.dropped is not None:
            await self.dropped.unsubscribe()
        if self.wait:
            await asyncio.sleep(self.wait)

    async def on_reconnect(self) -> None:
        self.events.append(("on_reconnect", time.monotonic(), self._nc().is_connected))
        if self.wait_on_reconnect:
            await asyncio.sleep(self.wait_on_reconnect)
            self.events.append(("on_reconnect_returned", time.monotonic(), None))


async def _started(proxy: Proxy, events: list, **options) -> CliffracerService:
    class Svc(CliffracerService):
        # Shared, not copied: an extension's arguments are copied per service.
        hooks = Hooks(SharedDependency(events), **options)

    svc = Svc(
        ServiceConfig(
            name="hooks_live",
            nats_url=proxy.url,
            health_port=0,
            reconnect_time_wait=0,
            max_reconnect_attempts=-1,
        )
    )
    await svc.start()
    return svc


def _names(events: list) -> list[str]:
    return [e[0] for e in events if e[0] != "received"]


async def test_the_hooks_run_on_a_lost_connection_and_a_regained_one_in_that_order(
    nats_connection,
):
    events: list = []
    async with Proxy() as proxy:
        svc = await _started(proxy, events)
        try:
            await proxy.sever()
            await until(lambda: "on_reconnect" in _names(events), "the reconnect hook")
        finally:
            await svc.stop()

    assert _names(events)[:2] == ["on_disconnect", "on_reconnect"]
    disconnect = next(e for e in events if e[0] == "on_disconnect")
    reconnect = next(e for e in events if e[0] == "on_reconnect")
    assert disconnect[2] is False, "on_disconnect ran while the client was disconnected"
    assert reconnect[2] is True, "on_reconnect ran once the connection was back"
    assert disconnect[1] < reconnect[1]


async def test_a_subscription_dropped_inside_on_disconnect_is_not_replayed(nats_connection):
    events: list = []
    async with Proxy() as proxy:
        svc = await _started(proxy, events, drop_on_disconnect=True)
        try:
            await proxy.sever()
            await until(lambda: "on_reconnect" in _names(events), "the reconnect hook")
            await asyncio.sleep(0.2)
            await nats_connection.publish("hooks.drop", b"after")
            await nats_connection.publish("hooks.keep", b"after")
            await nats_connection.flush()
            await until(lambda: ("received", "keep", "after") in events, "the kept subscription")
            await asyncio.sleep(0.3)
        finally:
            await svc.stop()

    assert ("received", "keep", "after") in events
    assert ("received", "drop", "after") not in events, "the dropped subscription was replayed"


async def test_CONTROL_a_subscription_left_alone_is_replayed_on_the_same_reconnect(
    nats_connection,
):
    events: list = []
    async with Proxy() as proxy:
        svc = await _started(proxy, events)
        try:
            await proxy.sever()
            await until(lambda: "on_reconnect" in _names(events), "the reconnect hook")
            await asyncio.sleep(0.2)
            await nats_connection.publish("hooks.drop", b"after")
            await nats_connection.flush()
            await until(
                lambda: ("received", "drop", "after") in events, "the replayed subscription"
            )
        finally:
            await svc.stop()

    assert ("received", "drop", "after") in events


async def test_a_hook_that_waits_delays_the_reconnect_by_as_long(nats_connection):
    events: list = []
    async with Proxy() as proxy:
        svc = await _started(proxy, events, wait=0.7)
        try:
            await proxy.sever()
            await until(lambda: "on_reconnect" in _names(events), "the reconnect hook", timeout=15)
        finally:
            await svc.stop()

    disconnect = next(e for e in events if e[0] == "on_disconnect")
    reconnect = next(e for e in events if e[0] == "on_reconnect")
    assert reconnect[1] - disconnect[1] >= 0.65, "the reconnect did not wait for the hook"


async def test_a_slow_on_reconnect_does_not_hold_the_regained_connection_back(nats_connection):
    """By the time `on_reconnect` runs the client is connected and has replayed its subscriptions,
    so traffic flows while the hook waits: the cost of a slow one is not a stalled service."""
    events: list = []
    async with Proxy() as proxy:
        svc = await _started(proxy, events, wait_on_reconnect=2.0)
        try:
            await proxy.sever()
            await until(lambda: "on_reconnect" in _names(events), "the reconnect hook")
            await nats_connection.publish("hooks.keep", b"during")
            await nats_connection.flush()
            await until(lambda: ("received", "keep", "during") in events, "traffic during the hook")
            assert "on_reconnect_returned" not in _names(events), "the hook had already returned"
        finally:
            await svc.stop()

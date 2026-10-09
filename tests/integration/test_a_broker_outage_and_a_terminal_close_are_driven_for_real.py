"""A service lives through a broker outage, and stops when nats-py gives up, driven end to end.

ADR-0008 says a service stays alive through a broker outage and resumes when the broker returns;
ADR-0007 says it winds down when the connection is permanently closed. Both were checked by calling
the callback body directly or by closing the client by hand, so nothing showed that nats-py reaches
the callbacks, that the subscriptions are replayed after a reconnect, or that an exhausted reconnect
budget stops the service.

The outage is a TCP forwarder between the service and the suite's broker that can be severed (every
open connection is reset and nothing listens) and restored on the same port. The service sees what
it sees when a broker goes away: its socket resets and every redial is refused. The broker itself
stays up, so a client that talks to it directly can ask the service something while the service's
own link is down. It is not a stopped broker process, so a broker losing its JetStream state is not
covered here.
"""

import asyncio
import json
from urllib.parse import urlsplit

import nats
import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener, rpc
from cliffracer.core.connection import BrokerConnectionState
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.testing.waiting import wait_until
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required, pytest.mark.slow]

LOOPBACK = "127.0.0.1"
WITHIN = 20.0


class Cut:
    """A forwarder to the broker whose link can be severed and restored on one port."""

    def __init__(self) -> None:
        upstream = urlsplit(broker_url())
        self._upstream = (upstream.hostname or LOOPBACK, upstream.port or 0)
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    @property
    def url(self) -> str:
        return f"nats://{LOOPBACK}:{self.port}"

    async def open(self) -> None:
        self._server = await asyncio.start_server(self._forward, LOOPBACK, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def sever(self) -> None:
        """Reset every open connection and stop listening, so each redial is refused."""
        assert self._server is not None
        server, self._server = self._server, None
        server.close()
        # `wait_closed` waits for every open connection to end (Python 3.12+), so they go first.
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()
        await asyncio.wait_for(server.wait_closed(), 5)

    async def restore(self) -> None:
        await self.open()

    async def close(self) -> None:
        if self._server is not None:
            await self.sever()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _forward(
        self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter
    ) -> None:
        try:
            broker_r, broker_w = await asyncio.open_connection(*self._upstream)
        except OSError:
            client_w.transport.abort()
            return
        self._writers |= {client_w, broker_w}

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                dst.transport.abort()

        for task in (
            asyncio.create_task(pump(client_r, broker_w)),
            asyncio.create_task(pump(broker_r, client_w)),
        ):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


@pytest.fixture
async def cut():
    link = Cut()
    await link.open()
    yield link
    await link.close()


async def _ask(name: str, method: str, **args) -> dict:
    """An RPC to `name` from a client that is not on the cut link."""
    subject = HandlerDiscovery.with_namespace(ServiceConfig(name=name), f"{name}.rpc.{method}")
    nc = await nats.connect(broker_url(), connect_timeout=5, max_reconnect_attempts=0)
    try:
        reply = await nc.request(subject, json.dumps(args).encode(), timeout=5)
    finally:
        await nc.close()
    return json.loads(reply.data)


async def test_a_service_lives_through_the_broker_going_away_and_answers_when_it_returns(cut):
    events: list[str] = []

    class Echo(CliffracerService):
        @rpc
        async def echo(self, value: str) -> str:
            return value

    svc = Echo(
        ServiceConfig(
            name="outage_echo",
            nats_url=cut.url,
            health_port=0,
            reconnect_time_wait=1,
            on_connect=lambda: events.append("connected"),
            on_disconnect=lambda: events.append("disconnected"),
        )
    )
    await svc.start()
    try:
        assert (await _ask("outage_echo", "echo", value="before"))["result"] == "before"
        assert events == ["connected"]

        await cut.sever()
        await wait_until(
            lambda: svc.nc.is_reconnecting,
            within=WITHIN,
            reason="the service to start reconnecting",
        )

        # Alive, not connected, and saying so.
        assert svc._running is True
        assert svc.broker_state is BrokerConnectionState.CONNECTING
        assert svc.is_broker_connected is False
        assert "disconnected" in events

        await cut.restore()
        await wait_until(
            lambda: svc.is_broker_connected, within=WITHIN, reason="the service to reconnect"
        )

        # The subscription was replayed: the same service answers a call it was not asked
        # before the outage, over a link that is new.
        assert (await _ask("outage_echo", "echo", value="after"))["result"] == "after"
        assert events.count("connected") == 2, events
        assert svc._running is True
    finally:
        await svc.stop()


async def test_the_health_endpoint_follows_the_outage_and_the_recovery(cut):
    svc = CliffracerService(
        ServiceConfig(name="outage_health", nats_url=cut.url, health_port=0, reconnect_time_wait=1)
    )
    await svc.start()
    try:
        assert await _health(svc) == (200, "healthy")

        await cut.sever()
        await wait_until(lambda: svc.nc.is_reconnecting, within=WITHIN, reason="reconnecting")
        assert await _health(svc) == (503, "connecting")

        await cut.restore()
        await wait_until(lambda: svc.is_broker_connected, within=WITHIN, reason="reconnected")
        assert await _health(svc) == (200, "healthy")
    finally:
        await svc.stop()


async def _health(svc: CliffracerService) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection(LOOPBACK, svc.health_listener.port)
    writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)["status"]


async def test_a_service_whose_reconnect_budget_is_spent_stops(cut):
    """nats-py gives up, calls `closed_cb`, and the service winds down: the real path.

    The same outage as the test above with a budget of one attempt. Together they show the
    instrument can tell a service that should stay up from one that should stop: the default budget
    outlives the outage, this one does not.
    """
    svc = CliffracerService(
        ServiceConfig(
            name="outage_budget",
            nats_url=cut.url,
            health_port=0,
            reconnect_time_wait=1,
            max_reconnect_attempts=1,
        )
    )
    await svc.start()
    assert svc.config.exit_on_closed is True
    port = svc.health_listener.port
    try:
        await cut.sever()  # and it stays away
        lifecycle = svc.container.lifecycle
        await wait_until(
            lambda: lifecycle.is_stopped,
            within=WITHIN,
            reason="the service to stop once nats-py spent its reconnect budget",
        )

        assert svc.nc.is_closed
        assert svc._running is False
        with pytest.raises(OSError):
            await asyncio.open_connection(LOOPBACK, port)
    finally:
        await svc.stop()


async def test_a_durable_pull_consumer_keeps_delivering_across_the_outage(cut, monkeypatch):
    """The pull loop keeps fetching through the failures while the link is down, and delivers what was
    published to the stream during the gap, then what is published after it.

    The fetch timeout is short and the outage is held until the loop has gone round several times
    inside it, so what is read is the loop's behaviour while the broker is away and not only before
    and after.
    """
    from cliffracer.core.dispatch.jetstream import JetStreamDispatcher

    handled: list[int] = []
    fetches: list[str] = []
    real_pull_once = JetStreamDispatcher.pull_once

    async def counted(self, sub, *, pattern=None):
        try:
            result = await real_pull_once(self, sub, pattern=pattern)
        except BaseException as exc:
            fetches.append(type(exc).__name__)
            raise
        fetches.append("ok")
        return result

    monkeypatch.setattr(JetStreamDispatcher, "pull_once", counted)

    class Worker(CliffracerService):
        @listener("outagejs.work", durable="outage-js", pull=True)
        async def on_work(self, subject: str, n: int = 0) -> None:
            handled.append(n)

    config = ServiceConfig(
        name="outage_js",
        nats_url=cut.url,
        health_port=0,
        reconnect_time_wait=1,
        jetstream_enabled=True,
        jetstream_pull_timeout=0.3,
        jetstream_nak_backoff=0.1,
        jetstream_streams=[
            StreamSpec(name="OUTAGE_JS", subjects=["outagejs.*"]),
            StreamSpec(name="OUTAGE_JS_DLQ", subjects=["dlq.outage_js"]),
        ],
    )
    svc = Worker(config)
    await svc.start()
    direct = await nats.connect(broker_url(), connect_timeout=5)
    subject = HandlerDiscovery.with_namespace(config, "outagejs.work")
    try:
        js = direct.jetstream()
        await js.publish(subject, json.dumps({"n": 1}).encode())
        await wait_until(lambda: handled == [1], within=WITHIN, reason="the first message")

        await cut.sever()
        await wait_until(lambda: svc.nc.is_reconnecting, within=WITHIN, reason="reconnecting")
        # The broker is up; the service's link is not.
        await js.publish(subject, json.dumps({"n": 2}).encode())
        during = len(fetches)
        await wait_until(
            lambda: len(fetches) >= during + 3,
            within=WITHIN,
            reason="the pull loop to go round three more times while the link is down",
        )
        assert handled == [1], "the service handled a message while its link was down"

        await cut.restore()
        await wait_until(lambda: svc.is_broker_connected, within=WITHIN, reason="reconnected")
        await wait_until(
            lambda: 2 in handled, within=WITHIN, reason="the message sent during the gap"
        )

        await js.publish(subject, json.dumps({"n": 3}).encode())
        await wait_until(
            lambda: 3 in handled, within=WITHIN, reason="a message sent after recovery"
        )
        assert handled == [1, 2, 3], handled
    finally:
        await direct.close()
        await svc.stop()

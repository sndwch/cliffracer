"""When `start()` returns, the broker has processed every subscription the service made.

`start()` ends its subscriptions with `nc.flush()`, and a flush is a PING whose PONG says the
broker has processed everything sent before it. nats-py does not keep that promise: it writes the
PING straight to the socket, while a SUB is first put in a pending buffer that a separate task
writes out. Called at once after `subscribe`, `flush()` puts its PING on the wire AHEAD of the SUBs
it is meant to confirm. The broker answers the PING, `start()` returns, and a request from another
connection arrives before the broker has read the SUB: `NoRespondersError` on a service that had
just said it was up. On a loaded host it was measured at about 4% of starts with nats-py alone,
queue group or not.

This test reads the order on the wire, which is what decides it and needs no load: the last PING
the broker saw before `start()` returned must come after every SUB.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

LISTENER_HOST = "127.0.0.1"  # this test's own listener, which is no suite address
INFO = b'INFO {"server_id":"fake","version":"2.10.0","proto":1,"max_payload":1048576}\r\n'


class _RecordingBroker:
    """A TCP server that says just enough NATS to be connected to, and records what it was sent,
    in the order it arrived. `PING` is answered with `PONG`, as a broker does after processing
    everything before it."""

    def __enter__(self) -> _RecordingBroker:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind((LISTENER_HOST, 0))
        self._server.listen(8)
        self._server.settimeout(0.05)
        self.port: int = self._server.getsockname()[1]
        self.commands: list[str] = []
        self.pongs = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._server.close()

    @property
    def url(self) -> str:
        return f"nats://{LISTENER_HOST}:{self.port}"

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.commands)

    def pongs_sent(self) -> int:
        with self._lock:
            return self.pongs

    async def settle(self, wanted: int, seconds: float = 2.0, quiet: float = 0.15) -> list[str]:
        """The stream once `wanted` SUBs have arrived and nothing more has for `quiet` seconds.

        The broker's thread reads on its own schedule and what matters is the ORDER it read them
        in, so this waits for the stream to stop growing, not just for the count it expects: a
        SUB written after `start()` returned would otherwise arrive after this had looked.
        """
        deadline = time.monotonic() + seconds
        last_change = time.monotonic()
        previous: list[str] = []
        while time.monotonic() < deadline:
            seen = self.snapshot()
            if seen != previous:
                previous, last_change = seen, time.monotonic()
            enough = sum(1 for line in seen if line.startswith("SUB ")) >= wanted
            if enough and time.monotonic() - last_change >= quiet:
                return seen
            await asyncio.sleep(0.01)
        return self.snapshot()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, connection: socket.socket) -> None:
        connection.settimeout(0.05)
        buffer = b""
        try:
            connection.sendall(INFO)
            while not self._stop.is_set():
                try:
                    data = connection.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    return
                buffer += data
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    text = line.decode(errors="replace")
                    with self._lock:
                        self.commands.append(text)
                    if text == "PING":
                        with self._lock:
                            self.pongs += 1
                        connection.sendall(b"PONG\r\n")
        except OSError:
            return
        finally:
            connection.close()


class Orders(CliffracerService):
    @rpc
    async def ping(self) -> str:
        return "pong"


async def test_every_subscription_is_on_the_wire_before_the_last_ping_start_waited_for():
    with _RecordingBroker() as broker:
        service = Orders(ServiceConfig(name="orders_svc", nats_url=broker.url, health_port=0))
        await service.start()
        answered = broker.pongs_sent()
        try:
            seen = await broker.settle(wanted=3)
        finally:
            await service.stop()

    subs = [i for i, line in enumerate(seen) if line.startswith("SUB ")]
    pings = [i for i, line in enumerate(seen) if line == "PING"]
    assert len(subs) >= 3, seen
    assert 0 < answered <= len(pings), (answered, seen)
    last_confirmed_ping = pings[answered - 1]
    assert last_confirmed_ping > max(subs), (
        f"the last PING start() waited for (the {answered}th) was written ahead of a SUB, so its "
        f"PONG confirmed nothing about it: {seen}"
    )


async def test_the_service_made_the_subscriptions_the_check_above_reads():
    """The control for the one above: it must be looking at the SUBs a service makes."""
    with _RecordingBroker() as broker:
        service = Orders(ServiceConfig(name="orders_svc", nats_url=broker.url, health_port=0))
        await service.start()
        try:
            seen = await broker.settle(wanted=3)
        finally:
            await service.stop()

    subjects = " ".join(line for line in seen if line.startswith("SUB "))
    for suffix in ("orders_svc.rpc.*", "orders_svc.describe", "orders_svc.async.*"):
        assert suffix in subjects, (suffix, seen)

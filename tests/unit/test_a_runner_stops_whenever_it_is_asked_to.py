"""A runner stops when it is told to, wherever it is: in a backoff, in a start, or cancelled.

A runner has three places to be waiting. In a restart backoff it waits for the shutdown event; in
`service.start()` it waits for a broker; in its steady state it waits for the shutdown event,
looking at the broker's state once a second. Three defects
let it ignore a stop in two of them and leak the service in the third:

* the SIGTERM and SIGINT handlers called `Event.set()`, which does not wake a loop blocked in
  `select()`, so a signal during a backoff took effect when the backoff ended, up to 60 s later.
  The restart-loop tests end the backoff with `_shutdown_event.set()` from inside the loop, which
  does wake it, so none of them could see this. The signal tests here send a real signal to a
  child process from outside its loop;
* the shutdown request never reached a start in progress, so a stop waited out `connect_timeout`;
* cancelling `run()` left the service started and connected.

The broker is a socket that accepts connections and never speaks, so a start against it waits for
the whole connect timeout, and a closed port, which refuses at once.
"""

import asyncio
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

pytestmark = pytest.mark.unit

LOOPBACK = "127.0.0.1"

CHILD = r"""
import sys
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

mode, url, restart_delay, connect_timeout = sys.argv[1:5]


class Svc(CliffracerService):
    def __init__(self):
        super().__init__(
            ServiceConfig(
                name="svc",
                health_port=0,
                nats_url=url,
                connect_timeout=float(connect_timeout),
                restart_delay=float(restart_delay),
                auto_restart=True,
            )
        )


if mode == "runner":
    ServiceRunner(Svc).run_forever()
else:
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Svc)
    orchestrator.run_forever()
"""


class Child:
    """A runner or orchestrator in its own process, with its log lines watched as they arrive."""

    def __init__(self, mode: str, url: str, restart_delay: float, connect_timeout: float) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", CHILD, mode, url, str(restart_delay), str(connect_timeout)],
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.lines.append(line)

    def wait_for_log(self, text: str, within: float = 30.0) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if any(text in line for line in self.lines):
                return
            time.sleep(0.05)
        self.process.kill()
        raise AssertionError(f"never logged {text!r}:\n{''.join(self.lines)}")

    def terminate_and_time(self, ceiling: float) -> float:
        """SIGTERM the child and return how long it took to exit, killing it at `ceiling`."""
        started = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=ceiling)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise AssertionError(
                f"still running {ceiling}s after SIGTERM:\n{''.join(self.lines)}"
            ) from None
        finally:
            self._reader.join(timeout=5)
        return time.monotonic() - started


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


@pytest.fixture
def silent_broker():
    """A listening socket nobody accepts from: connections complete and nothing is ever said."""
    with socket.socket() as listener:
        listener.bind((LOOPBACK, 0))
        listener.listen(8)
        yield f"nats://{LOOPBACK}:{listener.getsockname()[1]}"


@pytest.mark.parametrize("mode", ["runner", "orchestrator"])
def test_a_signal_during_a_restart_backoff_ends_it_at_once(mode: str):
    child = Child(mode, f"nats://{LOOPBACK}:{_closed_port()}", restart_delay=30, connect_timeout=1)
    child.wait_for_log("Restarting service in 30")

    took = child.terminate_and_time(ceiling=10)

    # Upper bound. CI p99 0.0644 s (run 4712: eric-7, CPython 3.12.15, n=40, p99 = max); 78x p99;
    # below 30 s (restart_delay=30).
    assert took < 5, f"exited {took:.1f}s after SIGTERM, with a 30s backoff running"
    assert child.process.returncode == 0, "".join(child.lines)


@pytest.mark.parametrize("mode", ["runner", "orchestrator"])
def test_a_signal_during_a_start_ends_the_start_at_once(mode: str, silent_broker: str):
    child = Child(mode, silent_broker, restart_delay=1, connect_timeout=30)
    child.wait_for_log("Starting service 'svc'")
    time.sleep(0.5)

    took = child.terminate_and_time(ceiling=15)

    # Upper bound. CI p99 0.0648 s (run 4712: eric-7, CPython 3.12.15, n=40, p99 = max); 123x p99;
    # below 30 s (connect_timeout=30).
    assert took < 8, f"exited {took:.1f}s after SIGTERM, with a 30s connect timeout running"
    assert child.process.returncode == 0, "".join(child.lines)


# --- in the loop: stop() during a start, and a cancelled run() ---------------------------------

EVENTS: list[str] = []


class Quiet(CliffracerService):
    """A service that starts and stops without a broker, and says when it does."""

    async def start(self) -> None:
        EVENTS.append("start")

    async def stop(self) -> None:
        await asyncio.sleep(0.05)
        EVENTS.append("stop")


def _config(name: str, **extra: object) -> ServiceConfig:
    return ServiceConfig(
        name=name, health_port=0, health_listener=False, auto_restart=False, **extra
    )


async def _started(host: ServiceOrchestrator | ServiceRunner) -> asyncio.Task[int]:
    task = asyncio.create_task(host.run())
    for _ in range(200):
        if "start" in EVENTS:
            return task
        await asyncio.sleep(0.01)
    task.cancel()
    raise AssertionError("the service did not start")


@pytest.fixture(autouse=True)
def _clear_events():
    EVENTS.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runner", "orchestrator"])
async def test_cancelling_run_stops_the_service_before_the_cancellation_propagates(kind: str):
    if kind == "runner":
        host: ServiceRunner | ServiceOrchestrator = ServiceRunner(Quiet, config=_config("q"))
    else:
        host = ServiceOrchestrator()
        host.add_service(Quiet, config=_config("q"))
    task = await _started(host)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert EVENTS == ["start", "stop"], "run() ended without stopping the service it started"


class NeverStops(Quiet):
    async def stop(self) -> None:
        EVENTS.append("stop requested")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            EVENTS.append("stop cancelled")
            raise


class OwnTimeout(NeverStops):
    """Sets its `shutdown_timeout` in its own config; the runner is given none."""

    def __init__(self) -> None:
        super().__init__(_config("own", shutdown_timeout=0.3))


@pytest.mark.asyncio
async def test_the_stop_after_a_cancel_is_bounded_by_the_services_shutdown_timeout():
    runner = ServiceRunner(NeverStops, config=_config("slow", shutdown_timeout=0.3))
    task = await _started(runner)

    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    assert "stop requested" in EVENTS
    # Upper bound. CI p99 0.301 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 3907x the overshoot.
    assert time.monotonic() - started < 5
    for _ in range(5):
        await asyncio.sleep(0)
    assert "stop cancelled" in EVENTS, "the stop that outlasted its timeout was left running"


@pytest.mark.asyncio
async def test_the_stop_after_a_cancel_is_bounded_by_a_timeout_only_the_service_sets():
    runner = ServiceRunner(OwnTimeout)
    task = await _started(runner)

    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    # Upper bound. CI p99 0.301 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 3275x the overshoot.
    assert time.monotonic() - started < 5


async def _silent_broker_url(connections: list[asyncio.StreamWriter]):
    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        await reader.read()

    server = await asyncio.start_server(accept, LOOPBACK, 0)
    return server, f"nats://{LOOPBACK}:{server.sockets[0].getsockname()[1]}"


class Connecting(CliffracerService):
    """A real service: its start waits on whatever broker the config names."""


def _connecting_config(url: str) -> ServiceConfig:
    return ServiceConfig(
        name="connecting", health_port=0, nats_url=url, connect_timeout=20, auto_restart=False
    )


async def _connecting(kind: str, url: str):
    config = _connecting_config(url)
    if kind == "runner":
        host: ServiceRunner | ServiceOrchestrator = ServiceRunner(Connecting, config=config)
        runner = host
    else:
        host = ServiceOrchestrator()
        host.add_service(Connecting, config=config)
        runner = host.runners[0]
    task = asyncio.create_task(host.run())
    for _ in range(200):
        service = runner.service
        if service is not None and service.container.lifecycle._starting:
            return host, task, runner
        await asyncio.sleep(0.01)
    task.cancel()
    raise AssertionError("the service never began to start")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runner", "orchestrator"])
async def test_a_stop_during_a_start_does_not_wait_out_the_connect_timeout(kind: str):
    connections: list[asyncio.StreamWriter] = []
    server, url = await _silent_broker_url(connections)
    try:
        host, task, runner = await _connecting(kind, url)
        await asyncio.sleep(0.3)

        if isinstance(host, ServiceOrchestrator):
            await asyncio.wait_for(host.stop(), timeout=5)
        else:
            host._running = False
            host._shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

        assert task.result() == 0
        assert runner.service is not None
        assert not runner.service.container.lifecycle.is_running
    finally:
        server.close()
        for writer in connections:
            writer.close()


@pytest.mark.asyncio
async def test_cancelling_run_during_a_start_ends_the_start_and_the_service():
    connections: list[asyncio.StreamWriter] = []
    server, url = await _silent_broker_url(connections)
    try:
        host, task, runner = await _connecting("runner", url)
        await asyncio.sleep(0.3)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        assert runner.service is not None
        assert not runner.service.container.lifecycle.is_running
        assert not runner.service.container.lifecycle._starting
    finally:
        server.close()
        for writer in connections:
            writer.close()

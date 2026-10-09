"""A dial the bound cuts off leaves no socket open: the half-open client is closed at the cut.

The service connection, `ServiceClient`, the metrics pool and the generator each bound the first dial by a wall
clock and cancel `nats.connect` when it runs out. A broker that accepts the TCP connection and then
says nothing is the case that reaches that cancellation inside nats-py, and `nats.connect` built its
client inside the call, so the cut left the client, and the socket it had opened, referenced only by
the cancelled call until a garbage collection happened to find them. Each dial now goes through
`cliffracer.core.dial.connect`, which closes the client it opened when the dial fails.

Measured with no collection forced: before, each cut dial held two descriptors (its own end and the
server's) and one server-side handler task; now none remain once the cut has returned.
"""

import asyncio
import inspect
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cliffracer_metrics import OptimizedNATSConnection
from nats.errors import Error as NatsError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core import dial
from cliffracer.core.exceptions import RpcConnectionError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not os.path.isdir("/proc/self/fd"), reason="counts open descriptors in /proc"
    ),
]

BOUND = 0.2


async def _service_dial(url: str) -> None:
    service = CliffracerService(
        ServiceConfig(name="cut", nats_url=url, connect_timeout=BOUND, health_port=0)
    )
    with pytest.raises(NatsError, match="no answer within connect_timeout"):
        await service.container.connection.connect()


async def _client_dial(url: str) -> None:
    client = ServiceClient(service="cut", nats_url=url, connect_timeout=BOUND, verify=False)
    with pytest.raises(RpcConnectionError, match="did not answer within"):
        await client._connection()


async def _pool_dial(url: str) -> None:
    pool = OptimizedNATSConnection(url, max_connections=1, connect_timeout=BOUND)
    with pytest.raises(NatsError, match="no answer within connect_timeout"):
        await pool.connect()


async def _generator_dial(url: str) -> None:
    from cliffracer.generate_client.cli import fetch_description

    with pytest.raises(TimeoutError):
        await fetch_description(url, "cut", None, BOUND)


DIALS = {
    "service": _service_dial,
    "client": _client_dial,
    "pool": _pool_dial,
    "generator": _generator_dial,
}


@pytest.mark.parametrize("site", DIALS)
async def test_a_cut_dial_leaves_no_descriptor_and_no_task_behind(site):
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # until the dialler closes its end
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    host, port = "127.0.0.1", server.sockets[0].getsockname()[1]
    url = f"nats://{host}:{port}"

    async def settle() -> tuple[int, int]:
        await asyncio.sleep(0.1)  # no collection: whatever is still open is held by a live object
        return len(asyncio.all_tasks()), len(os.listdir("/proc/self/fd"))

    try:
        await DIALS[site](url)  # what the first dial creates once
        tasks_before, fds_before = await settle()
        for _ in range(4):
            await DIALS[site](url)
        tasks_after, fds_after = await settle()
    finally:
        server.close()
        await asyncio.sleep(0.1)

    assert fds_after <= fds_before, (fds_before, fds_after)
    assert tasks_after <= tasks_before, (tasks_before, tasks_after)


async def test_CONTROL_a_dial_that_succeeds_is_not_closed():
    from unittest.mock import patch

    from cliffracer.core import dial

    client = AsyncMock()
    with patch("cliffracer.core.dial.nats.NATS", return_value=client):
        returned = await dial.connect("nats://broker:4222", timeout=1.0, name="x")

    assert returned is client
    client.connect.assert_awaited_once()
    client.close.assert_not_awaited()


async def test_a_refusal_closes_the_client_and_is_not_hidden():
    from unittest.mock import patch

    from cliffracer.core import dial

    client = AsyncMock()
    client.connect.side_effect = NatsError("no servers")
    with patch("cliffracer.core.dial.nats.NATS", return_value=client):
        with pytest.raises(NatsError, match="no servers"):
            await dial.connect("nats://broker:4222", timeout=1.0)

    client.close.assert_awaited_once()


async def test_a_close_that_fails_does_not_replace_the_dials_own_error():
    from unittest.mock import patch

    from cliffracer.core import dial

    client = AsyncMock()
    client.connect.side_effect = NatsError("no servers")
    client.close.side_effect = RuntimeError("close failed")
    with patch("cliffracer.core.dial.nats.NATS", return_value=client):
        with pytest.raises(NatsError, match="no servers"):
            await dial.connect("nats://broker:4222", timeout=1.0)


async def test_a_cancelled_dial_is_cancelled_and_leaves_nothing_behind():
    """A dial the caller cancels, with no bound at all, is cancelled as it was and closes its client.

    Catching `Exception` where the helper catches `BaseException` lets a timeout and a refusal
    through and leaves the cancellation's half-open client open, so this is the case that holds it.
    """
    accepted = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        await reader.read()
        writer.close()

    host = "127.0.0.1"
    server = await asyncio.start_server(silent, host, 0)
    url = f"nats://{host}:{server.sockets[0].getsockname()[1]}"

    async def settle() -> tuple[int, int]:
        await asyncio.sleep(0.1)  # no collection: whatever is still open is held by a live object
        return len(asyncio.all_tasks()), len(os.listdir("/proc/self/fd"))

    async def one_cancelled_dial() -> None:
        accepted.clear()
        dialling = asyncio.get_running_loop().create_task(dial.connect(url, timeout=None))
        await asyncio.wait_for(accepted.wait(), 5)  # the broker has the connection and is silent
        dialling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dialling

    try:
        await one_cancelled_dial()  # what the first dial creates once
        tasks_before, fds_before = await settle()
        for _ in range(4):
            await one_cancelled_dial()
        tasks_after, fds_after = await settle()
    finally:
        server.close()
        await asyncio.sleep(0.1)

    assert fds_after <= fds_before, (fds_before, fds_after)
    assert tasks_after <= tasks_before, (tasks_before, tasks_after)


def _recording_options() -> tuple[dict, list[str]]:
    calls: list[str] = []

    def callback(name: str):
        async def run(*args) -> None:
            calls.append(name)

        return run

    return {name: callback(name) for name in dial.CALLBACKS}, calls


async def test_a_failed_dial_runs_none_of_the_connection_callbacks_for_its_close():
    """The client a failed dial closes never connected, so it reports no disconnect and no close."""
    client = MagicMock()
    given_to_connect: dict = {}
    options, calls = _recording_options()

    async def connect(url, **given) -> None:
        given_to_connect.update(given)
        raise NatsError("no servers")

    async def close() -> None:
        # What nats-py does while it closes a client: it runs the callbacks it was given.
        await given_to_connect["disconnected_cb"]()
        await given_to_connect["closed_cb"]()

    client.connect = connect
    client.close = close
    with patch("cliffracer.core.dial.nats.NATS", return_value=client):
        with pytest.raises(NatsError):
            await dial.connect("nats://broker:4222", timeout=1.0, **options)

    assert calls == [], calls


async def test_CONTROL_the_callbacks_run_for_a_client_that_connected():
    client = MagicMock()
    given_to_connect: dict = {}

    async def connect(url, **given) -> None:
        given_to_connect.update(given)

    client.connect = connect
    options, calls = _recording_options()
    with patch("cliffracer.core.dial.nats.NATS", return_value=client):
        returned = await dial.connect("nats://broker:4222", timeout=1.0, **options)

    await given_to_connect["disconnected_cb"]()
    await given_to_connect["closed_cb"]()
    await given_to_connect["error_cb"](RuntimeError("x"))

    assert returned is client
    assert calls == ["disconnected_cb", "closed_cb", "error_cb"], calls
    assert all(inspect.iscoroutinefunction(given_to_connect[name]) for name in dial.CALLBACKS)


async def test_a_dial_given_no_callbacks_passes_none():
    client = MagicMock()
    seen: dict = {}

    async def connect(url, **given) -> None:
        seen.update(given)

    client.connect = connect
    with patch("cliffracer.core.dial.nats.NATS", return_value=client):
        await dial.connect("nats://broker:4222", timeout=None, name="x")

    assert seen == {"name": "x"}

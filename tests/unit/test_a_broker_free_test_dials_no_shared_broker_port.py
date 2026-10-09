"""A test with no broker of its own cannot dial the shared broker's client or monitoring port.

Three unit tests read `localhost:8222`, the shared broker's monitoring port, because the benchmark
harness reads `/varz` when it can. A read changes nothing on the broker, but the test's result then
depends on whatever is listening on the host. The root conftest refuses and records a connection to
port 4222 or 8222 from a test that does not carry `nats_required` (or the integration or benchmark
marker), and fails the test at teardown.
"""

import socket

import pytest

from conftest import SHARED_BROKER_PORTS

pytestmark = pytest.mark.unit

LOOPBACK = "127.0.0.1"
CLIENT_PORT, MONITOR_PORT = sorted(SHARED_BROKER_PORTS)


@pytest.mark.parametrize("port", sorted(SHARED_BROKER_PORTS))
def test_a_dial_of_a_shared_broker_port_is_refused_and_recorded(refused_broker_port_dials, port):
    with pytest.raises(ConnectionRefusedError, match="shared broker's port"):
        socket.create_connection((LOOPBACK, port), timeout=1)

    assert refused_broker_port_dials == [(LOOPBACK, port)]
    refused_broker_port_dials.clear()  # the refusal was the point of this test


def test_connect_ex_is_guarded_too(refused_broker_port_dials):
    sock = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError):
            sock.connect_ex((LOOPBACK, MONITOR_PORT))
    finally:
        sock.close()

    assert refused_broker_port_dials == [(LOOPBACK, MONITOR_PORT)]
    refused_broker_port_dials.clear()


def test_a_test_that_swallows_the_refusal_is_still_recorded(refused_broker_port_dials):
    """What the teardown reads is the record, not whether the dialler saw the refusal."""
    try:
        socket.create_connection((LOOPBACK, CLIENT_PORT), timeout=1)
    except OSError:
        pass

    assert len(refused_broker_port_dials) == 1
    refused_broker_port_dials.clear()


async def test_CONTROL_another_port_is_not_touched(refused_broker_port_dials):
    import asyncio

    async def serve(reader, writer):
        writer.close()

    server = await asyncio.start_server(serve, LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection(LOOPBACK, port)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()

    assert port not in SHARED_BROKER_PORTS
    assert refused_broker_port_dials == []

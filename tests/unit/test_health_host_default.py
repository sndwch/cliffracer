"""Tests verifying health endpoint binds to loopback by default."""

import asyncio
import json
import socket

import pytest

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


async def _started(**config_kwargs):
    """A service with its NATS half stubbed out, started, listener bound."""
    svc = CliffracerService(ServiceConfig(name="hh", **config_kwargs))

    async def noop(*a, **k):
        return None

    svc.container.connect = noop
    svc.container._setup_subscriptions = noop
    svc.container.disconnect = noop
    await svc.start()
    return svc


def _bound_address(svc) -> tuple[str, int]:
    server = svc.health_listener._server
    assert server is not None, "the listener did not bind"
    host, port = server.sockets[0].getsockname()[:2]
    return host, port


def _a_non_loopback_address() -> str:
    """This host's own routable IPv4 -- what an out-of-container reader uses.

    The UDP socket sends nothing; connect() on a datagram socket only sets the
    peer, and the address it picks is the one the routing table would use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, RFC 5737: routed, never answers
        return s.getsockname()[0]


# --- the default -------------------------------------------------------------


async def test_the_default_binds_loopback():
    svc = await _started()
    try:
        host, _ = _bound_address(svc)
        assert host == "127.0.0.1", f"the default bound {host}"
    finally:
        await svc.stop()


async def test_the_default_still_answers_a_container_style_healthcheck():
    """Verify default health endpoint responds to loopback requests."""
    svc = await _started()
    try:
        _, port = _bound_address(svc)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health HTTP/1.0\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()

        head, _, body = raw.partition(b"\r\n\r\n")
        # Verify endpoint returns status response over loopback.
        assert head.split(b" ")[1] in (b"200", b"503"), head
        assert json.loads(body)["service"] == "hh"
    finally:
        await svc.stop()


# --- the control: an explicit 0.0.0.0 is unchanged ----------------------------


async def test_CONTROL_an_explicit_all_interfaces_bind_still_works():
    """Without this, a listener that had stopped honouring `health_host` at all
    -- and bound loopback whatever it was told -- would pass the two above."""
    svc = await _started(health_host="0.0.0.0")
    try:
        host, _ = _bound_address(svc)
        assert host == "0.0.0.0", f"an explicit 0.0.0.0 bound {host}"
    finally:
        await svc.stop()


async def test_the_default_refuses_a_non_loopback_connect_and_0_0_0_0_accepts_one():
    """The behaviour an out-of-container reader actually sees, both ways.

    One test, because the halves are only meaningful together: a refusal on
    this host's routable address means nothing unless the same connect to the
    same address succeeds when the service asks for every interface.
    """
    outside = _a_non_loopback_address()
    assert not outside.startswith("127."), (
        f"no routable IPv4 on this host (got {outside}); this test needs one, "
        "and reporting a skip here would read as a pass"
    )

    svc = await _started()
    try:
        _, port = _bound_address(svc)
        with pytest.raises((ConnectionRefusedError, OSError)):
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(outside, port), timeout=5
            )
            writer.close()
            await writer.wait_closed()
    finally:
        await svc.stop()

    wide = await _started(health_host="0.0.0.0")
    try:
        _, port = _bound_address(wide)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(outside, port), timeout=5)
        writer.close()
        await writer.wait_closed()
    finally:
        await wide.stop()

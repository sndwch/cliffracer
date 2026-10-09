"""Tests for health listener port binding behavior and contention resolution."""

import asyncio
import errno
import json
import socket

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners import ServiceOrchestrator

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _real_ports(monkeypatch):
    """Disable test port override to verify explicit port contention handling."""
    from cliffracer.core.health_listener import HealthListener

    monkeypatch.setattr(HealthListener, "_test_port_override", None)


def _free_port() -> int:
    """Allocate an unused port dynamically."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health_port(svc, port: int) -> None:
    """Point a constructed service's health listener at `port`.

    `start()` re-reads `config.health_port`, so this is what decides the bind
    -- the value passed to the HealthListener constructor is superseded.
    """
    svc.config.health_port = port


async def _info(port: int) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    _, _, body = raw.partition(b"\r\n\r\n")
    return json.loads(body)


async def _detached(svc):
    async def noop(*a, **k):
        return None

    svc.container.connect = noop
    svc.container._setup_subscriptions = noop
    svc.container.disconnect = noop
    return svc


async def test_a_second_service_on_the_same_health_port_fails_to_start():
    """A taken port is a misconfiguration and says so at startup.

    Moving the second service to a port nobody asked for would leave its
    probes pointing at the first service's listener, so the failure would be
    discovered by a health check that passes while reading the wrong process.
    """
    # The first service takes whatever the OS gives it, and the second is pointed at that: the
    # port is never released between choosing it and binding it, so another process cannot
    # take it in the gap and turn this into a different failure.
    a = await _detached(CliffracerService(ServiceConfig(name="a", health_host="127.0.0.1")))
    b = await _detached(CliffracerService(ServiceConfig(name="b", health_host="127.0.0.1")))
    _health_port(a, 0)

    await a.start()
    try:
        port = a.health_listener.port
        assert port, "the first service binds"
        _health_port(b, port)
        with pytest.raises(OSError) as caught:
            await b.start()
        assert caught.value.errno == errno.EADDRINUSE
        assert b.health_listener.port is None
    finally:
        await a.stop()
        await b.stop()


async def test_health_port_zero_binds_a_real_port_and_info_reports_it():
    """`health_port=0` is how a caller asks for whatever is free.

    The number the OS picked is only useful if it can be read back, so /info
    carries it; asserting against `hl.port` alone would pass on a listener
    that reported a port it never bound.
    """
    svc = await _detached(
        CliffracerService(ServiceConfig(name="z", health_host="127.0.0.1", health_port=0))
    )
    await svc.start()
    try:
        bound = svc.health_listener.port
        assert bound is not None and bound > 0

        info = await _info(bound)
        assert info["name"] == "z"
        assert info["health_port"] == bound, "/info reports the port actually bound"
    finally:
        await svc.stop()


async def test_two_services_asking_for_port_zero_both_bind():
    """The co-located case the fallback used to serve, asked for explicitly."""
    a = await _detached(
        CliffracerService(ServiceConfig(name="z_a", health_host="127.0.0.1", health_port=0))
    )
    b = await _detached(
        CliffracerService(ServiceConfig(name="z_b", health_host="127.0.0.1", health_port=0))
    )
    await a.start()
    try:
        await b.start()
        try:
            assert a.health_listener.port != b.health_listener.port
            assert (await _info(a.health_listener.port))["name"] == "z_a"
            assert (await _info(b.health_listener.port))["name"] == "z_b"
        finally:
            await b.stop()
    finally:
        await a.stop()


async def test_a_port_set_at_construction_also_fails_closed():
    """The same outcome by the other route: `health_port` given to the config
    rather than assigned after construction."""
    c = await _detached(
        CliffracerService(ServiceConfig(name="c", health_host="127.0.0.1", health_port=0))
    )

    await c.start()
    try:
        port = c.health_listener.port
        d = await _detached(
            CliffracerService(ServiceConfig(name="d", health_host="127.0.0.1", health_port=port))
        )
        with pytest.raises(OSError) as caught:
            await d.start()
        # The failure is the bind's, and the listener and the service are left as if it never ran.
        assert caught.value.errno == errno.EADDRINUSE
        assert d.health_listener.port is None
        assert d.health_listener._server is None
        assert d._running is False
    finally:
        await c.stop()


async def test_a_single_service_binds_the_port_its_config_names():
    port = _free_port()
    svc = await _detached(CliffracerService(ServiceConfig(name="s", health_host="127.0.0.1")))
    _health_port(svc, port)
    await svc.start()
    try:
        assert svc.health_listener.port == port
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
    finally:
        await svc.stop()


async def test_two_orchestrated_services_contending_for_one_port_fail_closed():
    """The orchestrator builds its services through a different path, so it
    gets its own case rather than inheriting this file's verdict."""

    class A(CliffracerService):
        def __init__(self):
            super().__init__(ServiceConfig(name="orch-a", health_host="127.0.0.1"))

    class B(CliffracerService):
        def __init__(self):
            super().__init__(ServiceConfig(name="orch-b", health_host="127.0.0.1"))

    orch = ServiceOrchestrator()
    orch.add_service(A)
    orch.add_service(B)
    assert len(orch.runners) == 2

    services = [await _detached(r._construct_service()) for r in orch.runners]
    _health_port(services[0], 0)

    await services[0].start()
    try:
        port = services[0].health_listener.port
        assert port
        _health_port(services[1], port)  # both point at one port, as two defaults would
        with pytest.raises(OSError) as caught:
            await services[1].start()
        assert caught.value.errno == errno.EADDRINUSE
    finally:
        for svc in services:
            await svc.stop()


async def test_an_overridden_health_port_reaches_the_listener():
    """Ensure config overrides applied by ServiceRunner reach the health listener."""
    port = _free_port()

    class A(CliffracerService):
        def __init__(self):
            super().__init__(ServiceConfig(name="ov", health_host="127.0.0.1"))

    orch = ServiceOrchestrator()
    orch.add_service(A, overrides={"health_port": port})
    svc = await _detached(orch.runners[0]._construct_service())

    await svc.start()
    try:
        assert svc.health_listener.port == port, (
            "the overlaid port must be the one bound, not the default"
        )
    finally:
        await svc.stop()

"""Tests for health listener port binding behavior and contention resolution."""

import asyncio
import socket

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners import ServiceOrchestrator


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


def _default_to(svc, port: int) -> None:
    """Configure a port while simulating default configuration semantics."""
    svc.config.health_port = port
    svc.config.model_fields_set.discard("health_port")


async def _detached(svc):
    async def noop(*a, **k):
        return None

    svc.container.connect = noop
    svc.container._setup_subscriptions = noop
    svc.container.disconnect = noop
    return svc


@pytest.mark.unit
async def test_two_default_services_on_same_port_fail_fast_on_second(caplog):
    """Ensure default health port collisions fail fast with an error."""
    port = _free_port()
    a = await _detached(CliffracerService(ServiceConfig(name="a", health_host="127.0.0.1")))
    b = await _detached(CliffracerService(ServiceConfig(name="b", health_host="127.0.0.1")))
    _default_to(a, port)
    _default_to(b, port)

    await a.start()
    try:
        assert a.health_listener.port == port, "the first service binds"
        with pytest.raises(OSError):
            await b.start()
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.unit
async def test_an_explicit_port_still_fails_closed():
    """Ensure explicit health port collisions raise an error."""
    port = _free_port()
    c = await _detached(
        CliffracerService(ServiceConfig(name="c", health_host="127.0.0.1", health_port=port))
    )
    d = await _detached(
        CliffracerService(ServiceConfig(name="d", health_host="127.0.0.1", health_port=port))
    )

    await c.start()
    try:
        with pytest.raises(OSError):
            await d.start()
    finally:
        await c.stop()


@pytest.mark.unit
async def test_a_single_default_service_still_binds():
    """Ensure a single service binds the configured health port."""
    port = _free_port()
    svc = await _detached(
        CliffracerService(ServiceConfig(name="s", health_host="127.0.0.1", health_port=port))
    )
    await svc.start()
    try:
        assert svc.health_listener.port == port
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
    finally:
        await svc.stop()


@pytest.mark.unit
async def test_two_services_through_the_orchestrator_contending_fail_fast():
    """Ensure multiple services orchestrated with colliding ports fail fast."""
    port = _free_port()

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
    for svc in services:
        _default_to(svc, port)  # both inherit one port, as two defaults would

    await services[0].start()
    try:
        with pytest.raises(OSError):
            await services[1].start()
        assert services[0].health_listener.port == port
    finally:
        for svc in services:
            await svc.stop()


@pytest.mark.unit
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

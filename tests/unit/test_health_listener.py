import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


async def _get(port: int, path: str) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, (json.loads(body) if body else {})


async def test_health_and_info_are_served_as_json_on_an_ephemeral_port():
    svc = CliffracerService(ServiceConfig(name="h", health_port=0))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    try:
        status, body = await _get(hl.port, "/health")
        # Unstarted service reports stopped status with 503.
        assert status == 503
        assert body["service"] == "h" and body["status"] == "stopped"
        status, info = await _get(hl.port, "/info")
        assert status == 200 and info["name"] == "h"
        status, _ = await _get(hl.port, "/nope")
        assert status == 404
    finally:
        await hl.stop()


async def test_health_status_code_is_503_when_not_healthy():
    svc = CliffracerService(ServiceConfig(name="h"))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    try:
        status, body = await _get(hl.port, "/health")
        assert status == 503 and body["status"] == "stopped"
    finally:
        await hl.stop()


async def test_the_service_starts_and_stops_the_listener(monkeypatch):
    svc = CliffracerService(ServiceConfig(name="h", health_port=0))

    async def fake_connect():
        class NC:
            is_closed = False
            is_connected = True

        svc.nc = NC()

    monkeypatch.setattr(svc.container, "connect", fake_connect)
    monkeypatch.setattr(svc.container, "_setup_subscriptions", _noop)
    monkeypatch.setattr(svc.container, "disconnect", _noop)
    await svc.start()
    # The number the service was listening on, read BEFORE stop() clears `port`: after it,
    # `open_connection(host, None)` is refused whatever the socket is doing.
    port = svc.health_listener.port
    status, body = await _get(port, "/health")
    assert status == 200 and body["status"] == "healthy"
    await svc.stop()
    with pytest.raises(OSError):
        await _get(port, "/health")
    assert svc.health_listener.port is None


async def test_a_disabled_listener_does_not_bind(monkeypatch):
    svc = CliffracerService(ServiceConfig(name="h", health_listener=False))
    monkeypatch.setattr(svc.container, "connect", _noop)
    monkeypatch.setattr(svc.container, "_setup_subscriptions", _noop)
    monkeypatch.setattr(svc.container, "disconnect", _noop)
    await svc.start()
    assert svc.health_listener.port is None
    await svc.stop()


async def _noop(*a, **k):
    return None


async def test_a_listener_whose_configured_port_is_taken_raises(monkeypatch):
    """The port that decides is `config.health_port`, which start() re-reads.

    Passing the occupied port to the constructor alone proves nothing: start()
    would bind whatever the config names instead, so a listener that never
    contended the dummy server at all would still satisfy assertions about
    `hl.port`.
    """
    import errno

    monkeypatch.setattr(HealthListener, "_test_port_override", None)
    dummy_server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    occupied_port = dummy_server.sockets[0].getsockname()[1]
    try:
        svc = CliffracerService(ServiceConfig(name="hl_taken", health_port=occupied_port))
        hl = HealthListener(svc, "127.0.0.1", occupied_port)
        with pytest.raises(OSError) as exc_info:
            await hl.start()
        assert exc_info.value.errno == errno.EADDRINUSE
        assert hl.port is None
    finally:
        dummy_server.close()
        await dummy_server.wait_closed()


async def test_a_listener_asked_for_port_zero_binds_something_free(monkeypatch):
    """`health_port=0` is the opt-in for "any free port"; it must not raise
    even while the port the constructor was handed is occupied."""
    monkeypatch.setattr(HealthListener, "_test_port_override", None)
    dummy_server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    occupied_port = dummy_server.sockets[0].getsockname()[1]
    try:
        svc = CliffracerService(ServiceConfig(name="hl_zero", health_port=0))
        hl = HealthListener(svc, "127.0.0.1", occupied_port)
        await hl.start()
        try:
            assert hl.port is not None and hl.port > 0
            assert hl.port != occupied_port
            status, info = await _get(hl.port, "/info")
            assert status == 200
            assert info["health_port"] == hl.port
        finally:
            await hl.stop()
    finally:
        dummy_server.close()
        await dummy_server.wait_closed()


async def test_health_listener_port_collision_fails_fast(monkeypatch):
    """A taken port raises rather than moving the listener somewhere else."""
    import errno

    monkeypatch.setattr(HealthListener, "_test_port_override", None)
    dummy_server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    occupied_port = dummy_server.sockets[0].getsockname()[1]
    try:
        svc = CliffracerService(ServiceConfig(name="hl_explicit", health_port=occupied_port))
        hl = HealthListener(svc, "127.0.0.1", occupied_port)
        with pytest.raises(OSError) as exc_info:
            await hl.start()
        assert exc_info.value.errno == errno.EADDRINUSE
        assert hl.port is None
    finally:
        dummy_server.close()
        await dummy_server.wait_closed()

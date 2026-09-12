import asyncio
import importlib.util
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit

# Ensure container module is available before running tests that patch container methods.
_HAS_CONTAINER = importlib.util.find_spec("cliffracer.core.container") is not None
_needs_container = pytest.mark.skipif(
    not _HAS_CONTAINER, reason="cliffracer.core.container module required"
)


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


@_needs_container
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
    status, body = await _get(svc.health_listener.port, "/health")
    assert status == 200 and body["status"] == "healthy"
    await svc.stop()
    with pytest.raises(OSError):
        await _get(svc.health_listener.port, "/health")


@_needs_container
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

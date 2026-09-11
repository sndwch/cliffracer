"""Integration tests verifying health endpoint reports disconnected status on broker disconnection."""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


async def _get(port: int, path: str) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), (json.loads(body) if body else {})


async def test_health_reports_a_closed_connection_as_503_disconnected():
    svc = CliffracerService(ServiceConfig(name="probe_target", exit_on_closed=False, health_port=0))
    await svc.start()
    try:
        status, body = await _get(svc.health_listener.port, "/health")
        assert status == 200, body
        assert body["status"] == "healthy" and body["nats_connected"] is True, body

        # The falsification: take the broker away from underneath it.
        await svc.nc.close()

        status, body = await _get(svc.health_listener.port, "/health")
        assert status == 503, body
        assert body["status"] == "disconnected", body
        assert body["nats_connected"] is False, body
    finally:
        await svc.stop()


async def test_health_reports_reconnecting_broker_as_503_disconnected():
    """When a broker connection drops and enters reconnection,
    /health must return HTTP 503 with status: connecting and nats_connected: False.
    Subsequent stop() must cleanly close without raising."""
    svc = CliffracerService(
        ServiceConfig(name="reconnect_target", exit_on_closed=False, health_port=0)
    )
    await svc.start()
    try:
        status, body = await _get(svc.health_listener.port, "/health")
        assert status == 200, body
        assert body["status"] == "healthy" and body["nats_connected"] is True, body

        # Simulate broken transport
        svc.nc._transport.close()
        await asyncio.sleep(0.1)

        assert svc.nc.is_connected is False
        assert svc.nc.is_reconnecting is True

        # Probe must return 503 and nats_connected: False
        status, body = await _get(svc.health_listener.port, "/health")
        assert status == 503, body
        assert body["status"] == "connecting", body
        assert body["nats_connected"] is False, body
    finally:
        await svc.stop()

"""Integration tests verifying the health endpoint on a closed and on a reconnecting broker connection."""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.testing.waiting import wait_until

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

        # Liveness is not readiness: a dead broker pulls the pod out of the load balancer, it
        # must not make the orchestrator restart it.
        status, body = await _get(svc.health_listener.port, "/live")
        assert status == 200 and body["status"] == "healthy", (status, body)
    finally:
        await svc.stop()


async def test_health_reports_reconnecting_broker_as_503_connecting():
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

        # Simulate broken transport. nats-py offers no public way to drop the socket under a live
        # client, so this reaches for its private `_transport`: if that is renamed the test says so
        # here, not as an AttributeError further down.
        transport = getattr(svc.nc, "_transport", None)
        assert transport is not None, (
            "nats-py no longer exposes `_transport`; break the socket another way"
        )
        transport.close()
        await wait_until(
            lambda: svc.nc.is_reconnecting,
            within=10.0,
            reason="the client to notice the dead transport and start reconnecting",
        )

        assert svc.nc.is_connected is False

        # Probe must return 503 and nats_connected: False
        status, body = await _get(svc.health_listener.port, "/health")
        assert status == 503, body
        assert body["status"] == "connecting", body
        assert body["nats_connected"] is False, body

        status, body = await _get(svc.health_listener.port, "/live")
        assert status == 200 and body["status"] == "healthy", (status, body)
    finally:
        await svc.stop()


async def test_the_default_exit_on_closed_stops_the_service_when_the_connection_closes():
    """The default (`exit_on_closed=True`) path against a real nats-py connection.

    Both tests above turn the flag off, and the only coverage of the default was a hand-rolled
    fake calling the callback directly, so nothing showed that a real closed connection reaches
    the callback and that the service then stops (and its health listener goes with it).
    """
    svc = CliffracerService(ServiceConfig(name="closing_target", health_port=0))
    assert svc.config.exit_on_closed is True
    await svc.start()
    port = svc.health_listener.port
    try:
        status, body = await _get(port, "/health")
        assert status == 200, body

        await svc.nc.close()

        lifecycle = svc.container.lifecycle
        for _ in range(100):
            if lifecycle.is_stopped:
                break
            await asyncio.sleep(0.05)
        assert lifecycle.is_stopped and not lifecycle.is_running, "the service did not stop"
        with pytest.raises(OSError):
            await _get(port, "/health")  # the health listener went with it
    finally:
        await svc.stop()

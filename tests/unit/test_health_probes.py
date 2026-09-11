"""Unit tests verifying Kubernetes probe separation (/live vs /ready vs /health).

Verifies the operational invariant that /live (liveness probe) only checks process
liveness and never fails on downstream broker or dependency outages, preventing
Kubernetes restart storms, while /ready (readiness probe) and /health accurately
report 503 when broker or dependencies are degraded.
"""

from __future__ import annotations

import asyncio
import errno
import json
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener


async def _request(port: int, path: str, method: str = "GET") -> tuple[int, dict[str, Any]]:
    """Execute raw HTTP request against HealthListener over loopback TCP socket."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, (json.loads(body) if body else {})


def _simulate_running_service(svc: CliffracerService, *, broker_connected: bool = True) -> None:
    """Set up service state simulating running lifecycle without live NATS daemon."""
    svc._running = True
    if broker_connected:
        svc.nc = type(
            "NC",
            (),
            {
                "is_closed": False,
                "is_connected": True,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    else:
        svc.nc = type(
            "NC",
            (),
            {
                "is_closed": True,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": False,
            },
        )()


@pytest.mark.unit
async def test_live_probe_succeeds_when_dependencies_fail_while_ready_returns_503():
    """Verify /live remains 200 OK during dependency outages while /ready returns 503."""
    svc = CliffracerService(ServiceConfig(name="orders_svc", health_port=0))
    _simulate_running_service(svc, broker_connected=True)

    # Register a failing external dependency (e.g. database down)
    async def failing_db_probe() -> None:
        raise ConnectionRefusedError("Database connection refused on 127.0.0.1:5432")

    svc.add_dependency("postgres", failing_db_probe)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # /live must return 200 healthy: process is running and loop is responsive
        live_status, live_body = await _request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"
        assert live_body["service"] == "orders_svc"
        assert "dependencies" not in live_body

        # /ready must return 503 unhealthy: cannot accept traffic with broken dependencies
        ready_status, ready_body = await _request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"
        assert ready_body["service"] == "orders_svc"
        assert "postgres" in ready_body.get("unhealthy_dependencies", [])

        # /health is backward-compatible alias to /ready
        health_status, health_body = await _request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "unhealthy"
        assert health_body["service"] == ready_body["service"]
        assert health_body["unhealthy_dependencies"] == ready_body["unhealthy_dependencies"]
    finally:
        await hl.stop()


@pytest.mark.unit
async def test_live_probe_succeeds_when_dependency_raises_exception():
    """Verify /live remains 200 OK even if dependency probe raises an unhandled error."""
    svc = CliffracerService(ServiceConfig(name="payments_svc", health_port=0))
    _simulate_running_service(svc, broker_connected=True)

    async def explosive_probe() -> None:
        raise ConnectionResetError("Connection refused by downstream")

    svc.add_dependency("payment_gateway", explosive_probe)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        live_status, live_body = await _request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, ready_body = await _request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"
        assert "payment_gateway" in ready_body.get("unhealthy_dependencies", [])
    finally:
        await hl.stop()


@pytest.mark.unit
async def test_live_probe_succeeds_when_broker_is_disconnected():
    """Verify /live returns 200 when broker drops, preventing pod restart storms."""
    svc = CliffracerService(ServiceConfig(name="broker_test_svc", health_port=0))
    _simulate_running_service(svc, broker_connected=False)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # Liveness probe does not restart pod while NATS reconnect dial is active
        live_status, live_body = await _request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        # Readiness probe reports 503 disconnected
        ready_status, ready_body = await _request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "disconnected"

        health_status, health_body = await _request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "disconnected"
    finally:
        await hl.stop()


@pytest.mark.unit
async def test_live_probe_succeeds_when_broker_is_connecting():
    """Verify /live returns 200 when broker is connecting, while /ready returns 503."""
    svc = CliffracerService(ServiceConfig(name="connecting_svc", health_port=0))
    svc._running = True
    svc.nc = type(
        "NC",
        (),
        {"is_closed": False, "is_connected": False, "is_draining": False, "is_connecting": True},
    )()

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        live_status, live_body = await _request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, ready_body = await _request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "connecting"

        health_status, health_body = await _request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "connecting"
    finally:
        await hl.stop()


@pytest.mark.unit
async def test_all_probes_return_503_when_service_is_stopped():
    """Verify /live, /ready, and /health return 503 when service is stopped."""
    svc = CliffracerService(ServiceConfig(name="stopped_svc", health_port=0))
    svc._running = False

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        live_status, live_body = await _request(hl.port, "/live")
        assert live_status == 503
        assert live_body["status"] == "stopped"

        ready_status, ready_body = await _request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "stopped"

        health_status, health_body = await _request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "stopped"
    finally:
        await hl.stop()


@pytest.mark.unit
async def test_all_probes_return_200_when_fully_healthy():
    """Verify /live, /ready, and /health return 200 when all systems pass."""
    svc = CliffracerService(ServiceConfig(name="healthy_svc", health_port=0))
    _simulate_running_service(svc, broker_connected=True)

    async def passing_probe() -> bool:
        return True

    svc.add_dependency("cache", passing_probe)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        live_status, live_body = await _request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, ready_body = await _request(hl.port, "/ready")
        assert ready_status == 200
        assert ready_body["status"] == "healthy"

        health_status, health_body = await _request(hl.port, "/health")
        assert health_status == 200
        assert health_body["status"] == "healthy"
        assert health_body["service"] == ready_body["service"]
        assert health_body["dependencies"] == ready_body["dependencies"]
    finally:
        await hl.stop()


@pytest.mark.unit
async def test_health_listener_method_not_allowed_and_not_found():
    """Verify HTTP method and path routing enforcement."""
    svc = CliffracerService(ServiceConfig(name="routing_svc", health_port=0))
    _simulate_running_service(svc, broker_connected=True)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # POST to /live is not allowed
        post_status, post_body = await _request(hl.port, "/live", method="POST")
        assert post_status == 405
        assert post_body == {"error": "method not allowed"}

        # Unknown route gives 404
        not_found_status, not_found_body = await _request(hl.port, "/unknown")
        assert not_found_status == 404
        assert not_found_body == {"error": "not found"}

        # /info gives 200
        info_status, info_body = await _request(hl.port, "/info")
        assert info_status == 200
        assert info_body["name"] == "routing_svc"
    finally:
        await hl.stop()


@pytest.mark.unit
def test_direct_service_liveness_helpers():
    """Verify CliffracerService.liveness_check() and is_live() directly."""
    svc = CliffracerService(ServiceConfig(name="direct_svc"))
    # Before running: status is stopped
    stopped_check = svc.liveness_check()
    assert stopped_check["status"] == "stopped"
    assert stopped_check["service"] == "direct_svc"
    assert "timestamp" in stopped_check

    # When running: status is healthy
    svc._running = True
    running_check = svc.liveness_check()
    assert running_check["status"] == "healthy"
    assert running_check["service"] == "direct_svc"
    assert "timestamp" in running_check

    # Alias check
    alias_check = svc.is_live()
    assert alias_check["status"] == "healthy"
    assert alias_check["service"] == "direct_svc"
    assert "timestamp" in alias_check


@pytest.mark.unit
async def test_health_listener_port_conflict_fails_fast(monkeypatch):
    """Verify attempting to start two HealthListeners on the same port raises OSError."""
    monkeypatch.setattr(HealthListener, "_test_port_override", None)
    svc1 = CliffracerService(ServiceConfig(name="first_svc", health_port=0))
    _simulate_running_service(svc1)
    hl1 = HealthListener(svc1, "127.0.0.1", 0)
    await hl1.start()
    assert hl1.port is not None

    svc2 = CliffracerService(ServiceConfig(name="second_svc", health_port=hl1.port))
    _simulate_running_service(svc2)
    # Test collision even when port_is_explicit=False (default inheritance)
    hl2 = HealthListener(svc2, "127.0.0.1", hl1.port, port_is_explicit=False)
    try:
        with pytest.raises(OSError) as exc_info:
            await hl2.start()
        assert exc_info.value.errno == errno.EADDRINUSE
        assert hl2.port is None
        assert hl2._server is None
    finally:
        await hl1.stop()

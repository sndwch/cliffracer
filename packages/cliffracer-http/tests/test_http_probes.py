"""Tests for HTTP extension Kubernetes health probes (/live, /ready, /health).

Verifies the probe segregation on FastAPI applications mounted via HttpExtension
and AutoGatewayExtension, ensuring /live isolates process liveness from external
outages, while /ready and /health enforce readiness contracts.
"""

from __future__ import annotations

import pytest
from cliffracer_http import AutoGatewayExtension, HttpExtension
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import ExtensionSetupContext

pytestmark = pytest.mark.unit


class PingResponse(BaseModel):
    pong: bool


class ProbeTestService(CliffracerService):
    http = HttpExtension(port=0)

    @rpc
    async def ping(self) -> PingResponse:
        return PingResponse(pong=True)


async def _setup_http_service(svc: ProbeTestService) -> TestClient:
    ctx = ExtensionSetupContext(
        service_config=svc.config,
        broker_url="nats://localhost:4222",
        service=svc,
    )
    await svc.http.setup(ctx)
    return TestClient(svc.http.app)


def _mark_connected(svc: CliffracerService) -> None:
    svc.container.lifecycle._running = True
    svc.nc = type(
        "NC",
        (),
        {"is_closed": False, "is_connected": True, "is_draining": False, "is_connecting": False},
    )()


def _mark_disconnected(svc: CliffracerService) -> None:
    svc.container.lifecycle._running = True
    svc.nc = type(
        "NC",
        (),
        {"is_closed": True, "is_connected": False, "is_draining": False, "is_connecting": False},
    )()


def _mark_connecting(svc: CliffracerService) -> None:
    svc.container.lifecycle._running = True
    svc.nc = type(
        "NC",
        (),
        {"is_closed": False, "is_connected": False, "is_draining": False, "is_connecting": True},
    )()


@pytest.mark.asyncio
async def test_http_extension_live_probe_succeeds_when_dependencies_fail():
    """Verify HttpExtension /live returns 200 during dependency failure while /ready returns 503."""
    svc = ProbeTestService(ServiceConfig(name="http_probe_svc"))
    _mark_connected(svc)

    async def broken_db():
        raise ConnectionRefusedError("DB connection refused")

    svc.add_dependency("postgres", broken_db)

    client = await _setup_http_service(svc)

    # /live probe must be 200 healthy
    live_resp = client.get("/live")
    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "healthy"
    assert live_resp.json()["service"] == "http_probe_svc"

    # /ready probe must be 503 unhealthy
    ready_resp = client.get("/ready")
    assert ready_resp.status_code == 503
    assert ready_resp.json()["status"] == "unhealthy"
    assert "postgres" in ready_resp.json().get("unhealthy_dependencies", [])

    # /health is backward-compatible alias to /ready
    health_resp = client.get("/health")
    assert health_resp.status_code == 503
    assert health_resp.json()["status"] == "unhealthy"
    assert (
        health_resp.json()["unhealthy_dependencies"] == ready_resp.json()["unhealthy_dependencies"]
    )


@pytest.mark.asyncio
async def test_http_extension_live_probe_succeeds_when_broker_disconnected():
    """Verify /live remains 200 when broker is disconnected, while /ready and /health return 503."""
    svc = ProbeTestService(ServiceConfig(name="http_probe_svc"))
    _mark_disconnected(svc)

    client = await _setup_http_service(svc)

    # /live returns 200
    live_resp = client.get("/live")
    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "healthy"

    # /ready and /health return 503 disconnected
    ready_resp = client.get("/ready")
    assert ready_resp.status_code == 503
    assert ready_resp.json()["status"] == "disconnected"

    health_resp = client.get("/health")
    assert health_resp.status_code == 503
    assert health_resp.json()["status"] == "disconnected"


@pytest.mark.asyncio
async def test_http_extension_live_probe_succeeds_when_broker_connecting():
    """Verify /live returns 200 when broker is connecting, while /ready and /health return 503."""
    svc = ProbeTestService(ServiceConfig(name="http_probe_svc"))
    _mark_connecting(svc)

    client = await _setup_http_service(svc)

    live_resp = client.get("/live")
    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "healthy"

    ready_resp = client.get("/ready")
    assert ready_resp.status_code == 503
    assert ready_resp.json()["status"] == "connecting"

    health_resp = client.get("/health")
    assert health_resp.status_code == 503
    assert health_resp.json()["status"] == "connecting"


@pytest.mark.asyncio
async def test_http_extension_probes_return_503_when_service_stopped():
    """Verify all probes return 503 when service is stopped."""
    svc = ProbeTestService(ServiceConfig(name="http_probe_svc"))
    svc.container.lifecycle._running = False

    client = await _setup_http_service(svc)

    assert client.get("/live").status_code == 503
    assert client.get("/live").json()["status"] == "stopped"

    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json()["status"] == "stopped"

    assert client.get("/health").status_code == 503
    assert client.get("/health").json()["status"] == "stopped"


@pytest.mark.asyncio
async def test_http_extension_probes_return_200_when_fully_healthy():
    """Verify all probes return 200 when service is running and healthy."""
    svc = ProbeTestService(ServiceConfig(name="http_probe_svc"))
    _mark_connected(svc)

    async def passing_cache():
        return True

    svc.add_dependency("redis", passing_cache)

    client = await _setup_http_service(svc)

    assert client.get("/live").status_code == 200
    assert client.get("/live").json()["status"] == "healthy"

    assert client.get("/ready").status_code == 200
    assert client.get("/ready").json()["status"] == "healthy"

    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["status"] == "healthy"

    # /info endpoint
    info_resp = client.get("/info")
    assert info_resp.status_code == 200
    assert info_resp.json()["name"] == "http_probe_svc"


@pytest.mark.asyncio
async def test_standalone_auto_gateway_extension_serves_probes():
    """Verify standalone AutoGatewayExtension mounts and serves /live, /ready, and /health."""

    class StandaloneGatewayService(CliffracerService):
        gateway = AutoGatewayExtension(prefix="/api")

        @rpc
        async def hello(self) -> dict[str, str]:
            return {"greeting": "world"}

    config = ServiceConfig(name="standalone_gw_svc")
    svc = StandaloneGatewayService(config)
    _mark_connected(svc)

    ctx = ExtensionSetupContext(
        service_config=config,
        broker_url="nats://localhost:4222",
        service=svc,
    )
    await svc.gateway.setup(ctx)
    assert svc.gateway.app is not None

    client = TestClient(svc.gateway.app)

    # Verify /live
    live_resp = client.get("/live")
    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "healthy"
    assert live_resp.json()["service"] == "standalone_gw_svc"

    # Verify /ready
    ready_resp = client.get("/ready")
    assert ready_resp.status_code == 200
    assert ready_resp.json()["status"] == "healthy"

    # Verify /health
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "healthy"

    # Verify /info
    info_resp = client.get("/info")
    assert info_resp.status_code == 200
    assert info_resp.json()["name"] == "standalone_gw_svc"


@pytest.mark.asyncio
async def test_standalone_auto_gateway_extension_probe_isolation():
    """Verify standalone AutoGatewayExtension /live returns 200 while /ready returns 503 during outage."""

    class StandaloneDegradedService(CliffracerService):
        gateway = AutoGatewayExtension(prefix="/api")

    config = ServiceConfig(name="standalone_degraded_svc")
    svc = StandaloneDegradedService(config)
    _mark_connected(svc)

    async def dead_db():
        raise ConnectionError("Cannot connect to SQL backend")

    svc.add_dependency("sql", dead_db)

    ctx = ExtensionSetupContext(
        service_config=config,
        broker_url="nats://localhost:4222",
        service=svc,
    )
    await svc.gateway.setup(ctx)
    client = TestClient(svc.gateway.app)

    # /live remains 200
    live_resp = client.get("/live")
    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "healthy"

    # /ready and /health return 503
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json()["status"] == "unhealthy"
    assert "sql" in client.get("/ready").json().get("unhealthy_dependencies", [])

    assert client.get("/health").status_code == 503
    assert client.get("/health").json()["status"] == "unhealthy"

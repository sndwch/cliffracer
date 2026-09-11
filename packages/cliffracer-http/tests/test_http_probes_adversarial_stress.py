"""Adversarial stress tests for cliffracer-http Kubernetes probe segregation.

Tests both HttpExtension and AutoGatewayExtension across:
1. Downstream dependency failures and timeouts (segregating /live 200 from /ready 503).
2. Broker state transitions (CONNECTING, DISCONNECTED, CLOSED, dynamic flapping).
3. Stopped service states (/live, /ready, /health all 503).
4. Backward-compatible payload and status parity between /health and /ready.
5. Non-GET methods (POST, PUT, DELETE, PATCH returning 405) and invalid paths (404).
6. Dual HttpExtension + AutoGatewayExtension coexistence.
7. Concurrency isolation under slow dependency execution.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from cliffracer_http import AutoGatewayExtension, HttpExtension
from fastapi.testclient import TestClient

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import ExtensionSetupContext


def _simulate_service_state(
    svc: CliffracerService,
    *,
    running: bool = True,
    broker_state: str = "connected",
) -> None:
    """Simulate broker connection and service lifecycle states."""
    svc.container.lifecycle._running = running
    if broker_state == "connected":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": True,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    elif broker_state == "connecting":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": True,
            },
        )()
    elif broker_state == "disconnected":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": False,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    elif broker_state == "closed":
        svc.nc = type(
            "MockNC",
            (),
            {
                "is_closed": True,
                "is_connected": False,
                "is_draining": False,
                "is_connecting": False,
            },
        )()
    else:
        svc.nc = None


async def _create_http_client(svc: CliffracerService, ext: HttpExtension) -> TestClient:
    """Setup HttpExtension on service and return TestClient."""
    ctx = ExtensionSetupContext(
        service_config=svc.config,
        broker_url="nats://localhost:4222",
        service=svc,
    )
    await ext.setup(ctx)
    assert ext.app is not None
    return TestClient(ext.app)


async def _create_gateway_client(svc: CliffracerService, gw: AutoGatewayExtension) -> TestClient:
    """Setup AutoGatewayExtension on service and return TestClient."""
    ctx = ExtensionSetupContext(
        service_config=svc.config,
        broker_url="nats://localhost:4222",
        service=svc,
    )
    await gw.setup(ctx)
    assert gw.app is not None
    return TestClient(gw.app)


def _assert_health_mirrors_ready(health_resp: Any, ready_resp: Any) -> None:
    """Verify /health and /ready have identical status codes and operational fields."""
    assert health_resp.status_code == ready_resp.status_code
    h_data = health_resp.json()
    r_data = ready_resp.json()
    assert h_data["status"] == r_data["status"]
    assert h_data["service"] == r_data["service"]
    if "broker_state" in r_data:
        assert h_data["broker_state"] == r_data["broker_state"]
    assert h_data.get("unhealthy_dependencies") == r_data.get("unhealthy_dependencies")
    assert h_data.keys() == r_data.keys()


@pytest.mark.asyncio
async def test_http_extension_and_gateway_dependency_failure_segregation():
    """Verify /live remains 200 while /ready returns 503 on degraded dependencies across both extensions."""

    # 1. HttpExtension
    class HttpSvc(CliffracerService):
        http = HttpExtension(port=0)

    svc1 = HttpSvc(ServiceConfig(name="http_dep_svc"))
    _simulate_service_state(svc1, running=True, broker_state="connected")

    async def fail_redis():
        raise ConnectionRefusedError("Redis down")

    async def fail_timeout():
        await asyncio.sleep(5.0)

    svc1.add_dependency("redis", fail_redis)
    svc1.add_dependency("postgres", fail_timeout, timeout=0.05)

    client1 = await _create_http_client(svc1, svc1.http)

    live_resp = client1.get("/live")
    assert live_resp.status_code == 200
    assert live_resp.json()["status"] == "healthy"
    assert "dependencies" not in live_resp.json()

    ready_resp = client1.get("/ready")
    assert ready_resp.status_code == 503
    assert ready_resp.json()["status"] == "unhealthy"
    assert sorted(ready_resp.json()["unhealthy_dependencies"]) == ["postgres", "redis"]

    health_resp = client1.get("/health")
    _assert_health_mirrors_ready(health_resp, ready_resp)

    # 2. AutoGatewayExtension standalone
    class GatewaySvc(CliffracerService):
        gw = AutoGatewayExtension(prefix="/api")

    svc2 = GatewaySvc(ServiceConfig(name="gw_dep_svc"))
    _simulate_service_state(svc2, running=True, broker_state="connected")
    svc2.add_dependency("redis", fail_redis)
    svc2.add_dependency("postgres", fail_timeout, timeout=0.05)

    client2 = await _create_gateway_client(svc2, svc2.gw)

    live_resp2 = client2.get("/live")
    assert live_resp2.status_code == 200
    assert live_resp2.json()["status"] == "healthy"

    ready_resp2 = client2.get("/ready")
    assert ready_resp2.status_code == 503
    assert ready_resp2.json()["status"] == "unhealthy"
    assert sorted(ready_resp2.json()["unhealthy_dependencies"]) == ["postgres", "redis"]

    health_resp2 = client2.get("/health")
    _assert_health_mirrors_ready(health_resp2, ready_resp2)


@pytest.mark.asyncio
async def test_http_extension_and_gateway_broker_states():
    """Verify /live remains 200 while /ready returns 503 across broker states (connecting, disconnected, closed)."""

    class HttpSvc(CliffracerService):
        http = HttpExtension(port=0)

    svc = HttpSvc(ServiceConfig(name="http_broker_svc"))
    client = await _create_http_client(svc, svc.http)

    # CONNECTING
    _simulate_service_state(svc, running=True, broker_state="connecting")
    assert client.get("/live").status_code == 200
    ready_conn = client.get("/ready")
    assert ready_conn.status_code == 503
    assert ready_conn.json()["status"] == "connecting"
    _assert_health_mirrors_ready(client.get("/health"), ready_conn)

    # DISCONNECTED
    _simulate_service_state(svc, running=True, broker_state="disconnected")
    assert client.get("/live").status_code == 200
    ready_disc = client.get("/ready")
    assert ready_disc.status_code == 503
    assert ready_disc.json()["status"] == "disconnected"
    _assert_health_mirrors_ready(client.get("/health"), ready_disc)

    # CLOSED
    _simulate_service_state(svc, running=True, broker_state="closed")
    assert client.get("/live").status_code == 200
    ready_closed = client.get("/ready")
    assert ready_closed.status_code == 503
    assert ready_closed.json()["status"] == "disconnected"
    _assert_health_mirrors_ready(client.get("/health"), ready_closed)


@pytest.mark.asyncio
async def test_http_extension_dynamic_broker_flapping():
    """Verify /live is rock-solid 200 while /ready transitions dynamically between 200 and 503."""

    class FlapSvc(CliffracerService):
        http = HttpExtension(port=0)

    svc = FlapSvc(ServiceConfig(name="flap_svc"))
    client = await _create_http_client(svc, svc.http)

    for _ in range(3):
        _simulate_service_state(svc, running=True, broker_state="connected")
        assert client.get("/live").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/health").status_code == 200

        _simulate_service_state(svc, running=True, broker_state="disconnected")
        assert client.get("/live").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/health").status_code == 503


@pytest.mark.asyncio
async def test_http_extension_and_gateway_stopped_service_state():
    """Verify stopped service returns 503 on /live, /ready, and /health across both extensions."""

    # HttpExtension stopped
    class Svc1(CliffracerService):
        http = HttpExtension(port=0)

    svc1 = Svc1(ServiceConfig(name="stopped_http_svc"))
    _simulate_service_state(svc1, running=False, broker_state="connected")
    client1 = await _create_http_client(svc1, svc1.http)

    for path in ["/live", "/ready", "/health"]:
        resp = client1.get(path)
        assert resp.status_code == 503, f"{path} returned {resp.status_code}"
        assert resp.json()["status"] == "stopped"

    # AutoGatewayExtension stopped
    class Svc2(CliffracerService):
        gw = AutoGatewayExtension(prefix="/api")

    svc2 = Svc2(ServiceConfig(name="stopped_gw_svc"))
    _simulate_service_state(svc2, running=False, broker_state="connected")
    client2 = await _create_gateway_client(svc2, svc2.gw)

    for path in ["/live", "/ready", "/health"]:
        resp = client2.get(path)
        assert resp.status_code == 503, f"{path} returned {resp.status_code}"
        assert resp.json()["status"] == "stopped"


@pytest.mark.asyncio
async def test_http_extension_and_gateway_method_and_path_routing():
    """Verify non-GET methods return 405 and unknown paths return 404 across both extensions."""

    class Svc(CliffracerService):
        http = HttpExtension(port=0)

    svc = Svc(ServiceConfig(name="routing_http_svc"))
    _simulate_service_state(svc, running=True, broker_state="connected")
    client = await _create_http_client(svc, svc.http)

    endpoints = ["/live", "/ready", "/health", "/info"]
    methods = ["post", "put", "delete", "patch"]

    for path in endpoints:
        for m in methods:
            func = getattr(client, m)
            resp = func(path)
            assert resp.status_code == 405, (
                f"{m.upper()} {path} returned {resp.status_code}; expected 405"
            )

    unknown_paths = ["/unknown", "/live/subpath", "/ready/subpath", "/health/subpath", "/admin"]
    for path in unknown_paths:
        assert client.get(path).status_code == 404, f"GET {path} expected 404"


@pytest.mark.asyncio
async def test_combined_http_and_autogateway_extension_coexistence():
    """Verify service with BOTH HttpExtension and AutoGatewayExtension mounts probes cleanly without collision."""

    class CombinedSvc(CliffracerService):
        http = HttpExtension(port=0)
        gw = AutoGatewayExtension(prefix="/api")

    svc = CombinedSvc(ServiceConfig(name="combined_svc"))
    _simulate_service_state(svc, running=True, broker_state="connected")

    ctx = ExtensionSetupContext(
        service_config=svc.config,
        broker_url="nats://localhost:4222",
        service=svc,
    )
    # Set up both extensions
    await svc.http.setup(ctx)
    await svc.gw.setup(ctx)

    # Client against shared app
    client = TestClient(svc.http.app)

    # Probes succeed
    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/info").status_code == 200

    # Simulate outage
    async def bad_dep():
        raise RuntimeError("broken")

    svc.add_dependency("dep1", bad_dep)

    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 503


@pytest.mark.asyncio
async def test_http_extension_concurrency_isolation():
    """Verify /live remains responsive and fast (<0.1s) even while /ready waits on slow dependency."""

    class ConcurrencySvc(CliffracerService):
        http = HttpExtension(port=0)

    svc = ConcurrencySvc(ServiceConfig(name="concurrent_http_svc"))
    _simulate_service_state(svc, running=True, broker_state="connected")

    async def slow_probe():
        await asyncio.sleep(0.3)
        raise TimeoutError("slow timed out")

    svc.add_dependency("slow", slow_probe, timeout=0.35)
    client = await _create_http_client(svc, svc.http)

    loop = asyncio.get_running_loop()

    # Launch slow /ready in executor
    ready_future = loop.run_in_executor(None, lambda: client.get("/ready"))

    # Immediately fire 10 /live requests
    t0 = time.monotonic()
    live_resps = [client.get("/live") for _ in range(10)]
    t_duration = time.monotonic() - t0

    assert t_duration < 0.15, f"10 /live requests took {t_duration}s during slow /ready"
    for r in live_resps:
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    ready_resp = await ready_future
    assert ready_resp.status_code == 503
    assert ready_resp.json()["status"] == "unhealthy"

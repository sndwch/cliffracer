"""Adversarial stress tests for HealthListener Kubernetes probe segregation.

Empirically challenges:
1. Probe segregation under degraded downstream dependencies (exceptions, timeouts, partial outages).
2. Probe segregation under broker failures (CONNECTING, DISCONNECTED, CLOSED, dynamic flapping).
3. Probe responses under stopped service state (before start, after stop).
4. Backward compatibility parity between /health and /ready across all operational states.
5. HTTP routing security: non-GET methods (POST, PUT, DELETE, PATCH, OPTIONS) returning 405,
   invalid paths returning 404, and resilience to malformed HTTP streams.
6. Non-blocking concurrency isolation: slow/failing /ready checks do not block concurrent /live probes.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


async def _raw_request(
    port: int,
    path: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | bytes = "",
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Execute raw HTTP request over TCP socket, returning (status, headers, parsed_json_or_empty)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req_headers = {"Host": "localhost", "Connection": "close"}
    if headers:
        req_headers.update(headers)
    if isinstance(body, str):
        body_bytes = body.encode()
    else:
        body_bytes = body
    if body_bytes and "Content-Length" not in req_headers:
        req_headers["Content-Length"] = str(len(body_bytes))

    header_lines = "".join(f"{k}: {v}\r\n" for k, v in req_headers.items())
    req = f"{method} {path} HTTP/1.1\r\n{header_lines}\r\n".encode() + body_bytes
    writer.write(req)
    await writer.drain()

    raw = await reader.read()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass

    head, _, resp_body = raw.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split(" ")[1])
    resp_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            resp_headers[k.lower()] = v

    parsed: dict[str, Any] = {}
    if resp_body:
        try:
            parsed = json.loads(resp_body)
        except Exception:
            parsed = {"raw": resp_body.decode(errors="replace")}
    return status, resp_headers, parsed


def _simulate_service_state(
    svc: CliffracerService,
    *,
    running: bool = True,
    broker_state: str = "connected",
) -> None:
    """Configure mock broker and lifecycle state on service instance."""
    svc._running = running
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


async def test_health_listener_dependency_exceptions_segregation():
    """Verify that multiple explosive downstream dependencies degrade /ready but never /live."""
    svc = CliffracerService(ServiceConfig(name="orders_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    async def broken_db() -> None:
        raise ConnectionRefusedError("Database 10.0.0.5:5432 unreachable")

    async def broken_vault() -> None:
        raise RuntimeError("Vault token expired")

    async def healthy_cache() -> bool:
        return True

    svc.add_dependency("db", broken_db)
    svc.add_dependency("vault", broken_vault)
    svc.add_dependency("cache", healthy_cache)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # /live must return 200 OK
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"
        assert live_body["service"] == "orders_svc"
        assert "dependencies" not in live_body

        # /ready must return 503 Service Unavailable
        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"
        assert sorted(ready_body["unhealthy_dependencies"]) == ["db", "vault"]
        assert ready_body["dependencies"]["cache"]["ok"] is True
        assert ready_body["dependencies"]["db"]["ok"] is False
        assert "ConnectionRefusedError" in ready_body["dependencies"]["db"]["error"]

        # /health must mirror /ready exactly (excluding dynamic timestamp and latency)
        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == ready_body["status"]
        assert health_body["service"] == ready_body["service"]
        assert health_body.get("unhealthy_dependencies") == ready_body.get("unhealthy_dependencies")
        assert health_body.keys() == ready_body.keys()
    finally:
        await hl.stop()


async def test_health_listener_dependency_timeout_segregation():
    """Verify that hanging downstream dependencies trigger timeout in /ready without stalling /live."""
    svc = CliffracerService(ServiceConfig(name="slow_dep_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    async def hanging_payment_gateway() -> None:
        await asyncio.sleep(5.0)

    # 0.05s timeout for fast test execution
    svc.add_dependency("payment_gw", hanging_payment_gateway, timeout=0.05)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        # /live is instantaneous and 200 OK
        t0 = time.monotonic()
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        t_live = time.monotonic() - t0
        assert live_status == 200
        assert live_body["status"] == "healthy"
        assert t_live < 0.05, f"/live took {t_live}s; must be instantaneous"

        # /ready waits for timeout and reports 503
        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"
        assert "payment_gw" in ready_body["unhealthy_dependencies"]
        assert "timed out after 0.05s" in ready_body["dependencies"]["payment_gw"]["error"]

        # /health mirrors /ready
        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "unhealthy"
        assert "payment_gw" in health_body["unhealthy_dependencies"]
    finally:
        await hl.stop()


def _assert_health_mirrors_ready(health_body: dict[str, Any], ready_body: dict[str, Any]) -> None:
    """Verify /health payload mirrors /ready payload across all operational fields."""
    assert health_body["status"] == ready_body["status"]
    assert health_body["service"] == ready_body["service"]
    assert health_body["broker_state"] == ready_body["broker_state"]
    assert health_body.keys() == ready_body.keys()
    assert health_body.get("unhealthy_dependencies") == ready_body.get("unhealthy_dependencies")


async def test_health_listener_broker_states_segregation():
    """Verify /live remains 200 during CONNECTING, DISCONNECTED, and CLOSED states while /ready fails."""
    svc = CliffracerService(ServiceConfig(name="broker_test_svc", health_port=0))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # 1. CONNECTING state
        _simulate_service_state(svc, running=True, broker_state="connecting")
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "connecting"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)

        # 2. DISCONNECTED state
        _simulate_service_state(svc, running=True, broker_state="disconnected")
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "disconnected"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)

        # 3. CLOSED state
        _simulate_service_state(svc, running=True, broker_state="closed")
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 200
        assert live_body["status"] == "healthy"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "disconnected"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        _assert_health_mirrors_ready(health_body, ready_body)
    finally:
        await hl.stop()


async def test_health_listener_broker_flapping_dynamic_recovery():
    """Verify /live remains constant 200 while /ready dynamically toggles 200 <-> 503 as broker flaps."""
    svc = CliffracerService(ServiceConfig(name="flapping_svc", health_port=0))
    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # Connected
        _simulate_service_state(svc, running=True, broker_state="connected")
        assert (await _raw_request(hl.port, "/live"))[0] == 200
        assert (await _raw_request(hl.port, "/ready"))[0] == 200

        # Disconnected
        _simulate_service_state(svc, running=True, broker_state="disconnected")
        assert (await _raw_request(hl.port, "/live"))[0] == 200
        assert (await _raw_request(hl.port, "/ready"))[0] == 503

        # Re-connected
        _simulate_service_state(svc, running=True, broker_state="connected")
        assert (await _raw_request(hl.port, "/live"))[0] == 200
        assert (await _raw_request(hl.port, "/ready"))[0] == 200
    finally:
        await hl.stop()


async def test_health_listener_stopped_service_all_probes_503():
    """Verify /live, /ready, and /health return 503 when service is stopped."""
    svc = CliffracerService(ServiceConfig(name="stopped_svc", health_port=0))
    _simulate_service_state(svc, running=False, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None
    try:
        live_status, _, live_body = await _raw_request(hl.port, "/live")
        assert live_status == 503
        assert live_body["status"] == "stopped"

        ready_status, _, ready_body = await _raw_request(hl.port, "/ready")
        assert ready_status == 503
        assert ready_body["status"] == "stopped"

        health_status, _, health_body = await _raw_request(hl.port, "/health")
        assert health_status == 503
        assert health_body["status"] == "stopped"
    finally:
        await hl.stop()


async def test_health_listener_method_not_allowed_on_all_endpoints():
    """Verify POST, PUT, DELETE, PATCH, and OPTIONS on /live, /ready, /health, /info return 405."""
    svc = CliffracerService(ServiceConfig(name="methods_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    endpoints = ["/live", "/ready", "/health", "/info"]
    disallowed_methods = ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"]

    try:
        for path in endpoints:
            for method in disallowed_methods:
                status, _, body = await _raw_request(
                    hl.port, path, method=method, body="{'some': 'payload'}"
                )
                assert status == 405, f"{method} {path} returned {status}; expected 405"
                assert body == {"error": "method not allowed"}
    finally:
        await hl.stop()


async def test_health_listener_invalid_and_adversarial_paths_return_404():
    """Verify unknown and adversarial paths return 404 Not Found."""
    svc = CliffracerService(ServiceConfig(name="paths_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    invalid_paths = [
        "/",
        "/unknown",
        "/live/",
        "/ready/",
        "/health/",
        "/live/subpath",
        "/ready/subpath",
        "/admin",
        "/metrics",
        "/api/v1/health",
        "/..",
        "/./live",
    ]

    try:
        for path in invalid_paths:
            status, _, body = await _raw_request(hl.port, path)
            assert status == 404, f"GET {path} returned {status}; expected 404"
            assert body == {"error": "not found"}
    finally:
        await hl.stop()


async def test_health_listener_malformed_and_edge_case_requests():
    """Verify HealthListener survives malformed HTTP requests and immediate connection drops."""
    svc = CliffracerService(ServiceConfig(name="malformed_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # Case 1: Client connects and closes immediately without sending data
        reader, writer = await asyncio.open_connection("127.0.0.1", hl.port)
        writer.close()
        await writer.wait_closed()

        # Case 2: Client sends empty lines then closes
        reader, writer = await asyncio.open_connection("127.0.0.1", hl.port)
        writer.write(b"\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        # Case 3: Client sends binary garbage
        reader, writer = await asyncio.open_connection("127.0.0.1", hl.port)
        writer.write(b"\x00\xff\xfe\xfd\x01\x02\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        # Binary garbage does not have method "GET", returns 405 or 500
        assert b"405" in raw or b"500" in raw or b"404" in raw

        # Server must still be healthy and answer normal requests cleanly
        status, _, body = await _raw_request(hl.port, "/live")
        assert status == 200
        assert body["status"] == "healthy"
    finally:
        await hl.stop()


async def test_health_listener_non_blocking_concurrency_stress():
    """Stress test: verify /live probe is NEVER blocked or delayed by a slow/hanging /ready probe."""
    svc = CliffracerService(ServiceConfig(name="concurrency_stress_svc", health_port=0))
    _simulate_service_state(svc, running=True, broker_state="connected")

    slow_started = asyncio.Event()

    async def slow_probe() -> None:
        slow_started.set()
        await asyncio.sleep(0.3)
        raise TimeoutError("slow downstream timed out")

    svc.add_dependency("slow_db", slow_probe, timeout=0.35)

    hl = HealthListener(svc, "127.0.0.1", 0)
    await hl.start()
    assert hl.port is not None

    try:
        # Start slow /ready request in background task
        ready_task = asyncio.create_task(_raw_request(hl.port, "/ready"))

        # Wait until the slow probe has definitely begun execution
        await asyncio.wait_for(slow_started.wait(), timeout=1.0)

        # While /ready is blocked, fire 20 concurrent /live probes
        t0 = time.monotonic()
        live_results = await asyncio.gather(*[_raw_request(hl.port, "/live") for _ in range(20)])
        t_duration = time.monotonic() - t0

        # All 20 /live probes must have completed with 200 OK within < 0.1s
        assert len(live_results) == 20
        for status, _, body in live_results:
            assert status == 200
            assert body["status"] == "healthy"

        assert t_duration < 0.15, (
            f"20 /live requests took {t_duration}s during slow /ready; expected < 0.15s"
        )

        # The slow /ready task eventually completes with 503
        ready_status, _, ready_body = await ready_task
        assert ready_status == 503
        assert ready_body["status"] == "unhealthy"
    finally:
        await hl.stop()

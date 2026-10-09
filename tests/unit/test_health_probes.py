"""The probes of a healthy service, the service's own liveness helpers, and a port conflict.

What /live, /ready and /health answer while a dependency fails or the broker is connecting,
disconnected, draining or closed, and while the service is stopped, is read in
`test_health_listener_adversarial_stress.py`, with the routing and the concurrency of the listener.
"""

from __future__ import annotations

import asyncio
import errno
import json
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


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


def _simulate_running_service(svc: CliffracerService) -> None:
    """Set up service state simulating running lifecycle without live NATS daemon."""
    svc._running = True
    svc.nc = type(
        "NC",
        (),
        {"is_closed": False, "is_connected": True, "is_draining": False, "is_connecting": False},
    )()


async def test_all_probes_return_200_when_fully_healthy():
    """Verify /live, /ready, and /health return 200 when all systems pass."""
    svc = CliffracerService(ServiceConfig(name="healthy_svc", health_port=0))
    _simulate_running_service(svc)

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

        # `latency_ms` is a FRESH measurement on every probe run --
        # dependencies.py:118 rounds (time.monotonic() - started) to 0.1 ms -- so
        # two reads of the same passing probe are NOT expected to produce the
        # same number, and comparing whole payloads made this assertion fail
        # whenever the two landed in different tenths of a millisecond. Measured
        # in CI (run 1868 job 4103, under three concurrent jobs on one host):
        #     {'error': None, 'latency_ms': 0.0, 'ok': True}
        #  != {'error': None, 'latency_ms': 0.7, 'ok': True}
        # It passed on an idle box only because both rounded to 0.0 -- i.e. the
        # assertion held by luck of timer resolution, not by the contract.
        #
        # Compare everything EXCEPT the volatile field, rather than listing the
        # fields to compare, so a newly added stable field is still covered.
        volatile = {"latency_ms"}

        def _stable(deps: dict[str, Any]) -> dict[str, Any]:
            return {
                name: {k: v for k, v in dep.items() if k not in volatile}
                for name, dep in deps.items()
            }

        assert _stable(health_body["dependencies"]) == _stable(ready_body["dependencies"])

        # The excluded field is still asserted, so dropping it from the payload
        # (or emitting a non-numeric) reds rather than silently losing coverage.
        for body in (ready_body, health_body):
            latency = body["dependencies"]["cache"]["latency_ms"]
            assert isinstance(latency, int | float)
            assert latency >= 0.0
    finally:
        await hl.stop()


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
    hl2 = HealthListener(svc2, "127.0.0.1", hl1.port)
    try:
        with pytest.raises(OSError) as exc_info:
            await hl2.start()
        assert exc_info.value.errno == errno.EADDRINUSE
        assert hl2.port is None
        assert hl2._server is None
    finally:
        await hl1.stop()

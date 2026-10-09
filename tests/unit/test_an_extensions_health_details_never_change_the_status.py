"""An extension's `health_details()` is informational: it never changes the status.

A raising `health_details()`, one that reports the extension stopped, and one that returns a
`status` of `unhealthy` all leave a connected, running service `healthy` and `/ready` at 200; the
contribution is in the body. An extension that
must gate readiness registers a dependency probe, and that does turn the answer into a 503.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


class Raises(Extension):
    def health_details(self):
        raise RuntimeError("the exporter is down")


class SaysStopped(Extension):
    def health_details(self):
        return {"router": "stopped"}


class SaysUnhealthy(Extension):
    def health_details(self):
        return {"status": "unhealthy", "healthy": False, "ok": False}


class Svc(CliffracerService):
    broken = Raises()
    reports = SaysStopped()


class Unhealthy(CliffracerService):
    exporter = SaysUnhealthy()


async def _running_service(service_cls=Svc) -> CliffracerService:
    svc = service_cls(ServiceConfig(name="informed", health_port=0))
    await svc.container._setup_extensions()
    svc._running = True
    svc.nc = SimpleNamespace(
        is_closed=False, is_connected=True, is_draining=False, is_connecting=False
    )
    return svc


async def _get(listener: HealthListener, path: str) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


async def test_a_raising_and_a_stopped_extension_leave_the_service_healthy():
    svc = await _running_service()

    health = await svc.health_check()

    assert health["status"] == "healthy"
    assert health["broken"] == {"error": "health details unavailable"}
    assert health["reports"] == {"router": "stopped"}


async def test_a_contribution_that_says_unhealthy_leaves_the_service_healthy_and_the_listener_at_200():
    svc = await _running_service(Unhealthy)
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        ready = await _get(listener, "/ready")
        health = await _get(listener, "/health")
    finally:
        await listener.stop()

    assert (await svc.health_check())["status"] == "healthy"
    assert ready[0] == health[0] == 200
    assert health[1]["status"] == "healthy"
    # The extension's words are in the body, under its own name, and not read as the status.
    assert health[1]["exporter"] == {"status": "unhealthy", "healthy": False, "ok": False}


async def test_the_listener_still_answers_200_on_ready_and_health():
    svc = await _running_service()
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        ready = await _get(listener, "/ready")
        health = await _get(listener, "/health")
    finally:
        await listener.stop()

    assert ready[0] == health[0] == 200
    assert ready[1]["reports"] == {"router": "stopped"}


async def test_CONTROL_a_failing_dependency_probe_does_turn_the_answer_into_503():
    svc = await _running_service()

    async def down() -> None:
        raise ConnectionError("unreachable")

    svc.add_dependency("exporter", down)
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        status, body = await _get(listener, "/ready")
    finally:
        await listener.stop()

    assert status == 503 and body["status"] == "unhealthy"
    assert body["unhealthy_dependencies"] == ["exporter"]

"""A status probe whose evaluation crashes answers 503, so "unavailable" is the one thing a
prober reading status codes can see.

ADR-0003: `/health` is 200 when healthy and 503 when it is not. When `health_check()` itself
raised, the listener answered 500, which a `curl -f` still fails on but which the ADR does not
name, and which nothing pinned: a tidy-up that turned the crash into a 200 with an error body, or
dropped the connection, would have passed every test. `/live`, `/ready` and `/health` now answer
503 `{"status": "error", ...}` when their evaluation raises; `/info` is not a probe and a crash
there stays a 500. The exception's own words stay behind the exposure switch.
"""

import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener
from tests.unit.test_health_listener_adversarial_stress import (
    _raw_request,
    _simulate_service_state,
)

pytestmark = pytest.mark.unit

SECRET = "sup3rs3cret-password"


def _service(*, expose: bool = False) -> CliffracerService:
    svc = CliffracerService(
        ServiceConfig(name="crashy", health_port=0, expose_internal_errors=expose)
    )
    _simulate_service_state(svc, running=True, broker_state="connected")
    return svc


async def _get(svc: CliffracerService, path: str) -> tuple[int, dict]:
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        status, _, body = await _raw_request(listener.port, path)
        return status, body
    finally:
        await listener.stop()


async def _boom(*_args, **_kwargs):
    raise RuntimeError(f"evaluation crashed: postgres://user:{SECRET}@db/x")


@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_a_crashing_health_check_answers_503_with_an_error_status(path):
    svc = _service()
    svc.health_check = _boom

    status, body = await _get(svc, path)

    assert status == 503, (status, body)
    assert body["status"] == "error", body
    assert SECRET not in json.dumps(body), body


async def test_a_crashing_liveness_check_answers_503_too():
    svc = _service()
    svc.liveness_check = lambda: (_ for _ in ()).throw(RuntimeError("liveness crashed"))

    status, body = await _get(svc, "/live")

    assert (status, body["status"]) == (503, "error")


async def test_the_exception_text_is_exposed_only_when_the_switch_allows_it():
    svc = _service(expose=True)
    svc.health_check = _boom

    status, body = await _get(svc, "/health")

    assert status == 503
    assert "evaluation crashed" in body["error"], body


async def test_a_crash_building_the_info_payload_is_still_a_500_because_info_is_no_probe():
    svc = _service()
    svc.get_service_info = lambda: (_ for _ in ()).throw(RuntimeError("info crashed"))

    status, _ = await _get(svc, "/info")

    assert status == 500


async def test_CONTROL_a_healthy_service_is_still_200_and_an_unhealthy_one_still_503():
    healthy = _service()
    status, body = await _get(healthy, "/health")
    assert (status, body["status"]) == (200, "healthy"), body

    stopped = _service()
    _simulate_service_state(stopped, running=False, broker_state="connected")
    status, body = await _get(stopped, "/health")
    assert status == 503 and body["status"] != "error", (status, body)

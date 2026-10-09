"""A stopped service answers its health check without running a dependency probe.

`health_check` ran every probe before it looked at whether the service was running, so a stopped
service still called its downstreams on every `/ready` and answered as slowly as its slowest probe.
A stopped service is `stopped` whatever its dependencies say, so it runs none and its body has no
`dependencies` block; `connecting` and `disconnected` keep theirs, because a dependency that is down
beside a broker that is down is a diagnosis.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cliffracer import CliffracerService, ServiceConfig, dependency

pytestmark = pytest.mark.unit

calls = {"n": 0}


class Svc(CliffracerService):
    @dependency("db", timeout=1.0)
    async def _db(self) -> None:
        calls["n"] += 1
        await asyncio.sleep(0.2)


def _service(*, running: bool, connected: bool = True) -> Svc:
    calls["n"] = 0
    svc = Svc(ServiceConfig(name="probed", health_port=0))
    svc._discover_handlers()
    svc._running = running
    svc.nc = SimpleNamespace(
        is_closed=not connected,
        is_connected=connected,
        is_draining=False,
        is_connecting=False,
    )
    return svc


async def test_a_stopped_service_runs_no_probe_and_answers_at_once():
    svc = _service(running=False)

    started = time.perf_counter()
    health = await svc.health_check()
    elapsed = time.perf_counter() - started

    assert health["status"] == "stopped"
    assert calls["n"] == 0
    # Upper bound. CI p99 7.56e-05 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 1323x
    # p99.
    assert elapsed < 0.1, f"a stopped service waited {elapsed:.2f}s on a probe"


async def test_the_body_of_a_stopped_service_has_no_dependency_keys():
    health = await _service(running=False).health_check()

    assert "dependencies" not in health
    assert "unhealthy_dependencies" not in health
    assert "dependencies_error" not in health


async def test_CONTROL_a_running_service_runs_its_probe_and_reports_it():
    svc = _service(running=True)

    health = await svc.health_check()

    assert calls["n"] == 1
    assert health["status"] == "healthy"
    assert health["dependencies"]["db"]["ok"] is True


async def test_CONTROL_a_service_that_has_lost_the_broker_still_runs_its_probe():
    svc = _service(running=True, connected=False)

    health = await svc.health_check()

    assert health["status"] == "disconnected"
    assert calls["n"] == 1
    assert "db" in health["dependencies"]


async def test_a_stopped_service_reports_stopped_and_liveness_reads_the_same():
    svc = _service(running=False)

    assert (await svc.health_check())["status"] == "stopped"
    assert svc.liveness_check()["status"] == "stopped"

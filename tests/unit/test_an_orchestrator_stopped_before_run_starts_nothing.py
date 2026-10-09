"""`await orchestrator.stop()` before `run()` records the request, and `run()` then starts nothing.

`stop()` clears each runner's running flag and sets the shared shutdown event, and `run()` on a
runner set the flag back to true unconditionally, so the request was recorded and then undone:
every service was constructed, started and stopped once. A stop asked for before the run is a stop
that has already happened.
"""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

pytestmark = pytest.mark.unit

EVENTS: list[str] = []


class Quiet(CliffracerService):
    async def start(self) -> None:
        EVENTS.append(f"start {self.config.name}")

    async def stop(self) -> None:
        EVENTS.append(f"stop {self.config.name}")


def _config(name: str) -> ServiceConfig:
    return ServiceConfig(name=name, health_port=0, health_listener=False, auto_restart=False)


@pytest.fixture(autouse=True)
def _clear_events():
    EVENTS.clear()


@pytest.mark.asyncio
async def test_run_after_a_stop_starts_no_service():
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))
    orchestrator.add_service(Quiet, config=_config("two"))

    await asyncio.wait_for(orchestrator.stop(), timeout=1)
    status = await asyncio.wait_for(orchestrator.run(), timeout=10)

    assert EVENTS == [], EVENTS
    assert status == 0


@pytest.mark.asyncio
async def test_a_runner_whose_shutdown_event_is_already_set_starts_nothing_and_reports_ok():
    runner = ServiceRunner(Quiet, config=_config("solo"))
    runner._shutdown_event.set()

    status = await asyncio.wait_for(runner.run(), timeout=10)

    assert EVENTS == [], EVENTS
    assert status == 0


@pytest.mark.asyncio
async def test_CONTROL_run_without_a_prior_stop_starts_the_service():
    """Without this, "starts nothing" could mean "starts nothing ever"."""
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))
    task = asyncio.create_task(orchestrator.run())
    for _ in range(300):
        if "start one" in EVENTS:
            break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(orchestrator.stop(), timeout=10)
    await asyncio.wait_for(task, timeout=10)

    assert EVENTS[:1] == ["start one"], EVENTS

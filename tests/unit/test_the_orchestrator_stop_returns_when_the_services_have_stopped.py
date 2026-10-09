"""`await orchestrator.stop()` returns once `run()` has finished, and `run()` leaves signals alone.

`stop()` was `async` with no `await`: it set a flag and an event and returned, while the services
were still being torn down on another task, with no handle for a caller to wait on. It now waits
for `run()` to return. The orchestrator also installed process-wide SIGTERM and SIGINT handlers
that each runner then replaced with its own, so its handler and its `_running` flag were never
the ones in use. The handlers are installed by `run_forever()`, which owns the process, and the
runners an orchestrator drives install none.
"""

import asyncio
import signal

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

pytestmark = pytest.mark.unit

EVENTS: list[str] = []


class Quiet(CliffracerService):
    """A service that starts and stops without a broker, and says when it does."""

    async def start(self) -> None:
        EVENTS.append(f"start {self.config.name}")

    async def stop(self) -> None:
        await asyncio.sleep(0.05)
        EVENTS.append(f"stop {self.config.name}")


def _config(name: str) -> ServiceConfig:
    return ServiceConfig(name=name, health_port=0, health_listener=False, auto_restart=False)


@pytest.fixture(autouse=True)
def _clear_events():
    EVENTS.clear()


async def _running(orchestrator: ServiceOrchestrator) -> asyncio.Task[int]:
    task = asyncio.create_task(orchestrator.run())
    for _ in range(200):
        if len([e for e in EVENTS if e.startswith("start")]) == len(orchestrator.runners):
            return task
        await asyncio.sleep(0.01)
    task.cancel()
    raise AssertionError(f"the services did not start: {EVENTS}")


@pytest.mark.asyncio
async def test_stop_returns_after_every_service_has_stopped():
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))
    orchestrator.add_service(Quiet, config=_config("two"))
    task = await _running(orchestrator)

    await asyncio.wait_for(orchestrator.stop(), timeout=10)

    assert task.done(), "stop() returned while run() was still going"
    assert sorted(e for e in EVENTS if e.startswith("stop")) == ["stop one", "stop two"]
    assert task.result() == 0


@pytest.mark.asyncio
async def test_two_callers_of_stop_both_wait():
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))
    task = await _running(orchestrator)

    await asyncio.wait_for(asyncio.gather(orchestrator.stop(), orchestrator.stop()), timeout=10)

    assert task.done()


@pytest.mark.asyncio
async def test_CONTROL_stop_before_run_only_records_the_request_and_does_not_wait():
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))

    await asyncio.wait_for(orchestrator.stop(), timeout=1)

    assert EVENTS == []


@pytest.mark.asyncio
async def test_CONTROL_stop_after_run_has_finished_returns_at_once():
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))
    task = await _running(orchestrator)
    await asyncio.wait_for(orchestrator.stop(), timeout=10)
    assert task.done()

    await asyncio.wait_for(orchestrator.stop(), timeout=1)


def _dispositions():
    return {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}


@pytest.mark.asyncio
async def test_run_does_not_touch_the_process_signal_handlers():
    before = _dispositions()
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Quiet, config=_config("one"))
    task = await _running(orchestrator)
    during = _dispositions()
    await asyncio.wait_for(orchestrator.stop(), timeout=10)
    await asyncio.wait_for(task, timeout=10)

    assert during == before == _dispositions()


@pytest.mark.asyncio
async def test_a_runner_run_does_not_touch_the_process_signal_handlers():
    before = _dispositions()
    runner = ServiceRunner(Quiet, config=_config("solo"))
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if EVENTS:
            break
        await asyncio.sleep(0.01)
    during = _dispositions()
    runner._running = False
    runner._shutdown_event.set()
    await asyncio.wait_for(task, timeout=5)

    assert during == before == _dispositions()


def test_the_orchestrator_keeps_no_flag_nothing_reads():
    assert not hasattr(ServiceOrchestrator(), "_running")

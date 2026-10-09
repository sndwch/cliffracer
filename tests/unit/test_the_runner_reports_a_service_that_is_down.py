"""A runner whose service is permanently down reports a failure.

`run()` returned `None` however it ended, `run_forever()` exited non-zero only
on an unhandled exception, and the CLI returned 0 unconditionally. So a service
configured `auto_restart=False` that could not reach the broker logged its
crash, stopped, and the process exited **successfully** having never run it --
which tells a supervisor there was nothing to restart.

THE DISCRIMINATOR IS COMPLETION, NOT SPEED. Each bound below separates "returns"
from "never returns"; the expected time is milliseconds to about a second and
the bound is ten seconds, so a loaded host does not reach it. The numbers are
not thresholds and nothing here is measuring performance.

DRIVEN THROUGH THE REAL TRANSITIONS. The permanently-down paths are reached by
making the service's own start fail, and by moving the broker to
`BrokerConnectionState.CLOSED` so the runner's poll loop sees it -- not by
setting `_running` or `_shutdown_event` by hand. The two existing lifecycle
tests set `_shutdown_event` before awaiting, which is the condition that hid
this: the event being set is exactly what the runner reads to decide it was
asked to stop.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.connection import BrokerConnectionState
from cliffracer.runners.orchestrator import (
    RUNNER_OK,
    RUNNER_SERVICE_DOWN,
    ServiceOrchestrator,
    ServiceRunner,
)

pytestmark = pytest.mark.unit

# Separates "returns" from "hangs". Ten seconds against an expectation of about
# one: a bound this loose cannot be crossed by contention, only by the runner
# never finishing.
COMPLETES_WITHIN = 10.0

# Long enough that a runner which is going to return has, short enough to keep
# the healthy-service control quick. Only the control depends on it.
STAYS_ALIVE_FOR = 2.5


def _config(name: str, **kwargs) -> ServiceConfig:
    return ServiceConfig(
        name=name, health_port=0, health_listener=False, restart_delay=0.01, **kwargs
    )


class CannotStart(CliffracerService):
    """Its own start fails, as a service that cannot reach the broker does."""

    async def start(self) -> None:
        raise RuntimeError("cannot reach the broker")


class StartsThenLosesTheBroker(CliffracerService):
    """Starts, then reports the broker closed -- the real enum, read by the poll."""

    async def start(self) -> None:
        self._closed = True

    async def stop(self) -> None:
        return None

    @property
    def broker_state(self) -> BrokerConnectionState:
        if getattr(self, "_closed", False):
            return BrokerConnectionState.CLOSED
        return BrokerConnectionState.CONNECTED


class HealthyService(CliffracerService):
    """Starts and stays up, so its runner has no reason to return."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def broker_state(self) -> BrokerConnectionState:
        return BrokerConnectionState.CONNECTED


# --- permanently down: the runner returns, and says it failed ----------------


@pytest.mark.asyncio
async def test_a_service_that_cannot_start_makes_the_runner_report_down():
    """The realistic path: `auto_restart=False` and a start that raises."""
    runner = ServiceRunner(CannotStart, _config("cannot_start", auto_restart=False))

    status = await asyncio.wait_for(runner.run(), timeout=COMPLETES_WITHIN)

    assert status == RUNNER_SERVICE_DOWN, status
    assert not runner._shutdown_event.is_set(), (
        "nothing asked this runner to stop, so the event must be clear -- if it "
        "were set, the status above would be right for the wrong reason"
    )


@pytest.mark.asyncio
async def test_a_closed_broker_makes_the_runner_report_down():
    """Driven through the state the runner actually polls.

    The service starts, then its `broker_state` is `CLOSED`, which the runner's
    inner loop reads once a second. With `auto_restart=False` there is nothing
    to restart, so the runner ends and must say so.
    """
    runner = ServiceRunner(StartsThenLosesTheBroker, _config("loses_broker", auto_restart=False))

    status = await asyncio.wait_for(runner.run(), timeout=COMPLETES_WITHIN)

    assert status == RUNNER_SERVICE_DOWN, status


@pytest.mark.asyncio
async def test_the_orchestrator_reports_down_when_one_service_is():
    """One dead service is enough, even with a healthy one beside it.

    The orchestrator goes on running the rest, so the exit code is the only
    thing that will tell anyone a service is missing.
    """
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(CannotStart, _config("dead_one", auto_restart=False))
    orchestrator.add_service(HealthyService, _config("live_one"))

    dead = orchestrator.runners[0]
    dead_finished = asyncio.Event()
    run_the_dead_one = dead.run

    async def run_and_say_so() -> int:
        try:
            return await run_the_dead_one()
        finally:
            dead_finished.set()

    dead.run = run_and_say_so  # type: ignore[method-assign]

    async def stop_once_the_dead_one_has_finished() -> None:
        await dead_finished.wait()
        await orchestrator.stop()

    stopper = asyncio.create_task(stop_once_the_dead_one_has_finished())
    try:
        status = await asyncio.wait_for(orchestrator.run(), timeout=COMPLETES_WITHIN)
    finally:
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)

    assert status == RUNNER_SERVICE_DOWN, (
        f"one service was permanently down and the orchestrator reported {status}"
    )


# --- asked to stop: that is a success ----------------------------------------


@pytest.mark.asyncio
async def test_CONTROL_a_healthy_service_keeps_the_runner_alive():
    """The other direction. Without this, "returns 1" could mean "always returns".

    A runner over a healthy service must NOT return on its own -- and when it is
    finally asked to stop, it must report success rather than the down status.
    """
    runner = ServiceRunner(HealthyService, _config("healthy"))
    run_task = asyncio.create_task(runner.run())

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(run_task), timeout=STAYS_ALIVE_FOR)

    assert not run_task.done(), "the runner returned while its service was healthy"

    runner._shutdown_event.set()
    status = await asyncio.wait_for(run_task, timeout=COMPLETES_WITHIN)

    assert status == RUNNER_OK, (
        f"a runner asked to stop reported {status}; only a service that went down "
        "on its own is a failure"
    )


@pytest.mark.asyncio
async def test_CONTROL_the_two_statuses_are_different_numbers():
    """Asserted because every test above compares against one of them.

    If they were ever made equal, each assertion would pass whatever the runner
    reported.
    """
    assert RUNNER_OK != RUNNER_SERVICE_DOWN
    assert RUNNER_OK == 0, "a supervisor reads 0 as success"
    assert RUNNER_SERVICE_DOWN != 0, "a down service must not exit 0"


# --- the process boundary, which is what a supervisor reads -----------------


PROCESS_SCRIPT = """
import asyncio, os, signal, sys, threading

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.runners.orchestrator import ServiceRunner


class Service(CliffracerService):
    async def start(self):
        if {fails!r}:
            raise RuntimeError("cannot reach the broker")

    async def stop(self):
        return None


if not {fails!r}:
    # Ask it to stop the way an operator would, through the signal handler the
    # runner installs -- not by setting the event, which is the shortcut that
    # hid this defect in the first place.
    threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()

ServiceRunner(
    Service,
    ServiceConfig(
        name="proc", health_port=0, health_listener=False,
        restart_delay=0.01, auto_restart=False,
    ),
).run_forever()
"""


def _exit_code(*, fails: bool) -> int:
    """Run `run_forever()` in its own process and return what it exited with."""
    result = subprocess.run(
        [sys.executable, "-c", PROCESS_SCRIPT.format(fails=fails)],
        capture_output=True,
        text=True,
        timeout=COMPLETES_WITHIN * 3,
        check=False,
    )
    assert "Traceback" not in result.stderr or result.returncode != 0, result.stderr
    return result.returncode


def test_a_down_service_exits_the_process_non_zero():
    """The whole point of the issue: a supervisor has to see a failure.

    A live PID with a zero exit code is what let a service that never started
    look like a completed job.
    """
    assert _exit_code(fails=True) != 0, (
        "the process exited 0 with its service permanently down, so nothing would restart it"
    )


def test_CONTROL_a_service_asked_to_stop_exits_zero():
    """Otherwise "non-zero" above could just mean "always non-zero".

    Stopped through SIGTERM and the runner's own handler, so this is the path an
    operator or an orchestrator actually takes.
    """
    assert _exit_code(fails=False) == 0

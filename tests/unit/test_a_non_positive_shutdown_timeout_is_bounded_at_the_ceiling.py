"""A `shutdown_timeout` at or below zero is bounded at the ceiling and reported, never endless.

`ServiceConfig` refuses such a value, so only a config built around validation (a duck-typed one, or
one assigned without validation) holds it. `None` is the one spelling of "no deadline". Every place
that reads the setting as a bound reads a value at or below zero as `ON_SHUTDOWN_CEILING` and logs
one warning naming it: a timer stopping a cancelled run on its own, the drain of a service's
supervised tasks, `on_shutdown` after a cancelled stop, and the release of the intake after one.
The ceiling is patched small here, so a bounded wait is told from an endless one in a test's time.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.core import lifecycle
from cliffracer.core.lifecycle import LifecycleManager
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit

CEILING = 0.3
# The upper bounds below are measured on the CI runner: this file run 20 times on each of the two
# test jobs (n=40 per case, 1-minute load 1.3-3.7). Each bound is at least 3x the p99 of the wait
# it bounds, and an endless wait reads about 3 s, so each still fails when its deadline is removed.
VALUES = pytest.mark.parametrize("shutdown_timeout", [0, -1], ids=["zero", "negative"])


@pytest.fixture(autouse=True)
def small_ceiling(monkeypatch):
    monkeypatch.setattr(lifecycle, "ON_SHUTDOWN_CEILING", CEILING)


@pytest.fixture
def warnings_said():
    said: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: said.append((m.record["level"].name, m.record["message"])), level="WARNING"
    )
    yield said
    logger.remove(sink)


def _warning(where: str, value: float) -> tuple[str, str]:
    return (
        "WARNING",
        f"{where}: shutdown_timeout is {value!r}, which is not a duration; bounding it at "
        f"{CEILING:g} seconds (ON_SHUTDOWN_CEILING). Use None to wait without a deadline",
    )


def _manager(shutdown_timeout, **hooks) -> LifecycleManager:
    config = ServiceConfig(name="svc", health_port=0)
    object.__setattr__(config, "shutdown_timeout", shutdown_timeout)  # around validation
    return LifecycleManager(config=config, hooks=SimpleNamespace(**hooks))  # type: ignore[arg-type]


async def _three_seconds() -> None:
    await asyncio.sleep(3.0)


# --- a timer stopping a cancelled run on its own -------------------------------------------------


class Service:
    def __init__(self, shutdown_timeout) -> None:
        self.config = SimpleNamespace(name="svc", shutdown_timeout=shutdown_timeout)
        self.running = asyncio.Event()
        self.release = False

    async def tick(self) -> None:
        self.running.set()
        end = time.monotonic() + 3.0
        while time.monotonic() < end and not self.release:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass  # a run that ignores cancellation


@VALUES
async def test_a_timer_gives_a_run_that_ignores_cancellation_the_ceiling(
    shutdown_timeout, warnings_said
):
    service = Service(shutdown_timeout)
    t = Timer(interval=0.05, eager=True)
    t.method_name = "tick"
    await t.start(service)
    await asyncio.wait_for(service.running.wait(), 2)
    started = time.monotonic()

    await asyncio.wait_for(t.stop(), timeout=5)

    took = time.monotonic() - started
    service.release = True
    if t.task is not None and not t.task.done():
        await asyncio.wait_for(asyncio.shield(t.task), 2)
    # CI p99 0.303 s. The lower bound tells the ceiling from a run cut off at once.
    assert CEILING * 0.8 <= took < 1.5, took
    assert _warning("Timer tick", shutdown_timeout) in warnings_said, warnings_said


# --- the drain of a service's supervised tasks --------------------------------------------------


@VALUES
async def test_the_drain_is_bounded_at_the_ceiling(shutdown_timeout, warnings_said):
    manager = _manager(shutdown_timeout)
    task = manager.spawn_supervised_task(_three_seconds(), name="slow")
    started = time.monotonic()

    await asyncio.wait_for(manager.drain_active_tasks(timeout=shutdown_timeout), timeout=5)

    assert time.monotonic() - started < 1.5  # CI p99 0.302 s
    assert task.cancelled()
    assert _warning("Draining the tasks of service 'svc'", shutdown_timeout) in warnings_said


# --- on_shutdown and the intake after a cancelled stop ------------------------------------------


@VALUES
async def test_on_shutdown_after_a_cancel_is_bounded_at_the_ceiling(
    shutdown_timeout, warnings_said
):
    manager = _manager(shutdown_timeout, on_shutdown=_three_seconds)
    started = time.monotonic()

    await asyncio.wait_for(
        manager._on_shutdown_after_a_cancel([], asyncio.CancelledError()), timeout=5
    )

    # CI p99 0.302 s. The lower bound tells the ceiling from a wait of no time.
    assert CEILING * 0.8 <= time.monotonic() - started < 1.5
    assert (
        _warning("on_shutdown of service 'svc' after a cancel", shutdown_timeout) in warnings_said
    )


@VALUES
async def test_the_intake_after_a_cancel_is_released_within_the_ceiling(
    shutdown_timeout, warnings_said
):
    manager = _manager(
        shutdown_timeout, stop_health_listener=_three_seconds, cancel_subscriptions=_three_seconds
    )
    started = time.monotonic()

    await asyncio.wait_for(manager._release_intake_after_a_cancel([]), timeout=5)

    # CI p99 0.603 s. The lower bound tells a ceiling per step from one shared by both.
    assert CEILING * 1.6 <= time.monotonic() - started < 2.0, "each step gets the ceiling"
    assert _warning("Releasing the intake of service 'svc'", shutdown_timeout) in warnings_said


async def test_CONTROL_none_still_sets_no_deadline_for_the_drain(warnings_said):
    """`None` is the one spelling of "no deadline": the drain waits for its task and warns of
    nothing."""
    manager = _manager(None)

    async def half_a_second() -> None:
        await asyncio.sleep(0.5)

    task = manager.spawn_supervised_task(half_a_second(), name="half")

    await asyncio.wait_for(manager.drain_active_tasks(timeout=None), timeout=5)

    assert task.done() and not task.cancelled()
    assert warnings_said == []

"""Shutdown's deadline: where it comes from, and what it decides.

A service stops by draining its in-flight tasks under a deadline. Two
properties decide whether that drain is correct, and neither is a duration:

* the deadline is the one the caller configured, not one the drain picked;
* work still running when the deadline passes is cancelled, and work that
  finishes before it is awaited rather than cancelled.

The drain takes its deadline from ``time.monotonic`` in its own module, so a
stand-in clock puts the test in charge of whether that deadline has passed.
Three readings, reaching the drain by different routes, and none of them
asserts on a duration.

The first replaces the drain with a recorder and asserts on the value ``stop()``
passed it. The recorder delegates to the real drain, which reads the clock once
to set a deadline, but with nothing in flight it returns on its first pass and
that reading decides nothing.

The second hands the drain a stand-in clock whose deadline has already passed, so it
takes the expired branch and never reaches ``wait_for``.

The third pins that clock so the deadline never passes, so the drain does reach
``wait_for``, and the bound it passes there is a real quarter second from the
event loop's own timer. Nothing asserts on that duration -- the assertions are
on whether the work finished or was cancelled.
"""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import lifecycle
from cliffracer.core.lifecycle import LifecycleManager

pytestmark = pytest.mark.unit


class _FakeClock:
    """Stands in for the ``time`` module, reading what the test hands it.

    Each reading is consumed once; the last one is held from then on, so a
    two-value clock answers the drain's deadline calculation and then every
    later check with the time the test chose.
    """

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def monotonic(self) -> float:
        if len(self._readings) > 1:
            return self._readings.pop(0)
        return self._readings[0]


@pytest.mark.asyncio
async def test_stop_drains_under_the_configured_shutdown_timeout() -> None:
    """The deadline the drain runs under is the service's own shutdown_timeout."""
    cfg = ServiceConfig(
        name="deadline_svc", shutdown_timeout=0.25, health_port=0, health_listener=False
    )
    svc = CliffracerService(cfg)

    seen: list[float | None] = []
    drain = svc.container.lifecycle.drain_active_tasks

    async def record(timeout: float | None = None) -> None:
        seen.append(timeout)
        await drain(timeout=timeout)

    svc.container.lifecycle.drain_active_tasks = record  # type: ignore[method-assign]

    await svc.stop()

    # A drain under a deadline of the drain's own choosing is the defect this
    # reads: the value has to arrive from the config, and be the only one.
    assert seen == [0.25]


@pytest.mark.asyncio
async def test_work_still_running_when_the_deadline_passes_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that outlives the deadline is cancelled rather than waited on."""
    cfg = ServiceConfig(name="expired_svc", health_port=0, health_listener=False)
    manager = LifecycleManager(config=cfg)

    cancelled = False
    finished = False

    async def never_finishes() -> None:
        nonlocal cancelled, finished
        try:
            await asyncio.Event().wait()
            finished = True
        except asyncio.CancelledError:
            cancelled = True
            raise

    task = manager.spawn_supervised_task(never_finishes(), name="hangs")
    await asyncio.sleep(0)

    # Reading 0.0 sets the deadline; 1000.0 is the drain's next look at the
    # clock, by which time the deadline it just set has passed.
    monkeypatch.setattr(lifecycle, "time", _FakeClock(0.0, 1000.0))
    await manager.drain_active_tasks(timeout=0.25)

    assert cancelled is True
    assert finished is False
    assert task.cancelled() is True


@pytest.mark.asyncio
async def test_work_finishing_before_the_deadline_is_awaited_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain waits for work inside its budget instead of cancelling at once."""
    cfg = ServiceConfig(name="inside_svc", health_port=0, health_listener=False)
    manager = LifecycleManager(config=cfg)

    release = asyncio.Event()
    cancelled = False
    finished = False

    async def finishes_when_released() -> None:
        nonlocal cancelled, finished
        try:
            await release.wait()
            finished = True
        except asyncio.CancelledError:
            cancelled = True
            raise

    manager.spawn_supervised_task(finishes_when_released(), name="finishes")
    await asyncio.sleep(0)

    # The clock never advances, so the deadline never passes: a drain that
    # cancels here cancelled work it still had budget for.
    monkeypatch.setattr(lifecycle, "time", _FakeClock(0.0))

    async def release_once_draining() -> None:
        await asyncio.sleep(0)
        release.set()

    releaser = asyncio.create_task(release_once_draining())
    await manager.drain_active_tasks(timeout=0.25)
    await releaser

    assert finished is True
    assert cancelled is False
    assert len(manager.active_tasks) == 0

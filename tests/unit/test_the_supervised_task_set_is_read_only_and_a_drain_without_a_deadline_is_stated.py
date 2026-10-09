"""The set of supervised tasks is kept in one place, and a drain with no deadline says so.

`LifecycleManager.active_tasks` returned the live set, so a caller could add or
remove a task and desynchronise it from the done-callbacks that maintain it. It
is now a snapshot: a `frozenset` taken when it is read.

`drain_active_tasks(timeout=None)` (what `shutdown_timeout=None` passes) and a
timeout that is not positive set no deadline. The drain waits and cancels
nothing, which `ServiceConfig` documents for `None`; the method's own docstring
now says it too, and these tests pin it.
"""

import asyncio
import inspect

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.lifecycle import LifecycleManager

pytestmark = pytest.mark.unit


def _manager() -> LifecycleManager:
    return LifecycleManager(config=ServiceConfig(name="lifecycle_svc"))


async def _sleep(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "finished"


async def test_the_tasks_a_caller_reads_cannot_be_added_to_or_removed_from():
    lm = _manager()
    task = lm.spawn_supervised_task(_sleep(0.01), name="t")

    try:
        with pytest.raises(AttributeError):
            lm.active_tasks.add(task)  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            lm.active_tasks.discard(task)  # type: ignore[attr-defined]
        assert task in lm.active_tasks
    finally:
        await lm.drain_active_tasks(timeout=1.0)


async def test_a_snapshot_taken_before_a_spawn_does_not_grow():
    lm = _manager()
    before = lm.active_tasks

    task = lm.spawn_supervised_task(_sleep(0.01), name="t")

    try:
        assert before == frozenset()
        assert task in lm.active_tasks
    finally:
        await lm.drain_active_tasks(timeout=1.0)
    assert lm.active_tasks == frozenset()


async def test_a_task_assigned_through_the_container_setter_is_still_supervised_after():
    """The setter rebinds the set the done-callbacks discard from by attribute, so a
    task spawned after it is tracked and removed as before."""
    svc = CliffracerService(ServiceConfig(name="setter_svc"))
    lifecycle = svc.container.lifecycle
    stray = asyncio.ensure_future(_sleep(0))
    svc.container._active_tasks = {stray}

    assert stray in svc.container._active_tasks
    task = lifecycle.spawn_supervised_task(_sleep(0.01), name="after")
    assert task in lifecycle.active_tasks
    await task
    await asyncio.sleep(0)

    assert task not in lifecycle.active_tasks
    await stray


@pytest.mark.parametrize("timeout", [None, 0, -1.0], ids=["none", "zero", "negative"])
async def test_a_drain_with_no_deadline_waits_for_the_task_and_cancels_nothing(timeout):
    lm = _manager()
    task = lm.spawn_supervised_task(_sleep(0.2), name="slow")

    await asyncio.wait_for(lm.drain_active_tasks(timeout=timeout), timeout=5)

    assert task.done() and not task.cancelled()
    assert task.result() == "finished"
    assert lm.active_tasks == frozenset()


async def test_CONTROL_a_positive_timeout_still_cancels_what_overruns_it():
    """Without this the test above could pass because nothing ever cancels."""
    lm = _manager()
    task = lm.spawn_supervised_task(_sleep(30), name="stuck")

    await lm.drain_active_tasks(timeout=0.05)

    assert task.cancelled()


def test_the_drain_documents_what_no_deadline_means():
    doc = inspect.getdoc(LifecycleManager.drain_active_tasks) or ""

    assert "sets no deadline" in doc
    assert "cancels nothing" in doc


def test_a_manager_carries_no_event_loop_it_never_reads():
    assert not hasattr(_manager(), "_loop")

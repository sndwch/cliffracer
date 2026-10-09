"""A bounded dispatch returns its permit even when the task never runs.

The core event path acquires a permit in the callback and hands the work to a
spawned task; the JetStream and the two RPC paths spawn first and their task
takes its own permit, so a task of theirs that never runs holds none. A task cancelled before its first execution never runs
its body, so a release written inside that body is never reached and the permit
is gone for the life of the process. Five such cancellations empty a bound of
five and every later request waits forever.

Cancellation itself is not the defect: asyncio refunds a cancel that lands
during `acquire`, and a task cancelled after it has started runs its unwind.
The defect is the window between the spawn and the task's first step, which is
why the permit is returned by a done-callback -- that runs exactly once for
every task, whichever way it ended.

The four paths are covered as one parametrised case rather than one test each,
because they are the same two lines repeated and a fix that reached only some
of them is the failure worth catching. They are the callbacks under `dispatch/`
that a subscription calls.
"""

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit

BOUND = 5
PATTERN = "probe.subject"


def _msg() -> Any:
    msg = AsyncMock()
    msg.subject = PATTERN
    msg.data = b"{}"
    msg.headers = None
    msg.reply = "_INBOX.probe"
    msg.metadata = MagicMock(num_delivered=1)
    return msg


def _service() -> CliffracerService:
    """A service with every bounded path's limit set to the same bound."""
    return CliffracerService(
        ServiceConfig(
            name="permit_probe",
            health_port=0,
            health_listener=False,
            max_rpc_concurrency=BOUND,
            max_event_concurrency=BOUND,
            max_async_rpc_concurrency=BOUND,
            jetstream_enabled=False,
        )
    )


async def _events_dispatch(svc):
    callback = svc.container.dispatcher.events.make_event_callback(PATTERN)
    return svc.container.dispatcher.events.get_event_semaphore(), callback


async def _jetstream_dispatch(svc):
    callback = svc.container.dispatcher.jetstream.make_event_callback(PATTERN)
    return svc.container.dispatcher.events.get_event_semaphore(), callback


async def _rpc_dispatch(svc):
    rpc = svc.container.dispatcher.rpc
    return rpc.get_rpc_semaphore(), rpc.on_rpc_request


async def _async_rpc_dispatch(svc):
    rpc = svc.container.dispatcher.rpc
    return rpc.get_async_rpc_semaphore(), rpc.on_async_request


PATHS = {
    "events.make_event_callback": _events_dispatch,
    "jetstream.make_event_callback": _jetstream_dispatch,
    "rpc.on_rpc_request": _rpc_dispatch,
    "rpc.on_async_request": _async_rpc_dispatch,
}


# How many permits the entry path has taken by the time it returns. The JetStream and RPC callbacks
# take none: their task takes the permit once it runs (the JetStream one pulsing the message while
# it waits), so a task that never starts holds nothing, and the check below is that it also loses
# nothing.
PERMITS_TAKEN_BY_THE_CALL = {
    "jetstream.make_event_callback": 0,
    "rpc.on_rpc_request": 0,
    "rpc.on_async_request": 0,
}


@pytest.mark.parametrize("path_name", sorted(PATHS))
async def test_a_task_cancelled_before_it_runs_returns_its_permit(path_name: str):
    """Cancel in the window between the spawn and the first step, then count."""
    svc = _service()
    svc._discover_handlers()
    svc.container.lifecycle._running = True
    svc.container.nc = AsyncMock()

    sem, entry = await PATHS[path_name](svc)
    assert sem is not None, f"{path_name}: no semaphore, so the path is unbounded here"
    assert sem._value == BOUND, f"{path_name}: the bound did not start full"

    before = set(asyncio.all_tasks())
    await entry(_msg())
    spawned = [t for t in asyncio.all_tasks() - before if not t.done()]
    assert spawned, f"{path_name}: the call spawned no task, so there is nothing to cancel"
    taken = PERMITS_TAKEN_BY_THE_CALL.get(path_name, 1)
    assert sem._value == BOUND - taken, f"{path_name}: expected {taken} permit(s) taken by the call"

    # "Before its first execution" is established by construction here -- there is
    # no await between the spawn and the cancel -- but construction is a property
    # of this file, not of the entry path. An await added inside an entry path
    # after its spawn would turn this into a cancel-after-start test that still
    # passes, so the state is asserted rather than reasoned about.
    for task in spawned:
        state = inspect.getcoroutinestate(task.get_coro())
        assert state == "CORO_CREATED", (
            f"{path_name}: the task is {state}, not CORO_CREATED, so it has already "
            "taken a step and this is no longer the window under test. Something "
            "now awaits between the spawn and the cancel."
        )
    for task in spawned:
        task.cancel()
    await asyncio.gather(*spawned, return_exceptions=True)
    await asyncio.sleep(0)

    assert sem._value == BOUND, (
        f"{path_name}: a task cancelled before its first execution kept the permit "
        f"({sem._value} of {BOUND} available). Release it from the task's "
        "done-callback rather than from inside the coroutine."
    )


@pytest.mark.parametrize("path_name", sorted(PATHS))
async def test_a_task_that_has_taken_a_step_returns_its_permit_once(path_name: str):
    """The control: the case that already worked must keep working.

    Without this, moving the release to a done-callback could release twice --
    once in the unwind and once in the callback -- and push the count above the
    bound. asyncio.Semaphore does not cap its value, so that would read as a
    larger bound rather than as an error.

    What this measurably drives is narrower than "cancelled after it starts".
    After one loop iteration five of the six paths have already reached
    CORO_CLOSED with a mocked client, so their `cancel()` lands on a finished
    task and does nothing; only jetstream is still CORO_SUSPENDED. The property
    it pins is therefore "a task that ran its own release path releases exactly
    once", which is what a double release would break either way. The state is
    asserted so that it cannot silently become a second copy of the
    never-started case.
    """
    svc = _service()
    svc._discover_handlers()
    svc.container.lifecycle._running = True
    svc.container.nc = AsyncMock()

    sem, entry = await PATHS[path_name](svc)
    before = set(asyncio.all_tasks())
    await entry(_msg())
    spawned = [t for t in asyncio.all_tasks() - before if not t.done()]

    await asyncio.sleep(0)  # let each spawned task take its first step
    for task in spawned:
        state = inspect.getcoroutinestate(task.get_coro())
        assert state != "CORO_CREATED", (
            f"{path_name}: the task is still CORO_CREATED, so this control has "
            "become a second copy of the never-started case and no longer guards "
            "against a double release."
        )
    for task in spawned:
        task.cancel()
    await asyncio.gather(*spawned, return_exceptions=True)
    await asyncio.sleep(0)

    assert sem._value == BOUND, (
        f"{path_name}: the permit count is {sem._value}, not {BOUND}. Below the "
        "bound loses a permit; above it releases twice."
    )


@pytest.mark.parametrize("path_name", sorted(PATHS))
async def test_a_task_that_completes_returns_its_permit(path_name: str):
    """And the ordinary case, so the fix is not read from cancellation alone."""
    svc = _service()
    svc._discover_handlers()
    svc.container.lifecycle._running = True
    svc.container.nc = AsyncMock()

    sem, entry = await PATHS[path_name](svc)
    before = set(asyncio.all_tasks())
    await entry(_msg())
    spawned = list(asyncio.all_tasks() - before)
    await asyncio.gather(*spawned, return_exceptions=True)
    await asyncio.sleep(0)

    assert sem._value == BOUND, (
        f"{path_name}: a completed dispatch left the count at {sem._value} of {BOUND}"
    )


@pytest.mark.parametrize("path_name", sorted(PATHS))
async def test_the_bound_survives_five_cancellations_in_the_window(path_name: str):
    """The hang, in miniature: a bound of five emptied by five lost permits.

    One lost permit is a smaller `assert 3 == 5`; five is a semaphore nothing
    can acquire again, which is what the storm test's 120s timeout was. This is
    the only case that shows the semaphore still usable -- `wait_for(acquire)`
    rather than a count -- so it runs on every path and not just the one the
    storm test drives.
    """
    svc = _service()
    svc._discover_handlers()
    svc.container.lifecycle._running = True
    svc.container.nc = AsyncMock()

    sem, entry = await PATHS[path_name](svc)
    for _ in range(BOUND):
        before = set(asyncio.all_tasks())
        await entry(_msg())
        spawned = [t for t in asyncio.all_tasks() - before if not t.done()]
        for task in spawned:
            task.cancel()
        await asyncio.gather(*spawned, return_exceptions=True)
        await asyncio.sleep(0)

    assert sem._value == BOUND, (
        f"{path_name}: after {BOUND} cancellations in the spawn window the bound "
        f"is {sem._value} of {BOUND}; at 0 every later request waits forever"
    )
    await asyncio.wait_for(sem.acquire(), timeout=1.0)
    sem.release()

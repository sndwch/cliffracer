"""A synchronous host that does not run the coroutine it was given closes it.

`loop_host.run` takes a coroutine already made: `run_forever` passes `self.run()` and a
service's `run` passes its own `_run()`. Inside a running loop the host refuses, and when it
cannot make its loop it fails. Either way the coroutine is closed. Left open, it is never
awaited, and Python warns about it at whichever later garbage collection frees it, inside an
unrelated test.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import signal
import warnings

import pytest

from cliffracer import CliffracerService
from cliffracer.core import ServiceConfig, loop_host
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

pytestmark = pytest.mark.unit


async def _work() -> int:
    return 7


@pytest.fixture
def handlers(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))
    return installed


def _never_awaited(action) -> list[str]:
    """Run `action`, drop every reference it made, collect, and return the warnings.

    Garbage left by earlier tests is collected first, so only `action`'s own is reported.
    Each warning is its first line: with coroutine origin tracking on, the message goes on
    to say where the coroutine was created.
    """
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        action()
        gc.collect()
    return [str(w.message).splitlines()[0] for w in caught if "never awaited" in str(w.message)]


def _refused(make_coroutine):
    def action() -> None:
        try:
            loop_host.run(make_coroutine())
        except RuntimeError as refused:
            assert "cannot run inside a running event loop" in str(refused)
        else:
            raise AssertionError("the host ran inside a running loop")

    return action


async def test_a_coroutine_refused_inside_a_running_loop_is_closed():
    main = _work()
    with pytest.raises(RuntimeError, match="cannot run inside a running event loop"):
        loop_host.run(main)
    assert inspect.getcoroutinestate(main) == inspect.CORO_CLOSED


async def test_a_refusal_leaves_no_coroutine_unawaited_for_a_later_collection():
    assert _never_awaited(_refused(_work)) == []


async def test_CONTROL_a_coroutine_dropped_unrun_is_reported_by_the_collection():
    assert _never_awaited(lambda: _work()) == ["coroutine '_work' was never awaited"]


def test_a_coroutine_whose_loop_cannot_be_made_is_closed(monkeypatch):
    def no_loop(self):
        raise OSError("no loop for you")

    monkeypatch.setattr(asyncio.Runner, "get_loop", no_loop)
    main = _work()
    with pytest.raises(OSError, match="no loop for you"):
        loop_host.run(main)
    assert inspect.getcoroutinestate(main) == inspect.CORO_CLOSED


def test_a_coroutine_the_host_runs_is_run_to_its_result():
    main = _work()
    assert loop_host.run(main) == 7
    assert inspect.getcoroutinestate(main) == inspect.CORO_CLOSED


@pytest.mark.parametrize(
    "make", [lambda: ServiceRunner(object), ServiceOrchestrator], ids=["runner", "orchestrator"]
)
async def test_an_entry_point_refused_inside_a_running_loop_closes_its_run(
    handlers, monkeypatch, make
):
    owner = make()
    made = []
    real = type(owner).run

    def recorded(self):
        made.append(real(self))
        return made[-1]

    monkeypatch.setattr(type(owner), "run", recorded)
    with pytest.raises(SystemExit):
        owner.run_forever()
    assert [inspect.getcoroutinestate(c) for c in made] == [inspect.CORO_CLOSED]


class Quiet(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="quiet"))


async def test_a_service_run_refused_inside_a_running_loop_leaves_nothing_unawaited():
    service = Quiet()

    def action() -> None:
        with pytest.raises(RuntimeError, match="cannot run inside a running event loop"):
            service.run()

    assert _never_awaited(action) == []

"""A supervised task that crashes is reported by the supervisor, and only there.

Every background task the framework runs -- RPC, event and JetStream dispatch,
pull loops -- is spawned through `_spawn_supervised_task`, and its done-callback
is the one place that retrieves the task's exception and logs it under the
task's name. Without that, the exception surfaces only when the task object is
garbage-collected, as asyncio's "Task exception was never retrieved" on the loop
exception handler, with nothing in the service's own log.

HOW THESE TESTS WAIT, because the obvious way cannot fail. Each crashed task is
waited on with `asyncio.wait`, never `asyncio.gather(..., return_exceptions=
True)`: `gather` retrieves every task's exception itself, so a supervisor that
retrieves nothing still leaves nothing for the loop to report. Nor does any test
call `task.exception()` -- that is the test doing the supervisor's job. And the
channel is the loop's exception handler, not the `warnings` module, which asyncio
never writes this to.

The last CONTROL shows the handler really receives an unretrieved exception from
an unsupervised task, so an empty handler elsewhere means the supervisor retrieved
it, not that the channel was deaf.
"""

import asyncio
import gc
from typing import Any

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit

UNRETRIEVED = "Task exception was never retrieved"


class Crash(Exception):
    pass


async def _raise(exc: BaseException) -> None:
    await asyncio.sleep(0.001)
    raise exc


class _ErrorRecords:
    """The ERROR records the log receives while installed, as the sinks see them.

    Read from a real sink rather than from a patched logger method: the message
    is the formatted text and the record carries the exception that was logged,
    so what is asserted is what an operator reads.
    """

    def __init__(self) -> None:
        self.records: list[Any] = []

    def __enter__(self) -> "_ErrorRecords":
        self._sink = logger.add(lambda message: self.records.append(message.record), level="ERROR")
        return self

    def __exit__(self, *exc_info: object) -> None:
        logger.remove(self._sink)


class _LoopReports:
    """Everything the running loop's exception handler receives while installed."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __enter__(self) -> "_LoopReports":
        self._loop = asyncio.get_running_loop()
        self._previous = self._loop.get_exception_handler()
        self._loop.set_exception_handler(lambda _loop, ctx: self.messages.append(ctx["message"]))
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._loop.set_exception_handler(self._previous)


async def _let_go(tasks: list[asyncio.Task]) -> None:
    """Wait for the tasks without retrieving anything, then drop them.

    A task reports an unretrieved exception from its finaliser, so the last
    references go and the collector runs before anything is read.
    """
    await asyncio.wait(tasks)
    tasks.clear()
    await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)


async def test_a_crashed_supervised_task_is_logged_under_its_name():
    svc = CliffracerService(ServiceConfig(name="supervised"))
    crash = Crash("kaboom")

    with _ErrorRecords() as errors:
        await _let_go([svc.container._spawn_supervised_task(_raise(crash), name="doomed")])

    assert len(errors.records) == 1, errors.records
    (record,) = errors.records
    assert "doomed" in record["message"] and "kaboom" in record["message"], record["message"]
    assert record["exception"].value is crash


async def test_a_crashed_supervised_task_reaches_no_loop_exception_handler():
    svc = CliffracerService(ServiceConfig(name="supervised"))

    with _LoopReports() as reports:
        await _let_go([svc.container._spawn_supervised_task(_raise(Crash("x")), name="doomed")])

    assert reports.messages == []


async def test_a_batch_of_crashes_is_each_reported_and_none_is_lost():
    svc = CliffracerService(ServiceConfig(name="supervised"))
    kinds = [RuntimeError, ValueError, KeyError, ZeroDivisionError, Crash]

    with _LoopReports() as reports, _ErrorRecords() as errors:
        tasks = [
            svc.container._spawn_supervised_task(
                _raise(kinds[i % len(kinds)](f"failure {i}")), name=f"task_{i}"
            )
            for i in range(50)
        ]
        await _let_go(tasks)

    logged = sorted(record["message"].split("'")[1] for record in errors.records)
    assert logged == sorted(f"task_{i}" for i in range(50))
    assert reports.messages == []
    assert svc.container._active_tasks == set()


async def test_CONTROL_a_cancelled_supervised_task_is_not_a_crash():
    svc = CliffracerService(ServiceConfig(name="supervised"))

    with _LoopReports() as reports, _ErrorRecords() as errors:
        task = svc.container._spawn_supervised_task(asyncio.sleep(10), name="stopped")
        await asyncio.sleep(0)
        task.cancel()
        await _let_go([task])
        del task
        gc.collect()

    assert errors.records == []
    assert reports.messages == []


async def test_CONTROL_an_unsupervised_crash_does_reach_the_loop_exception_handler():
    """The channel the tests above read is live: with no supervisor to retrieve
    the exception, the same crash is reported there."""
    with _LoopReports() as reports:
        await _let_go([asyncio.get_running_loop().create_task(_raise(Crash("x")))])

    assert reports.messages == [UNRETRIEVED]

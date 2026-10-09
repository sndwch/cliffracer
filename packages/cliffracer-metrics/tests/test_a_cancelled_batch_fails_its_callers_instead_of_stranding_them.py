"""A batch that is cancelled answers the callers waiting on it.

`_execute_batch` resolved its callers' futures on success and on an `Exception`, and a
`CancelledError` is neither: it went through the `finally` and left every `add_item` caller
awaiting a future nothing would ever resolve, with no log line that the items were lost.
"""

import asyncio
import weakref

import pytest
from cliffracer_metrics import BatchProcessor
from loguru import logger

pytestmark = pytest.mark.unit


async def _running_batch_tasks(bp: BatchProcessor, expected: int) -> list[asyncio.Task]:
    for _ in range(200):
        if len(bp._batch_tasks) >= expected:
            return list(bp._batch_tasks)
        await asyncio.sleep(0.005)
    raise AssertionError(f"{len(bp._batch_tasks)} batch tasks, expected {expected}")


async def _wait_until(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition never held")


async def test_a_cancelled_batch_fails_every_caller_it_had_not_answered():
    started = asyncio.Event()

    async def hangs(items):
        started.set()
        await asyncio.sleep(60)

    bp = BatchProcessor(batch_size=2, batch_timeout_ms=1000)
    callers = [
        asyncio.create_task(bp.add_item("k", 1, hangs)),
        asyncio.create_task(bp.add_item("k", 2, hangs)),
    ]
    await started.wait()
    for task in await _running_batch_tasks(bp, 1):
        task.cancel()

    outcomes = await asyncio.wait_for(asyncio.gather(*callers, return_exceptions=True), timeout=5)

    assert all(isinstance(o, RuntimeError) and "interrupted" in str(o) for o in outcomes), outcomes
    assert "CancelledError" in str(outcomes[0])


async def test_a_group_the_batch_had_already_answered_keeps_its_result():
    reached_second = asyncio.Event()

    def first(items):
        return [i * 10 for i in items]

    async def second(items):
        reached_second.set()
        await asyncio.sleep(60)

    bp = BatchProcessor(batch_size=2, batch_timeout_ms=1000)
    answered = asyncio.create_task(bp.add_item("k", 1, first, results="per_item"))
    stranded = asyncio.create_task(bp.add_item("k", 2, second))
    await reached_second.wait()
    for task in await _running_batch_tasks(bp, 1):
        task.cancel()

    assert await asyncio.wait_for(answered, timeout=5) == 10
    with pytest.raises(RuntimeError, match="interrupted"):
        await asyncio.wait_for(stranded, timeout=5)


async def test_the_batch_logs_how_many_callers_it_failed():
    started = asyncio.Event()

    async def hangs(items):
        started.set()
        await asyncio.sleep(60)

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"))
    bp = BatchProcessor(batch_size=1)
    caller = asyncio.create_task(bp.add_item("k", 1, hangs))
    try:
        await started.wait()
        for task in await _running_batch_tasks(bp, 1):
            task.cancel()
        await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), timeout=5)
    finally:
        logger.remove(sink)

    assert "ERROR Batch interrupted by CancelledError: 1 of 1 callers were failed" in lines, lines


class _InOrder(weakref.WeakSet):
    """The batch tasks, in the order they were made."""

    def __init__(self) -> None:
        super().__init__()
        self.made: list[asyncio.Task] = []

    def add(self, task) -> None:
        self.made.append(task)
        super().add(task)


async def test_a_batch_cancelled_while_it_waits_for_a_free_slot_fails_its_callers():
    release_first = asyncio.Event()
    first_started = asyncio.Event()

    async def blocks(items):
        first_started.set()
        await release_first.wait()
        return "done"

    bp = BatchProcessor(batch_size=1, max_concurrent_batches=1)
    bp._batch_tasks = _InOrder()
    first = asyncio.create_task(bp.add_item("a", 1, blocks))
    await first_started.wait()
    second = asyncio.create_task(bp.add_item("b", 2, blocks))
    await _wait_until(lambda: len(bp._batch_tasks.made) == 2)
    bp._batch_tasks.made[1].cancel()  # the one still waiting for the slot

    with pytest.raises(RuntimeError, match="interrupted"):
        await asyncio.wait_for(second, timeout=5)
    release_first.set()
    assert await asyncio.wait_for(first, timeout=5) == "done"
    await _wait_until(lambda: bp._concurrent_batches == 0)


async def test_the_concurrency_count_returns_to_zero_after_a_cancelled_batch():
    started = asyncio.Event()

    async def hangs(items):
        started.set()
        await asyncio.sleep(60)

    bp = BatchProcessor(batch_size=1)
    caller = asyncio.create_task(bp.add_item("k", 1, hangs))
    await started.wait()
    assert bp._concurrent_batches == 1
    for task in await _running_batch_tasks(bp, 1):
        task.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    await _wait_until(lambda: bp._concurrent_batches == 0)


async def test_CONTROL_a_processor_that_raises_still_fails_only_its_own_group_with_its_error():
    def boom(items):
        raise ValueError("bad items")

    bp = BatchProcessor(batch_size=1)

    with pytest.raises(ValueError, match="bad items"):
        await bp.add_item("k", 1, boom)


async def test_the_cancelled_batch_task_still_reports_that_it_was_cancelled():
    """Answering the callers must not swallow the cancellation: whoever cancelled the task, a
    supervisor or a loop teardown, still sees it cancelled."""
    started = asyncio.Event()

    async def hangs(items):
        started.set()
        await asyncio.sleep(60)

    bp = BatchProcessor(batch_size=1)
    caller = asyncio.create_task(bp.add_item("k", 1, hangs))
    await started.wait()
    tasks = await _running_batch_tasks(bp, 1)
    for task in tasks:
        task.cancel()

    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
    await asyncio.gather(caller, return_exceptions=True)

    assert all(isinstance(r, asyncio.CancelledError) for r in results), results
    assert all(task.cancelled() for task in tasks)

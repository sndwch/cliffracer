"""`BatchProcessor.shutdown()` returns only after the work it was given is done.

A metrics flush leans on this: a shutdown that returned while a batch was still
mid-flight would drop that batch's writes. The tests drive the processor through
`add_item`, so there is real batch state to flush, drain and clear, and read what
happened to that state, not a flag `shutdown()` sets on its first line.
"""

import asyncio

import pytest
from cliffracer_metrics import BatchProcessor

pytestmark = pytest.mark.unit


async def _until(condition, what: str) -> None:
    """Yield to the loop until `condition()` holds, bounded so a miss fails by name."""
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


async def test_shutdown_waits_for_a_batch_that_is_mid_flight():
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = []

    async def processor(items):
        entered.set()
        await release.wait()
        finished.append(list(items))
        return ["done"] * len(items)

    bp = BatchProcessor(batch_size=1, batch_timeout_ms=60_000)
    adder = asyncio.create_task(bp.add_item("k", "a", processor, results="per_item"))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    shutdown = asyncio.create_task(bp.shutdown())
    await asyncio.sleep(0.05)
    still_waiting = not shutdown.done()

    release.set()
    await asyncio.wait_for(shutdown, timeout=2.0)

    assert still_waiting, "shutdown() returned while a batch was still being processed"
    assert finished == [["a"]]
    assert await asyncio.wait_for(adder, timeout=2.0) == "done"


async def test_shutdown_flushes_a_batch_that_has_not_filled_or_timed_out():
    seen = []

    async def processor(items):
        seen.append(list(items))
        return ["flushed"] * len(items)

    bp = BatchProcessor(batch_size=100, batch_timeout_ms=60_000)
    adder = asyncio.create_task(bp.add_item("k", "a", processor, results="per_item"))
    await _until(lambda: bp.get_stats()["pending_batches"] == 1, "the item to be batched")

    await asyncio.wait_for(bp.shutdown(), timeout=2.0)

    assert seen == [["a"]]
    assert await asyncio.wait_for(adder, timeout=2.0) == "flushed"


async def test_shutdown_leaves_no_batch_state_behind():
    async def processor(items):
        return list(items)

    bp = BatchProcessor(batch_size=100, batch_timeout_ms=60_000)
    adders = [
        asyncio.create_task(bp.add_item(f"k{i}", i, processor, results="per_item"))
        for i in range(3)
    ]
    await _until(lambda: bp.get_stats()["pending_batches"] == 3, "three batches to be pending")
    assert len(bp._batches) == 3 and len(bp._batch_futures) == 3

    await asyncio.wait_for(bp.shutdown(), timeout=2.0)

    assert await asyncio.wait_for(asyncio.gather(*adders), timeout=2.0) == [0, 1, 2]
    assert len(bp._batches) == 0, list(bp._batches)
    assert len(bp._batch_futures) == 0, list(bp._batch_futures)


async def test_CONTROL_the_batch_state_the_other_tests_read_is_filled_by_add_item():
    """Before shutdown the same reads are non-zero, so a zero afterwards means something."""

    async def processor(items):
        return list(items)

    bp = BatchProcessor(batch_size=100, batch_timeout_ms=60_000)
    adder = asyncio.create_task(bp.add_item("k", "a", processor, results="per_item"))
    await _until(lambda: bp.get_stats()["pending_batches"] == 1, "the item to be batched")

    assert len(bp._batches) == 1
    assert len(bp._batch_futures) == 1

    await asyncio.wait_for(bp.shutdown(), timeout=2.0)
    await asyncio.wait_for(adder, timeout=2.0)


async def test_an_item_added_after_shutdown_is_refused():
    async def processor(items):
        return list(items)

    bp = BatchProcessor(batch_size=1, batch_timeout_ms=60_000)
    await bp.shutdown()

    with pytest.raises(RuntimeError, match="shutting down"):
        await bp.add_item("k", "a", processor, results="per_item")

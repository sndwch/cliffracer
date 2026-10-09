"""A cancelled dispatch is not a handler error, and `items_per_second` is a rate in wall-clock time.

`MetricsExtension` counted anything that was not a refusal as an error, so a shutdown that cancelled
in-flight handlers, or a timeout that cancelled one, put crashes on /health that never happened.
`BatchProcessor` divided the items by the sum of each batch's own duration, which counts overlapping
batches twice, so four concurrent batches reported a quarter of the rate the processor sustained.
"""

import asyncio

import pytest
from cliffracer_metrics import BatchProcessor, MetricsExtension

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    metrics = MetricsExtension()


async def _started() -> Svc:
    svc = Svc(ServiceConfig(name="counted", health_port=0))
    await svc.container._setup_extensions()
    return svc


def _ctx(kind: str = "rpc") -> WorkerContext:
    return WorkerContext(
        kind=kind, subject="counted.rpc.work", headers={}, correlation_id="c", payload={}
    )


async def _one(svc: Svc, exc: BaseException | None, kind: str = "rpc") -> None:
    ctx = _ctx(kind)
    await svc.metrics.worker_setup(ctx)
    await svc.metrics.worker_result(ctx, None, exc)


async def test_a_cancelled_dispatch_is_counted_as_cancelled_and_as_neither_error_nor_rejection():
    svc = await _started()

    await _one(svc, asyncio.CancelledError())

    stats = svc.metrics.health_details()["rpc"]
    assert (stats["count"], stats["errors"], stats["rejected"], stats["cancelled"]) == (1, 0, 0, 1)


async def test_each_outcome_is_counted_in_its_own_column_only():
    svc = await _started()

    await _one(svc, None)
    await _one(svc, RuntimeError("the handler broke"))
    await _one(svc, RejectMessage("not allowed"))
    await _one(svc, asyncio.CancelledError())

    stats = svc.metrics.health_details()["rpc"]
    assert (stats["count"], stats["errors"], stats["rejected"], stats["cancelled"]) == (4, 1, 1, 1)


async def test_a_handler_cancelled_in_the_pipeline_is_counted_as_cancelled():
    svc = await _started()
    pipeline = ExtensionPipeline([svc.metrics])

    async def cancelled() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await pipeline.run_worker(_ctx("timer"), cancelled)

    stats = svc.metrics.health_details()["timer"]
    assert (stats["errors"], stats["cancelled"]) == (0, 1)


async def test_the_kinds_are_counted_apart():
    svc = await _started()

    await _one(svc, asyncio.CancelledError(), "event")

    details = svc.metrics.health_details()
    assert set(details) == {"event"} and details["event"]["cancelled"] == 1


async def _run(bp: BatchProcessor, count: int, seconds: float) -> None:
    async def processor(items):
        await asyncio.sleep(seconds)
        return "done"

    await asyncio.gather(*(bp.add_item(f"k{i}", i, processor) for i in range(count)))


async def test_batches_that_overlap_are_counted_once_in_the_rate():
    """Four 50 ms batches at once sustain about 80 items a second, not about 20."""
    bp = BatchProcessor(batch_size=1, max_concurrent_batches=4)

    await _run(bp, 4, 0.05)

    stats = bp.get_stats()
    assert 45 < stats["items_per_second"] < 100, stats["items_per_second"]
    assert stats["processing_time_total_ms"] >= 190, "the sum of the batches' own durations"


async def test_the_time_between_batches_is_not_in_the_rate():
    bp = BatchProcessor(batch_size=1, max_concurrent_batches=1)

    await _run(bp, 1, 0.05)
    await asyncio.sleep(0.3)
    await _run(bp, 1, 0.05)

    assert 12 < bp.get_stats()["items_per_second"] < 25, bp.get_stats()["items_per_second"]


async def test_resetting_the_stats_restarts_the_rate():
    bp = BatchProcessor(batch_size=1, max_concurrent_batches=1)
    await _run(bp, 1, 0.05)

    bp.reset_stats()

    assert bp.get_stats()["items_per_second"] == 0
    await _run(bp, 1, 0.05)
    assert 14 < bp.get_stats()["items_per_second"] < 27, bp.get_stats()["items_per_second"]


async def test_staggered_overlapping_batches_are_busy_from_the_first_start_to_the_last_end():
    """A starts at 0 and runs 80 ms, B starts at 40 and runs 40 ms: busy 80 ms, two items, 25/s."""
    bp = BatchProcessor(batch_size=1, max_concurrent_batches=2)

    async def slow(items):
        await asyncio.sleep(0.08)

    async def quick(items):
        await asyncio.sleep(0.04)

    first = asyncio.create_task(bp.add_item("a", 1, slow))
    await asyncio.sleep(0.04)
    second = asyncio.create_task(bp.add_item("b", 2, quick))
    await asyncio.gather(first, second)

    assert 18 < bp.get_stats()["items_per_second"] < 32, bp.get_stats()["items_per_second"]

"""The batch processor, the connection pool and the counters do what their docstrings say."""

import asyncio
import builtins
import types
from unittest.mock import MagicMock, patch

import pytest
from cliffracer_metrics import (
    BatchProcessor,
    MetricsExtension,
    OptimizedNATSConnection,
    PerformanceMetrics,
    PoolExtension,
)
from cliffracer_metrics import batch_processor as batch_module
from cliffracer_metrics import metrics as metrics_module

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import WorkerContext
from cliffracer.core.validation import NumericBounds

pytestmark = pytest.mark.unit


# --- BatchProcessor ---------------------------------------------------------


class _Unhashable:
    """A processor that defines equality and so cannot be hashed."""

    def __init__(self, tag):
        self.tag = tag
        self.calls = []

    def __eq__(self, other):
        return self is other

    __hash__ = None

    def __call__(self, items):
        self.calls.append(list(items))
        return self.tag


async def test_two_processors_that_cannot_be_hashed_are_called_apart():
    bp = BatchProcessor(batch_size=2)
    first, second = _Unhashable("one"), _Unhashable("two")
    got = await asyncio.wait_for(
        asyncio.gather(bp.add_item("k", 1, first), bp.add_item("k", 2, second)), 5
    )
    assert got == ["one", "two"]
    assert (first.calls, second.calls) == ([[1]], [[2]])


@pytest.mark.parametrize("bad", [0, -1, "3", 1.5])
def test_max_concurrent_batches_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError):
        BatchProcessor(max_concurrent_batches=bad)


def test_max_concurrent_batches_may_be_the_ceiling_and_not_past_it():
    ceiling = NumericBounds.MAX_CONCURRENT
    assert BatchProcessor(max_concurrent_batches=ceiling).max_concurrent_batches == ceiling
    with pytest.raises(ValueError):
        BatchProcessor(max_concurrent_batches=ceiling + 1)


def test_a_new_processor_reports_zero_for_every_statistic():
    stats = BatchProcessor().get_stats()
    assert (
        stats["total_items_processed"],
        stats["total_batches_processed"],
        stats["average_batch_size"],
        stats["processing_time_total_ms"],
        stats["items_per_second"],
    ) == (0, 0, 0, 0, 0)


def test_the_stats_reset_on_a_processor_that_has_run_nothing():
    bp = BatchProcessor()
    bp.reset_stats()
    assert bp.get_stats()["total_batches_processed"] == 0


async def test_a_batch_that_never_fills_is_processed_when_its_timeout_ends():
    bp = BatchProcessor(batch_size=10, batch_timeout_ms=20)
    assert await asyncio.wait_for(bp.add_item("k", 1, lambda items: len(items)), 2) == 1


async def test_no_more_batches_run_at_once_than_max_concurrent_batches():
    bp = BatchProcessor(batch_size=1, max_concurrent_batches=1)
    running, peak = 0, 0

    async def processor(items):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    await asyncio.wait_for(
        asyncio.gather(*(bp.add_item(f"k{i}", i, processor) for i in range(3))), 5
    )
    assert peak == 1


async def _second_caller_with_the_first_cancelled(results, outcome):
    """Two callers in one group; the first is cancelled while the processor runs."""
    bp = BatchProcessor(batch_size=2)
    go = asyncio.Event()

    async def processor(items):
        await go.wait()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    first = asyncio.create_task(bp.add_item("k", 1, processor, results=results))
    await asyncio.sleep(0)
    second = asyncio.create_task(bp.add_item("k", 2, processor, results=results))
    await asyncio.sleep(0.01)
    first.cancel()
    await asyncio.sleep(0)
    go.set()
    return await asyncio.wait_for(second, 5)


async def test_a_cancelled_caller_leaves_the_rest_of_its_group_the_shared_result():
    assert await _second_caller_with_the_first_cancelled("shared", "done") == "done"


async def test_a_cancelled_caller_leaves_the_rest_of_its_group_their_own_results():
    assert await _second_caller_with_the_first_cancelled("per_item", ["r1", "r2"]) == "r2"


async def test_a_cancelled_caller_leaves_the_rest_of_its_group_the_processors_error():
    with pytest.raises(ValueError, match="processor broke"):
        await _second_caller_with_the_first_cancelled("shared", ValueError("processor broke"))


async def test_every_caller_in_a_group_whose_processor_raises_gets_its_error():
    bp = BatchProcessor(batch_size=2)

    def processor(items):
        raise ValueError("processor broke")

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            bp.add_item("k", 1, processor), bp.add_item("k", 2, processor), return_exceptions=True
        ),
        2,
    )
    assert [type(o) for o in outcomes] == [ValueError, ValueError]
    assert all(str(o) == "processor broke" for o in outcomes)


async def test_a_batchs_duration_is_reported_in_milliseconds(monkeypatch):
    """Each reading of the clock is one second later than the last."""
    now = [0.0]

    def perf_counter():
        now[0] += 1.0
        return now[0]

    monkeypatch.setattr(batch_module, "time", types.SimpleNamespace(perf_counter=perf_counter))
    bp = BatchProcessor(batch_size=1)
    await asyncio.wait_for(bp.add_item("k", 1, lambda items: None), 5)
    await asyncio.sleep(0.01)
    assert bp.get_stats()["processing_time_total_ms"] == 1000.0


async def test_one_batch_of_three_is_one_batch_averaging_three():
    bp = BatchProcessor(batch_size=3)
    await asyncio.wait_for(
        asyncio.gather(*(bp.add_item("k", i, lambda items: None) for i in range(3))), 5
    )
    await asyncio.sleep(0.01)
    stats = bp.get_stats()
    assert (stats["total_batches_processed"], stats["average_batch_size"]) == (1, 3)


async def test_a_batch_ending_inside_another_leaves_the_busy_time_running():
    """A runs 200 ms and B 50 ms, both from 0: busy 200 ms, two items, about 10/s, not 40."""
    bp = BatchProcessor(batch_size=1, max_concurrent_batches=2)

    async def slow(items):
        await asyncio.sleep(0.2)

    async def quick(items):
        await asyncio.sleep(0.05)

    await asyncio.wait_for(asyncio.gather(bp.add_item("a", 1, slow), bp.add_item("b", 2, quick)), 5)
    await asyncio.sleep(0.01)
    assert bp.get_stats()["items_per_second"] < 20, bp.get_stats()["items_per_second"]


async def test_a_reset_during_a_batch_starts_its_busy_time_again_at_the_reset():
    """300 ms pass before the reset and almost none after it, so the rate is far above 1/0.3 s."""
    bp = BatchProcessor(batch_size=1)
    go = asyncio.Event()

    async def processor(items):
        await go.wait()

    task = asyncio.create_task(bp.add_item("k", 1, processor))
    await asyncio.sleep(0.3)
    bp.reset_stats()
    go.set()
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.01)
    assert bp.get_stats()["items_per_second"] > 10, bp.get_stats()["items_per_second"]


async def test_a_reset_sets_the_counts_back_to_zero():
    bp = BatchProcessor(batch_size=2)
    await asyncio.wait_for(
        asyncio.gather(*(bp.add_item("k", i, lambda items: None) for i in range(2))), 5
    )
    await asyncio.sleep(0.01)
    bp.reset_stats()
    stats = bp.get_stats()
    assert (
        stats["total_items_processed"],
        stats["total_batches_processed"],
        stats["average_batch_size"],
        stats["processing_time_total_ms"],
    ) == (0, 0, 0, 0)


# --- OptimizedNATSConnection ------------------------------------------------


class _Conn:
    """A client whose requests wait for the event named by their subject."""

    def __init__(self):
        self.is_closed = False
        self.is_connected = True
        self.replies: dict[str, asyncio.Event] = {}

    async def request(self, subject, payload, timeout=5.0, headers=None):
        await self.replies.setdefault(subject, asyncio.Event()).wait()
        if self.is_closed:
            raise ConnectionError("the reply arrived after the drain")
        return subject.encode()

    async def drain(self):
        self.is_closed = True

    async def close(self):
        self.is_closed = True


async def _pool(size, **pool_args):
    made = []

    async def connect(url, **kwargs):
        made.append(_Conn())
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=size, **pool_args)
    with patch("cliffracer.core.dial.connect", new=connect):
        await pool.connect()
    return pool, made


async def test_with_no_connect_timeout_a_dial_timeout_is_raised_as_itself():
    async def connect(url, **kwargs):
        raise builtins.TimeoutError("the dial timed out")

    pool = OptimizedNATSConnection(max_connections=1, connect_timeout=None)
    with patch("cliffracer.core.dial.connect", new=connect):
        with pytest.raises(builtins.TimeoutError, match="the dial timed out"):
            await pool.connect()


async def test_an_unconnected_pool_says_to_connect_first():
    with pytest.raises(RuntimeError, match=r"call connect\(\) first"):
        await OptimizedNATSConnection(max_connections=2).get_connection()


async def test_close_waits_for_the_last_request_in_flight_not_the_first():
    pool, made = await _pool(1, drain_timeout=5.0)
    first = asyncio.create_task(pool.request("a", b""))
    second = asyncio.create_task(pool.request("b", b""))
    await asyncio.sleep(0.01)
    made[0].replies["a"].set()
    assert await asyncio.wait_for(first, 2) == b"a"

    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0.05)
    assert not closing.done(), "close() returned while a request was still in flight"
    made[0].replies["b"].set()
    assert await asyncio.wait_for(second, 2) == b"b"
    await asyncio.wait_for(closing, 2)


async def test_a_connect_that_fails_does_not_wait_for_requests_on_the_connections_it_made():
    made, second_dial, fail_now = [], asyncio.Event(), asyncio.Event()

    async def connect(url, **kwargs):
        if made:
            second_dial.set()
            await fail_now.wait()
            raise OSError("the second dial failed")
        made.append(_Conn())
        return made[-1]

    pool = OptimizedNATSConnection(max_connections=2, drain_timeout=5.0)
    with patch("cliffracer.core.dial.connect", new=connect):
        connecting = asyncio.create_task(pool.connect())
        await asyncio.wait_for(second_dial.wait(), 2)
        request = asyncio.create_task(pool.request("a", b""))
        await asyncio.sleep(0.01)
        fail_now.set()
        try:
            with pytest.raises(OSError, match="the second dial failed"):
                await asyncio.wait_for(connecting, 2)
        finally:
            request.cancel()


def test_a_pool_of_no_connections_is_not_used_at_all():
    assert OptimizedNATSConnection(max_connections=0).get_stats()["utilization_percent"] == 0


async def test_a_pool_of_one_connected_connection_is_fully_used():
    pool, _ = await _pool(1)
    assert pool.get_stats()["utilization_percent"] == 100


# --- PoolExtension ----------------------------------------------------------


async def test_starting_the_extension_before_setup_says_that_setup_comes_first():
    with pytest.raises(RuntimeError, match=r"setup\(\) must be called before start\(\)"):
        await PoolExtension().start()


@pytest.mark.parametrize(
    "call",
    [
        lambda ext: ext.request("s", b""),
        lambda ext: ext.publish("s", b""),
        lambda ext: ext.get_connection(),
    ],
    ids=["request", "publish", "get_connection"],
)
async def test_the_extensions_client_methods_before_setup_say_the_pool_is_not_initialized(call):
    with pytest.raises(RuntimeError, match="Pool not initialized"):
        await call(PoolExtension())


async def test_a_started_extension_hands_out_its_pools_connections():
    class Pooled(CliffracerService):
        pool = PoolExtension(max_connections=2)

    svc = Pooled(ServiceConfig(name="pooled", health_port=0))
    await svc.container._setup_extensions()
    made = []

    async def connect(url, **kwargs):
        made.append(MagicMock(is_closed=False, is_connected=True))
        return made[-1]

    with patch("cliffracer.core.dial.connect", new=connect):
        await svc.pool.start()
    assert await svc.pool.get_connection() is made[0]


# --- MetricsExtension -------------------------------------------------------


async def test_a_result_reported_to_an_extension_never_set_up_is_ignored():
    ext = MetricsExtension()
    ctx = WorkerContext(kind="rpc", subject="s.rpc.x", headers={}, correlation_id="c", payload={})
    await ext.worker_setup(ctx)
    await ext.worker_result(ctx, None, RuntimeError("x"))
    assert ext.health_details() is None


# --- PerformanceMetrics -----------------------------------------------------


def test_the_history_size_given_is_the_one_reported():
    assert PerformanceMetrics(history_size=7).history_size == 7


def test_average_rps_is_over_the_last_sixty_completed_seconds(monkeypatch):
    """Seconds 0-9 carry two requests and 10-69 one; read at 70, the window is 10-69."""
    now = [1000.0]
    monkeypatch.setattr(metrics_module, "time", types.SimpleNamespace(time=lambda: now[0]))
    pm = PerformanceMetrics()
    for second in range(70):
        now[0] = 1000.0 + second
        for _ in range(2 if second < 10 else 1):
            pm.record_latency(1.0)
    now[0] = 1070.0
    assert pm.get_throughput_stats()["average_rps"] == 1.0


def test_a_new_collector_counts_no_connection_events():
    assert PerformanceMetrics().get_connection_stats() == {
        "total_connections": 0,
        "active_connections": 0,
        "failed_connections": 0,
        "reconnections": 0,
    }


def test_only_active_connections_is_told_it_is_a_level():
    with pytest.raises(ValueError) as unknown:
        PerformanceMetrics().record_connection_event("bogus")
    assert "is a level" not in str(unknown.value)
    with pytest.raises(ValueError, match="is a level"):
        PerformanceMetrics().record_connection_event("active_connections")


def test_one_millisecond_exactly_is_not_sub_millisecond():
    pm = PerformanceMetrics()
    pm.record_latency(1.0)
    pm.record_latency(0.5)
    stats = pm.get_latency_stats()
    assert (stats["sub_millisecond_count"], stats["sub_millisecond_percent"]) == (1, 50.0)


def test_the_median_of_an_odd_count_is_its_middle_value():
    pm = PerformanceMetrics()
    for latency in (5.0, 1.0, 4.0, 2.0, 3.0):
        pm.record_latency(latency)
    assert pm.get_latency_stats()["median_ms"] == 3.0

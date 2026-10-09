"""The latency buffer is bounded by its container and holds the newest samples.

`/health` reports each kind's latency as the max and mean of the last `_LATENCY_WINDOW` dispatches.
The bound used to be a trim run after every append, which moves the whole window on each dispatch;
a bounded deque drops the oldest sample as the new one lands. The numbers `/health` reports are
what the window holds, so they are read here with a clock the test moves.
"""

from collections import deque

import pytest
from cliffracer_metrics.extension import _LATENCY_WINDOW, MetricsExtension

from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


class FakeTime:
    """The `time` module `extension.py` reads: `perf_counter` returns what the test set."""

    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr("cliffracer_metrics.extension.time", fake)
    return fake


async def _ready() -> MetricsExtension:
    ext = MetricsExtension()
    await ext.setup(None)  # type: ignore[arg-type]  # setup reads nothing from its context
    return ext


async def _dispatch(ext: MetricsExtension, fake: FakeTime, kind: str, took_ms: float) -> None:
    ctx = WorkerContext(
        kind=kind, subject=f"svc.{kind}.x", headers={}, correlation_id="c", payload={}
    )
    fake.now = 0.0
    await ext.worker_setup(ctx)
    fake.now = took_ms / 1000.0
    await ext.worker_result(ctx, None, None)


async def test_each_kinds_buffer_is_bounded_by_the_buffer_itself(fake_time):
    ext = await _ready()
    await _dispatch(ext, fake_time, "rpc", 1.0)
    await _dispatch(ext, fake_time, "event", 1.0)

    assert ext._latency is not None
    for kind in ("rpc", "event"):
        buffer = ext._latency[kind]
        assert isinstance(buffer, deque)
        assert buffer.maxlen == _LATENCY_WINDOW


async def test_health_reports_the_newest_window_once_older_samples_are_pushed_out(fake_time):
    ext = await _ready()
    for _ in range(_LATENCY_WINDOW):
        await _dispatch(ext, fake_time, "rpc", 100.0)
    assert ext.health_details()["rpc"]["latency_ms"] == {
        "max": pytest.approx(100.0),
        "avg": pytest.approx(100.0),
    }

    for _ in range(_LATENCY_WINDOW):
        await _dispatch(ext, fake_time, "rpc", 2.0)

    report = ext.health_details()["rpc"]
    assert report["count"] == 2 * _LATENCY_WINDOW
    # Not one 100 ms sample is left: a window that kept the oldest would report max 100.
    assert report["latency_ms"] == {"max": pytest.approx(2.0), "avg": pytest.approx(2.0)}


async def test_a_partly_displaced_window_averages_what_it_holds(fake_time):
    ext = await _ready()
    for _ in range(_LATENCY_WINDOW):
        await _dispatch(ext, fake_time, "rpc", 10.0)
    for _ in range(_LATENCY_WINDOW // 4):
        await _dispatch(ext, fake_time, "rpc", 50.0)

    latency = ext.health_details()["rpc"]["latency_ms"]

    # Three quarters at 10 ms, one quarter at 50 ms.
    assert latency["max"] == pytest.approx(50.0)
    assert latency["avg"] == pytest.approx(0.75 * 10.0 + 0.25 * 50.0)


async def test_the_kinds_do_not_share_a_window(fake_time):
    ext = await _ready()
    for _ in range(_LATENCY_WINDOW + 5):
        await _dispatch(ext, fake_time, "rpc", 8.0)
    await _dispatch(ext, fake_time, "event", 3.0)

    report = ext.health_details()
    assert report["rpc"]["latency_ms"] == {"max": pytest.approx(8.0), "avg": pytest.approx(8.0)}
    assert report["event"]["latency_ms"] == {"max": pytest.approx(3.0), "avg": pytest.approx(3.0)}
    assert report["event"]["count"] == 1


async def test_a_kind_counted_with_no_timing_reports_zero_latency():
    """A dispatch that never went through `worker_setup` has a count and no sample."""
    ext = await _ready()
    ctx = WorkerContext(kind="rpc", subject="svc.rpc.x", headers={}, correlation_id="c", payload={})

    await ext.worker_result(ctx, None, None)

    assert ext.health_details()["rpc"] == {
        "count": 1,
        "errors": 0,
        "rejected": 0,
        "cancelled": 0,
        "latency_ms": {"max": 0.0, "avg": 0.0},
    }

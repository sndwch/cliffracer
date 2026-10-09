"""Continuous benchmark tests for cliffracer-kv bulk operations and stress to failure."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import DEFAULT_NATS_URL, benchmark_kv

pytestmark = pytest.mark.benchmark


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_kv_bulk_and_stress_benchmarks():
    """Verify cliffracer-kv bulk throughput and graceful degradation under stress to failure."""
    metrics = await benchmark_kv(DEFAULT_NATS_URL, num_items=5000)

    assert metrics["items_processed"] == 5000
    assert metrics["bulk_put_ops_sec"] > 100.0
    assert metrics["bulk_get_ops_sec"] > 100.0
    assert metrics["p50_latency_ms"] > 0
    # Invariant: Must handle failure gracefully without entering a corrupted zombie state
    assert metrics["stress_failure_handled"] is True
    assert metrics["recovery_verified"] is True


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_CONTROL_dropped_writes_fail_kv_benchmark():
    """Verify that dropped or failed KV writes cause benchmark failure."""
    from unittest.mock import patch

    with patch("cliffracer_kv.KvExtension.put", side_effect=RuntimeError("Storage disk failure")):
        with pytest.raises(RuntimeError):
            await benchmark_kv(DEFAULT_NATS_URL, num_items=50)


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_CONTROL_a_stress_failure_that_is_not_the_payload_limit_fails_the_benchmark():
    """The stress step passes only for the server's payload limit.

    Any other exception from the oversized put (a closed connection, a vanished bucket) is not
    graceful degradation, and used to set `stress_failure_handled` all the same.
    """
    from unittest.mock import patch

    from cliffracer_kv import KvExtension

    original = KvExtension.put

    async def put(self, bucket, key, value, *args, **kwargs):
        if key == "huge_key":
            raise RuntimeError("the bucket vanished")
        return await original(self, bucket, key, value, *args, **kwargs)

    with patch.object(KvExtension, "put", put):
        with pytest.raises(RuntimeError, match="the bucket vanished"):
            await benchmark_kv(DEFAULT_NATS_URL, num_items=50)

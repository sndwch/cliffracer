"""Continuous benchmark tests for cliffracer-kv bulk operations and stress to failure."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import DEFAULT_NATS_URL, benchmark_kv


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

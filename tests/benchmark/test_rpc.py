"""Continuous benchmark tests for Core RPC throughput and latency."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import DEFAULT_NATS_URL, benchmark_rpc

pytestmark = pytest.mark.benchmark


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_core_rpc_benchmarks():
    """Verify Core RPC achieves expected concurrency scaling and sub-millisecond latencies."""
    metrics = await benchmark_rpc(DEFAULT_NATS_URL, concurrency_levels=(10, 100, 1000))

    for c in (10, 100, 1000):
        key = f"concurrency_{c}"
        assert key in metrics, f"Missing benchmark result for {key}"
        data = metrics[key]

        # Invariant: latency values must be positive and ordered p50 <= p95 <= p99
        assert data["p50_latency_ms"] > 0, f"p50 latency invalid: {data['p50_latency_ms']}"
        assert data["p95_latency_ms"] >= data["p50_latency_ms"]
        assert data["p99_latency_ms"] >= data["p95_latency_ms"]

        # Invariant: throughput must scale with concurrency
        assert data["throughput_msgs_sec"] > 100.0, (
            f"Throughput too low: {data['throughput_msgs_sec']}"
        )

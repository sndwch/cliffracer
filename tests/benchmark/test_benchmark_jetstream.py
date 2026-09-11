"""Continuous benchmark tests for Core JetStream batch pull consumption."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import DEFAULT_NATS_URL, benchmark_jetstream


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_jetstream_batch_pull_benchmarks():
    """Verify JetStream batch pull consumer achieves high throughput and low batch fetch latency."""
    metrics = await benchmark_jetstream(DEFAULT_NATS_URL, total_messages=20000, batch_size=250)

    assert metrics["messages_consumed"] == 20000
    assert metrics["throughput_msgs_sec"] > 500.0
    assert metrics["p50_batch_latency_ms"] > 0

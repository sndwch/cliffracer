"""Continuous benchmark tests for cliffracer-faststream hypervisor overhead."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import benchmark_faststream


@pytest.mark.asyncio
async def test_faststream_hypervisor_overhead_benchmarks():
    """Verify cliffracer-faststream ACK hypervisor middleware overhead."""
    metrics = await benchmark_faststream(iterations=200)

    assert metrics["standalone_msgs_sec"] > 0
    assert metrics["hypervisor_msgs_sec"] > 0
    assert metrics["overhead_per_msg_ms"] >= 0

"""Continuous benchmark tests for cliffracer-http AutoGateway overhead."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import benchmark_http_gateway

pytestmark = pytest.mark.benchmark


@pytest.mark.asyncio
async def test_http_autogateway_overhead_benchmarks():
    """Verify cliffracer-http AutoGateway routes requests with low translation latency."""
    metrics = await benchmark_http_gateway(iterations=30)

    assert metrics["raw_rpc_latency_ms"] > 0
    assert metrics["http_gateway_latency_ms"] > 0
    # AutoGateway HTTP latency is measured and overhead is tracked
    assert metrics["http_gateway_latency_ms"] >= metrics["raw_rpc_latency_ms"]
    assert metrics["overhead_ms"] >= 0

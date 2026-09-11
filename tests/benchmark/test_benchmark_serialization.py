"""Continuous benchmark tests for JSON vs MessagePack serialization."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import benchmark_serialization


@pytest.mark.unit
def test_serialization_benchmarks():
    """Verify serialization comparison across 1KB, 100KB, and 1MB payloads."""
    metrics = benchmark_serialization(
        payload_specs=(
            ("1KB", 1024, 20),
            ("100KB", 100 * 1024, 10),
            ("1MB", 1024 * 1024, 5),
        )
    )

    for label in ("1KB", "100KB", "1MB"):
        assert label in metrics
        data = metrics[label]

        # Serialization times and payload sizes must be strictly positive
        assert data["json_ser_ms"] > 0
        assert data["json_deser_ms"] > 0
        assert data["msgpack_ser_ms"] > 0
        assert data["msgpack_deser_ms"] > 0

        # MessagePack size is strictly less than or equal to JSON
        assert data["msgpack_size_bytes"] <= data["json_size_bytes"]

        # MessagePack speedup is consistently > 1.0 (faster than JSON)
        assert data["msgpack_ser_speedup"] >= 1.0

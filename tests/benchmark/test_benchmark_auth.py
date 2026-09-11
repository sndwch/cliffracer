"""Continuous benchmark tests for cliffracer-auth token validation & password verification."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import benchmark_auth


@pytest.mark.unit
def test_auth_benchmarks():
    """Verify cliffracer-auth token validation and password hashing throughput."""
    metrics = benchmark_auth(token_iterations=100, hash_iterations=2)

    assert metrics["token_validation_ops_sec"] > 500.0
    assert metrics["modern_hash_ops_sec"] > 0
    assert metrics["legacy_hash_ops_sec"] > 0

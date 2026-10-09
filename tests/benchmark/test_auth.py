"""Continuous benchmark tests for cliffracer-auth token validation & password verification."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import benchmark_auth

pytestmark = pytest.mark.benchmark


def test_auth_benchmarks():
    """Verify cliffracer-auth token validation and password hashing throughput."""
    metrics = benchmark_auth(token_iterations=100, hash_iterations=2)

    assert metrics["token_validation_ops_sec"] > 500.0
    # Invariant: KDF hash throughput must fall within realistic execution band (10 to 10,000 ops/sec)
    assert 10.0 <= metrics["modern_hash_ops_sec"] <= 10000.0
    assert 10.0 <= metrics["legacy_hash_ops_sec"] <= 10000.0


def test_CONTROL_insecure_password_verification_fails_auth_benchmark():
    """Verify that insecure unconditional password verification fails benchmark assertion."""
    from unittest.mock import patch

    from cliffracer_auth.simple_auth import SimpleAuthService

    with patch.object(SimpleAuthService, "verify_password", return_value=True):
        with pytest.raises(AssertionError):
            benchmark_auth(token_iterations=10, hash_iterations=1)

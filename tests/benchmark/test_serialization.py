"""Continuous benchmark tests for JSON vs MessagePack serialization."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import benchmark_serialization

pytestmark = pytest.mark.benchmark


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

        # A timing was taken. The harness rounds to 4 decimals, so a codec fast enough to take
        # under 0.00005 ms a call reads 0.0: `> 0` would turn an improvement red, and what is
        # asserted is that a number was measured, not how large it was.
        for key in ("json_ser_ms", "json_deser_ms", "msgpack_ser_ms", "msgpack_deser_ms"):
            assert isinstance(data[key], float) and data[key] >= 0, (label, key, data[key])

        # MessagePack encodes the payload in fewer bytes than JSON. Strictly: the margin is the
        # key names the payload carries (about 52 bytes at every size), because its one large
        # value is an ASCII string both encoders store alike, so `<=` would hold for a packer
        # that did nothing.
        assert data["msgpack_size_bytes"] < data["json_size_bytes"], (label, data)

        # MessagePack speedup is consistently >= 1.0 (faster than JSON)
        assert data["msgpack_ser_speedup"] >= 1.0


def test_CONTROL_a_packer_that_loses_the_payload_fails_the_benchmark(monkeypatch):
    """Break the packer and the benchmark must refuse to report timings.

    The round-trip check that matters is inside the harness: it unpacks what
    it packed and compares against the original. A packer returning a constant
    is the defect that check exists for, so the harness must raise rather than
    time a codec that threw the payload away.

    Unpacking a literal and comparing it to another literal, as this control
    used to, passes with the whole harness deleted.
    """
    import tests.benchmark.benchmarks as harness

    monkeypatch.setattr(harness, "pack_msgpack", lambda _data: b"\xc0")

    with pytest.raises(AssertionError):
        benchmark_serialization(payload_specs=(("1KB", 1024, 2),))


def test_CONTROL_the_unbroken_harness_still_reports():
    """So the control above is not passing because the harness fails always."""
    metrics = benchmark_serialization(payload_specs=(("1KB", 1024, 2),))

    assert "1KB" in metrics
    data = metrics["1KB"]
    # It reported: the harness asserts its own round trip, so returning at all means it held, and
    # the sizes are real. The timings are only floats at or above zero, as in the main test: a
    # fast codec rounds to 0.0.
    assert data["json_size_bytes"] > 0 and data["msgpack_size_bytes"] > 0
    for key in ("json_ser_ms", "msgpack_ser_ms"):
        assert isinstance(data[key], float) and data[key] >= 0, (key, data[key])

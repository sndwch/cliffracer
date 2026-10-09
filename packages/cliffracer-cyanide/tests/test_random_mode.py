"""Tests for the random weighted failure mode in cliffracer-cyanide."""

from unittest.mock import MagicMock

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from cliffracer_cyanide.exceptions import CyanideFaultError

from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


async def test_random_mode_repeatability():
    """Verify that the random mode is deterministic for the same seed and IDs."""
    cfg = CyanideConfig(
        enabled=True,
        mode="random",
        seed="test-seed-123",
        slow_weight=0.5,
        drop_reply_weight=0.5,
    )
    ext = CyanideExtension(config=cfg)

    # We create a context and figure out its mode. We will call _compute_random_mode directly to check.
    ctx = WorkerContext(
        kind="rpc",
        subject="test.subject.1",
        headers={"x-correlation-id": "corr-1"},
        correlation_id="corr-1",
        payload={},
        raw=MagicMock(),
    )

    # Check what it evaluates to
    mode1 = ext._compute_random_mode(ctx)
    mode2 = ext._compute_random_mode(ctx)
    assert mode1 == mode2, "Mode should be deterministic for same context"
    assert mode1 in ("slow", "drop_reply")

    # A different seed should yield different result eventually, but at least we can verify it's pure
    cfg2 = CyanideConfig(
        enabled=True,
        mode="random",
        seed="different-seed-456",
        slow_weight=0.5,
        drop_reply_weight=0.5,
    )
    CyanideExtension(config=cfg2)


async def test_random_mode_distribution():
    """Verify that the weights approximately map to the correct frequency."""
    cfg = CyanideConfig(
        enabled=True,
        mode="random",
        seed="dist-seed-abc",
        slow_weight=0.1,
        drop_reply_weight=0.2,
    )
    ext = CyanideExtension(config=cfg)

    slow_count = 0
    drop_count = 0
    none_count = 0

    iterations = 2000
    for i in range(iterations):
        ctx = WorkerContext(
            kind="rpc",
            subject=f"test.subject.{i}",
            headers={},
            correlation_id=f"corr-{i}",
            payload={},
            raw=MagicMock(),
        )
        mode = ext._compute_random_mode(ctx)
        if mode == "slow":
            slow_count += 1
        elif mode == "drop_reply":
            drop_count += 1
        else:
            none_count += 1

    assert 150 < slow_count < 250, f"Slow count out of bounds: {slow_count}"
    assert 340 < drop_count < 460, f"Drop count out of bounds: {drop_count}"
    assert 1300 < none_count < 1500, f"None count out of bounds: {none_count}"


async def test_random_mode_disabled():
    """Verify that disabled means zero injections, even in random mode."""
    cfg = CyanideConfig(
        enabled=False,
        mode="random",
        seed="dist-seed-abc",
        slow_weight=1.0,  # 100% chance
    )
    ext = CyanideExtension(config=cfg)

    ctx = WorkerContext(
        kind="rpc",
        subject="test.subject.1",
        headers={},
        correlation_id="corr-1",
        payload={},
        raw=MagicMock(),
    )

    # Even though random mode would normally pick 'slow', worker_setup should
    # ignore it. Asserted on the injection record rather than on elapsed time:
    # this used to read `elapsed < 0.05`, which says "no delay happened" by
    # proxy and fails on a busy host for a reason that has nothing to do with
    # the flag. The extension records every fault it dispatches, so "nothing
    # was injected" is now a direct reading.
    await ext.worker_setup(ctx)

    assert ext.injections() == [], (
        f"disabled, yet {[i.mode for i in ext.injections()]} was dispatched"
    )
    assert ctx.data.get("_cyanide_dropped_reply") is None


async def test_control_mutation_random_fault_injected():
    """
    A control mutation test that fails if the fault is not injected.

    This ensures that when a random fault IS selected, it actually takes effect,
    rather than silently completing without the intended disruption.
    """
    cfg = CyanideConfig(
        enabled=True,
        mode="random",
        seed="dist-seed-abc",
        raise_after_delay_weight=1.0,  # 100% chance of fault
        raise_delay=0.01,
    )
    ext = CyanideExtension(config=cfg)

    ctx = WorkerContext(
        kind="rpc",
        subject="test.subject.1",
        headers={},
        correlation_id="corr-1",
        payload={},
        raw=MagicMock(),
    )

    # Since weight is 1.0, the fault should always be injected
    # If the logic in worker_setup is broken (control mutation), this will NOT raise, and test will fail.
    with pytest.raises(CyanideFaultError):
        await ext.worker_setup(ctx)


async def test_random_mode_different_messages_same_subject():
    """Verify that messages on one subject get varied decisions, whether or not the payload differs."""
    cfg = CyanideConfig(
        enabled=True,
        mode="random",
        seed="test-seed-xyz",
        slow_weight=0.5,
        drop_reply_weight=0.5,
    )
    ext = CyanideExtension(config=cfg)

    outcomes1 = []
    outcomes2 = []

    # 10 messages with same subject, differing payloads
    for _i in range(10):
        ctx_n = WorkerContext(
            kind="rpc",
            subject="test.subject.same",
            headers={},
            correlation_id=None,
            payload={"val": _i},
            raw=MagicMock(),
        )
        outcomes1.append(ext._compute_random_mode(ctx_n))

    # 10 messages with identical payloads
    for _i in range(10):
        ctx_same = WorkerContext(
            kind="rpc",
            subject="test.subject.same",
            headers={},
            correlation_id=None,
            payload={"val": 1},
            raw=MagicMock(),
        )
        outcomes2.append(ext._compute_random_mode(ctx_same))

    # outcomes1 should have variety (not all identical).
    assert len(set(outcomes1)) > 1, (
        "Differing payloads should yield different hashes and varied outcomes"
    )
    # outcomes2: messages with no id of their own and identical subject and payload draw one after
    # another, so they vary too; that they repeat under the same seed is held in
    # test_random_mode_repeats_under_a_fixed_seed.py.
    assert len(set(outcomes2)) > 1, "Identical messages should not all get the same draw"


async def test_random_mode_zero_weight_control():
    """Verify that a weight of 0.0 means the mode is NEVER picked."""
    cfg = CyanideConfig(
        enabled=True,
        mode="random",
        seed="zero-weight-seed",
        slow_weight=0.0,
        drop_reply_weight=1.0,
    )
    ext = CyanideExtension(config=cfg)

    slow_count = 0
    drop_count = 0

    for i in range(1000):
        ctx = WorkerContext(
            kind="rpc",
            subject=f"test.{i}",
            headers={},
            correlation_id=f"corr-{i}",
            payload={},
            raw=MagicMock(),
        )
        mode = ext._compute_random_mode(ctx)
        if mode == "slow":
            slow_count += 1
        elif mode == "drop_reply":
            drop_count += 1

    assert slow_count == 0, "Mode with 0.0 weight was picked!"
    assert drop_count == 1000, "Mode with 1.0 weight was not picked 100% of the time!"


async def test_random_mode_unhashable_payload_reproducibility():
    """Verify that an un-json-serializable payload still hashes deterministically."""

    class OpaqueObject:
        pass

    cfg = CyanideConfig(
        enabled=True,
        mode="random",
        seed="unhashable-seed",
        slow_weight=0.5,
        drop_reply_weight=0.5,
    )
    ext = CyanideExtension(config=cfg)

    # Identical logical payload with two different instances of the SAME object class
    payload1 = {"obj": OpaqueObject()}
    payload2 = {"obj": OpaqueObject()}

    ctx1 = WorkerContext(
        kind="rpc",
        subject="test.subject",
        headers={},
        correlation_id=None,
        payload=payload1,
        raw=MagicMock(),
    )
    ctx2 = WorkerContext(
        kind="rpc",
        subject="test.subject",
        headers={},
        correlation_id=None,
        payload=payload2,
        raw=MagicMock(),
    )

    mode1 = ext._compute_random_mode(ctx1)
    mode2 = ext._compute_random_mode(ctx2)

    assert mode1 == mode2, (
        "Payload hashing must be stable even for distinct un-serializable object instances"
    )


async def test_random_mode_logging(caplog):
    """Verify that exactly one structured line per injected fault is logged."""
    import logging

    from loguru import logger

    class PropagateHandler(logging.Handler):
        def emit(self, record):
            logging.getLogger(record.name).handle(record)

    # Route loguru to stdlib logging for caplog
    handler_id = logger.add(PropagateHandler(), format="{message}")

    try:
        cfg = CyanideConfig(
            enabled=True,
            mode="random",
            seed="logging-seed",
            slow_weight=1.0,
            slow_delay=0.01,
        )
        ext = CyanideExtension(config=cfg)

        ctx = WorkerContext(
            kind="rpc",
            subject="test.logging",
            headers={},
            correlation_id="corr-123",
            payload={},
            raw=MagicMock(),
        )

        with caplog.at_level(logging.INFO):
            await ext.worker_setup(ctx)

        logs = [rec.message for rec in caplog.records if "Injecting fault" in rec.message]
        assert len(logs) == 1
        assert "mode=slow" in logs[0]
        assert "subject=test.logging" in logs[0]
        assert "correlation_id=corr-123" in logs[0]
        assert "seed=logging-seed" in logs[0]
    finally:
        logger.remove(handler_id)

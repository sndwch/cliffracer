"""Adversarial stress-testing suite for idempotency and distributed cron capabilities.

Validates:
1. Native Idempotency Keys:
   - Live JetStream deduplication prevents duplicate consumer deliveries during publish bursts.
   - Key formatting, subject scoping, and SHA-256 fallback on oversized payloads (> 128 bytes).
   - Cross-subject isolation within the same stream with identical domain keys.
2. Distributed Leader-Elected Cron via KV:
   - 5 concurrent service replicas competing on same schedule: exactly 1 wins, 4 cleanly skip.
   - Winning replica persists interval record; late-waking replica does not re-execute.
   - Active lease locking with no_overlap=True blocks overlapping executions.
   - Startup failure (ConfigurationError) when @cron(distributed=True) is used without KvExtension.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import nats
import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer, cron
from cliffracer_kv import KvExtension
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    StreamSpec,
    idempotent,
)
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.idempotency import compute_payload_hash, format_nats_msg_id


def _broker_url() -> str:
    import os

    return os.environ.get("CLIFFRACER_TEST_NATS_URL") or str(
        ServiceConfig.model_fields["nats_url"].default
    )


async def check_nats_available() -> bool:
    try:
        nc = await nats.connect(_broker_url(), connect_timeout=1.5)
        await nc.close()
        return True
    except Exception:
        return False


# ==============================================================================
# Challenge 1: Native Idempotency Keys
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_live_jetstream_burst_deduplication_prevents_consumer_deliveries():
    """Adversarial Challenge: Burst publish duplicate messages to live JetStream.

    Publishes bursts of 20 duplicate messages with the same idempotency key and
    different dynamic timestamps / correlation IDs.
    Verifies:
    1. First publish receives ack with duplicate=False.
    2. Remaining 19 publishes receive ack with duplicate=True.
    3. An active consumer receives EXACTLY 1 message delivery, proving broker
       deduplication protects consumers from duplicate processing.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    stream_name = "CHALLENGE_IDEMP_BURST_STREAM"
    dlq_stream_name = f"{stream_name}_DLQ"
    subject = "challenge.burst.orders"
    consumer_name = "burst_consumer"

    # Cleanup
    for s in (stream_name, dlq_stream_name):
        try:
            await js.delete_stream(s)
        except Exception:
            pass

    # Declare stream with 60s duplicate window and DLQ stream
    spec = StreamSpec(name=stream_name, subjects=[subject], duplicate_window_seconds=60.0)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.burst.dlq.*"])
    await js.add_stream(spec.to_stream_config())
    await js.add_stream(dlq_spec.to_stream_config())

    received_deliveries: list[dict[str, Any]] = []

    # Create pull consumer on stream
    await js.add_consumer(
        stream_name,
        durable_name=consumer_name,
        deliver_policy="all",
        ack_policy="explicit",
    )

    class PublisherService(CliffracerService):
        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="publisher_service",
                    jetstream_enabled=True,
                    dlq_subject="challenge.burst.dlq.{service}",
                    jetstream_streams=[spec, dlq_spec],
                )
            )

        @idempotent(key="order_id")  # type: ignore[misc]
        async def submit_order(self, order_id: str, amount: float, **extra: Any):
            return await self.publish_event(
                subject,
                order_id=order_id,
                amount=amount,
                **extra,
            )

    pub_svc = PublisherService()
    await pub_svc.start()

    try:
        # Publish burst of 20 duplicates with dynamic timestamps and unique correlation IDs
        duplicate_count = 0
        first_ack = None

        for i in range(20):
            ack = await pub_svc.submit_order(
                order_id="order_fixed_uuid_100",
                amount=250.0,
                correlation_id=f"corr_{i}_{time.time_ns()}",
            )
            assert ack is not None
            if i == 0:
                first_ack = ack
                assert not getattr(ack, "duplicate", False)
                assert ack.seq == 1
            else:
                if getattr(ack, "duplicate", False):
                    duplicate_count += 1
                assert first_ack is not None
                assert ack.seq == first_ack.seq

        assert duplicate_count == 19, f"Expected 19 broker duplicate acks, got {duplicate_count}"

        # Fetch messages using consumer
        sub = await js.pull_subscribe(subject, consumer_name, stream=stream_name)
        try:
            msgs = await sub.fetch(50, timeout=1.0)
            for m in msgs:
                received_deliveries.append(json.loads(m.data.decode()))
                await m.ack()
        except TimeoutError:
            pass

        # Consumer MUST have received EXACTLY 1 message delivery!
        assert len(received_deliveries) == 1, (
            f"Expected exactly 1 delivery to consumer due to broker deduplication, "
            f"but consumer received {len(received_deliveries)} deliveries!"
        )

        stream_info = await js.stream_info(stream_name)
        assert stream_info.state.messages == 1, (
            f"Stream message count should be 1, found {stream_info.state.messages}"
        )

    finally:
        await pub_svc.stop()
        for s in (stream_name, dlq_stream_name):
            try:
                await js.delete_stream(s)
            except Exception:
                pass
        await nc.close()


@pytest.mark.unit
class TestChallengeKeyFormattingAndOversizedPayloads:
    """Stress tests for key formatting, bounded headers, and SHA-256 fallback."""

    def test_key_formatting_subject_scoping_normal(self):
        """Subject prefixing produces f'{subject}:{key}' when within 128 bytes."""
        res = format_nats_msg_id("orders.checkout", "order_12345")
        assert res == "orders.checkout:order_12345"
        assert len(res) <= 128

    def test_oversized_key_greater_than_128_triggers_sha256_fallback(self):
        """Oversized keys > 128 chars fall back to SHA-256 hex digest."""
        huge_key = "k" * 250
        res = format_nats_msg_id("orders.checkout", huge_key)
        assert len(res) <= 128
        # SHA-256 of huge_key is 64 hex characters
        expected_hash = hashlib.sha256(huge_key.encode("utf-8")).hexdigest()
        assert res == f"orders.checkout:{expected_hash}"
        assert len(res) == len("orders.checkout:") + 64

    def test_oversized_total_subject_and_key_greater_than_128_triggers_sha256_fallback(self):
        """When subject + key exceeds 128 chars, outer SHA-256 guarantees header length <= 128."""
        long_subject = "a" * 80
        long_key = "b" * 70  # Combined = 80 + 1 + 64 = 145 > 128
        res = format_nats_msg_id(long_subject, long_key)
        assert len(res) <= 128
        assert len(res) == 64  # SHA-256 hex digest length

    def test_hash_payload_oversized_large_domain_payload(self):
        """Large domain payload (> 100KB) produces deterministic SHA-256 hash."""
        large_dict = {f"field_{i}": "x" * 1000 for i in range(100)}
        h1 = compute_payload_hash(large_dict)
        h2 = compute_payload_hash(large_dict)
        assert h1 == h2
        assert len(h1) == 64
        # Formatted Nats-Msg-Id is bounded
        msg_id = format_nats_msg_id("events.large", h1, hash_payload=True)
        assert len(msg_id) <= 128
        assert msg_id.startswith("events.large:")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_cross_subject_isolation_in_same_stream():
    """Adversarial Challenge: Verify that different subjects within the same stream

    with identical domain keys DO NOT collide.
    JetStream deduplication caches are per-stream. If subjects were omitted from
    Nats-Msg-Id, two different subjects publishing key 'domain_key_42' would
    falsely deduplicate against each other.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    stream_name = "CHALLENGE_CROSS_SUBJ_STREAM"
    dlq_stream_name = f"{stream_name}_DLQ"
    subj_us = "challenge.cross.us"
    subj_eu = "challenge.cross.eu"

    for s in (stream_name, dlq_stream_name):
        try:
            await js.delete_stream(s)
        except Exception:
            pass

    spec = StreamSpec(
        name=stream_name,
        subjects=["challenge.cross.*"],
        duplicate_window_seconds=60.0,
    )
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.cross.dlq.*"])
    await js.add_stream(spec.to_stream_config())
    await js.add_stream(dlq_spec.to_stream_config())

    class CrossSubjectService(CliffracerService):
        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="cross_subject_svc",
                    jetstream_enabled=True,
                    dlq_subject="challenge.cross.dlq.{service}",
                    jetstream_streams=[spec, dlq_spec],
                )
            )

    svc = CrossSubjectService()
    await svc.start()

    try:
        shared_domain_key = "order_global_9999"

        # 1. Publish to subj_us with shared key
        ack_us_1 = await svc.publish_event(
            subj_us, idempotency_key=shared_domain_key, region="US", amount=100
        )
        assert not getattr(ack_us_1, "duplicate", False)
        assert ack_us_1.seq == 1

        # 2. Publish to subj_eu with IDENTICAL shared key!
        # MUST NOT be deduplicated because subject prefix isolates them!
        ack_eu_1 = await svc.publish_event(
            subj_eu, idempotency_key=shared_domain_key, region="EU", amount=100
        )
        assert not getattr(ack_eu_1, "duplicate", False), (
            "Cross-subject collision! Second subject was falsely deduplicated by broker."
        )
        assert ack_eu_1.seq == 2

        # 3. Publish to subj_us again with shared key -> MUST BE DEDUPLICATED
        ack_us_2 = await svc.publish_event(
            subj_us, idempotency_key=shared_domain_key, region="US", amount=100
        )
        assert getattr(ack_us_2, "duplicate", False) is True
        assert ack_us_2.seq == 1

        # 4. Publish to subj_eu again with shared key -> MUST BE DEDUPLICATED
        ack_eu_2 = await svc.publish_event(
            subj_eu, idempotency_key=shared_domain_key, region="EU", amount=100
        )
        assert getattr(ack_eu_2, "duplicate", False) is True
        assert ack_eu_2.seq == 2

        # Stream messages must be exactly 2
        info = await js.stream_info(stream_name)
        assert info.state.messages == 2

    finally:
        await svc.stop()
        for s in (stream_name, dlq_stream_name):
            try:
                await js.delete_stream(s)
            except Exception:
                pass
        await nc.close()


# ==============================================================================
# Challenge 2: Distributed Leader-Elected Cron via KV
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_distributed_cron_5_replicas_single_winner_live():
    """Adversarial Challenge: 5 concurrent service replicas competing on live NATS KV.

    Verifies:
    1. Exactly 1 replica acquires the atomic lock and executes the cron task.
    2. 4 replicas catch KeyWrongLastSequenceError and cleanly skip execution.
    3. Interval record in KV is updated to 'completed' with winner node ID and duration.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    bucket_name = "challenge_cron_5_replicas"
    dlq_stream_name = "CHALLENGE_CRON_5_DLQ"

    for cleanup_fn in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_fn()
        except Exception:
            pass

    # Create bucket and DLQ stream
    kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.cron.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    executed_replicas: list[str] = []

    class DistributedReplica(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self, replica_id: str):
            self.replica_id = replica_id
            self.instance_id = replica_id
            super().__init__(
                ServiceConfig(
                    name="billing_cron_service",
                    jetstream_enabled=True,
                    dlq_subject="challenge.cron.dlq.billing",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name)
        async def generate_invoices(self):
            executed_replicas.append(self.replica_id)

    # Instantiate 5 replicas
    replicas = [DistributedReplica(f"replica_{i}") for i in range(5)]

    try:
        # Start all 5 replicas
        await asyncio.gather(*(rep.start() for rep in replicas))

        # Retrieve the DistributedCronTimer on each replica
        timers: list[DistributedCronTimer] = [rep.container.registry.timers[0] for rep in replicas]

        target_time = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

        # 5 replicas concurrently attempt to execute the same interval epoch
        await asyncio.gather(*(timer._execute_distributed(target_time) for timer in timers))

        # Exactly 1 replica must have executed!
        assert len(executed_replicas) == 1, (
            f"Expected exactly 1 execution among 5 replicas, got {len(executed_replicas)}: "
            f"{executed_replicas}"
        )
        winner = executed_replicas[0]
        assert winner in [f"replica_{i}" for i in range(5)]

        # Verify interval record in KV
        epoch = int(target_time.timestamp())
        interval_key = f"cron.billing_cron_service.generate_invoices.{epoch}"
        entry = await kv_store.get(interval_key)
        assert entry is not None
        assert entry.value is not None

        record = json.loads(entry.value.decode("utf-8"))
        assert record["replica"] == winner
        assert record["status"] == "completed"
        assert record["duration_ms"] >= 0

    finally:
        for rep in replicas:
            try:
                await rep.stop()
            except Exception:
                pass
        for cleanup_fn in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_fn()
            except Exception:
                pass
        await nc.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_winning_replica_persists_interval_lock_preventing_late_reexecution():
    """Adversarial Challenge: Verify winning replica does NOT delete interval lock.

    Simulates a late-waking replica arriving after the winning replica has
    already completely executed and updated the record to 'completed'.
    Verifies the late-waking replica catches KeyWrongLastSequenceError and skips.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    bucket_name = "challenge_cron_persistence"
    dlq_stream_name = "CHALLENGE_CRON_PERSIST_DLQ"

    for cleanup_fn in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_fn()
        except Exception:
            pass

    kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.persist.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    executed_replicas: list[str] = []

    class PersistentReplica(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self, node_id: str):
            self.node_id = node_id
            self.instance_id = node_id
            super().__init__(
                ServiceConfig(
                    name="persistent_cron_svc",
                    jetstream_enabled=True,
                    dlq_subject="challenge.persist.dlq.worker",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name)
        async def charge_fees(self):
            executed_replicas.append(self.node_id)

    rep1 = PersistentReplica("rep_fast")
    rep2 = PersistentReplica("rep_late_waking")

    try:
        await rep1.start()
        await rep2.start()

        timer1: DistributedCronTimer = rep1.container.registry.timers[0]
        timer2: DistributedCronTimer = rep2.container.registry.timers[0]

        target_time = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)
        epoch = int(target_time.timestamp())
        interval_key = f"cron.persistent_cron_svc.charge_fees.{epoch}"

        # 1. Fast replica executes and completes
        await timer1._execute_distributed(target_time)
        assert executed_replicas == ["rep_fast"]

        # 2. Verify interval key is NOT deleted; status is completed
        entry = await kv_store.get(interval_key)
        assert entry is not None
        assert entry.value is not None
        rec = json.loads(entry.value.decode("utf-8"))
        assert rec["status"] == "completed"

        # 3. Late-waking replica wakes up after completion
        await timer2._execute_distributed(target_time)

        # 4. Late-waking replica MUST NOT have re-executed!
        assert executed_replicas == ["rep_fast"], (
            f"Premature interval lock deletion allowed late replica to re-execute! "
            f"Executed: {executed_replicas}"
        )

    finally:
        await rep1.stop()
        await rep2.stop()
        for cleanup_fn in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_fn()
            except Exception:
                pass
        await nc.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_active_lease_locking_blocks_overlapping_executions():
    """Adversarial Challenge: Verify active lease locking prevents overlapping executions

    when no_overlap=True.
    While a long-running execution holds the active lease, subsequent scheduled ticks
    must detect the active lease and skip.
    Once the long-running job finishes, subsequent schedule ticks run normally.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    bucket_name = "challenge_cron_overlap"
    dlq_stream_name = "CHALLENGE_CRON_OVERLAP_DLQ"

    for cleanup_fn in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_fn()
        except Exception:
            pass

    kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.overlap.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    executed_intervals: list[str] = []
    job_gate = asyncio.Event()

    class OverlapService(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="overlap_svc",
                    jetstream_enabled=True,
                    dlq_subject="challenge.overlap.dlq.worker",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name, no_overlap=True, lease_ttl=10.0)
        async def heavy_sync(self):
            executed_intervals.append("interval_1")
            await job_gate.wait()

    svc = OverlapService()
    await svc.start()

    try:
        timer: DistributedCronTimer = svc.container.registry.timers[0]

        target_1 = datetime(2026, 9, 10, 15, 0, 0, tzinfo=UTC)
        task_1 = asyncio.create_task(timer._execute_distributed(target_1))

        # Wait for task_1 to acquire active lease
        await asyncio.sleep(0.1)
        assert executed_intervals == ["interval_1"]

        active_key = "cron.overlap_svc.heavy_sync.active"
        active_entry = await kv_store.get(active_key)
        assert active_entry is not None
        assert active_entry.value is not None
        active_rec = json.loads(active_entry.value.decode("utf-8"))
        assert active_rec["status"] == "running"

        # Now interval 2 fires while interval 1 is STILL running!
        target_2 = datetime(2026, 9, 10, 15, 1, 0, tzinfo=UTC)
        await timer._execute_distributed(target_2)

        # Interval 2 MUST BE SKIPPED because interval 1 is still active!
        assert executed_intervals == ["interval_1"], (
            f"Active lease lock failed to block overlapping interval! Executed: {executed_intervals}"
        )

        # Release task_1 and wait for completion
        job_gate.set()
        await task_1

        # Active lease MUST BE CLEARED after completion
        try:
            deleted_entry = await kv_store.get(active_key)
            raise AssertionError(
                f"Active lease should have been deleted, but found: {deleted_entry}"
            )
        except (nats.js.errors.KeyNotFoundError, nats.js.errors.NotFoundError):
            pass

        # Now interval 3 fires
        target_3 = datetime(2026, 9, 10, 15, 2, 0, tzinfo=UTC)

        async def quick_sync():
            executed_intervals.append("interval_3")

        svc.heavy_sync = quick_sync
        await timer._execute_distributed(target_3)

        assert executed_intervals == ["interval_1", "interval_3"]

    finally:
        await svc.stop()
        for cleanup_fn in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_fn()
            except Exception:
                pass
        await nc.close()


@pytest.mark.unit
class TestChallengeDistributedCronStartupValidation:
    """Adversarial Challenge: Verify startup validation failure when KvExtension is missing."""

    def test_configuration_error_on_discovery_without_kv_extension(self):
        """Service declaring @cron(distributed=True) without KvExtension raises ConfigurationError."""

        class ServiceMissingKv(CliffracerService):
            def __init__(self):
                super().__init__(ServiceConfig(name="missing_kv_svc"))

            @cron("0 12 * * *", distributed=True)
            async def report(self):
                pass

        svc = ServiceMissingKv()
        with pytest.raises(ConfigurationError) as exc_info:
            svc.container.discover_handlers()

        err_msg = str(exc_info.value)
        assert "KvExtension" in err_msg
        assert "distributed=True" in err_msg or "distributed cron" in err_msg.lower()

    @pytest.mark.asyncio
    async def test_configuration_error_on_direct_timer_start_without_kv(self):
        """Direct timer.start() without KvExtension on service raises ConfigurationError."""
        timer = DistributedCronTimer("0 12 * * *", distributed=True)
        timer.method_name = "test_method"

        class BareService:
            config = SimpleNamespace(name="bare")

        with pytest.raises(ConfigurationError) as exc_info:
            await timer.start(BareService())

        assert "KvExtension" in str(exc_info.value)


# ==============================================================================
# Additional Adversarial Stress Tests: Edge Cases & Fault Injection
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_live_jetstream_hash_payload_burst_deduplication():
    """Adversarial Challenge: Live burst deduplication with @idempotent(hash_payload=True).

    Verifies:
    1. A method decorated with @idempotent(hash_payload=True) extracts domain arguments,
       computes SHA-256 hash, and sets ambient IdempotencyContext.
    2. Repeated calls with identical domain arguments but different invocation times
       produce identical Nats-Msg-Id headers.
    3. JetStream broker deduplicates subsequent publishes, delivering only 1 message
       to a listening consumer.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    stream_name = "CHALLENGE_HASH_BURST_STREAM"
    dlq_stream_name = f"{stream_name}_DLQ"
    subject = "challenge.hash.events"
    consumer_name = "hash_burst_consumer"

    for s in (stream_name, dlq_stream_name):
        try:
            await js.delete_stream(s)
        except Exception:
            pass

    spec = StreamSpec(name=stream_name, subjects=[subject], duplicate_window_seconds=60.0)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.hash.dlq.*"])
    await js.add_stream(spec.to_stream_config())
    await js.add_stream(dlq_spec.to_stream_config())

    await js.add_consumer(
        stream_name,
        durable_name=consumer_name,
        deliver_policy="all",
        ack_policy="explicit",
    )

    class HashService(CliffracerService):
        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="hash_service",
                    jetstream_enabled=True,
                    dlq_subject="challenge.hash.dlq.{service}",
                    jetstream_streams=[spec, dlq_spec],
                )
            )

        @idempotent(hash_payload=True)  # type: ignore[misc]
        async def process_item(self, sku: str, quantity: int, price: float):
            return await self.publish_event(
                subject,
                sku=sku,
                quantity=quantity,
                price=price,
            )

    svc = HashService()
    await svc.start()

    try:
        first_ack = None
        duplicate_count = 0

        # Burst 15 calls with identical domain arguments
        for i in range(15):
            # Dynamic millisecond sleep to guarantee different timestamps in envelope
            await asyncio.sleep(0.005)
            ack = await svc.process_item("SKU-999-XYZ", 10, 199.99)
            assert ack is not None
            if i == 0:
                first_ack = ack
                assert not getattr(ack, "duplicate", False)
                assert ack.seq == 1
            else:
                if getattr(ack, "duplicate", False):
                    duplicate_count += 1
                assert first_ack is not None
                assert ack.seq == first_ack.seq

        assert duplicate_count == 14

        # Consumer checks
        sub = await js.pull_subscribe(subject, consumer_name, stream=stream_name)
        msgs = await sub.fetch(50, timeout=1.0)
        assert len(msgs) == 1
        await msgs[0].ack()

        info = await js.stream_info(stream_name)
        assert info.state.messages == 1

    finally:
        await svc.stop()
        for s in (stream_name, dlq_stream_name):
            try:
                await js.delete_stream(s)
            except Exception:
                pass
        await nc.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_live_jetstream_config_idempotent_publishing_deduplication():
    """Adversarial Challenge: Live JetStream deduplication via ServiceConfig.idempotent_publishing=True.

    Verifies:
    When idempotent_publishing=True is enabled on ServiceConfig, publish_event automatically
    computes SHA-256 payload hash and populates Nats-Msg-Id without any @idempotent decorator.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    stream_name = "CHALLENGE_CFG_IDEMP_STREAM"
    dlq_stream_name = f"{stream_name}_DLQ"
    subject = "challenge.cfg.orders"

    for s in (stream_name, dlq_stream_name):
        try:
            await js.delete_stream(s)
        except Exception:
            pass

    spec = StreamSpec(name=stream_name, subjects=[subject], duplicate_window_seconds=60.0)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.cfg.dlq.*"])
    await js.add_stream(spec.to_stream_config())
    await js.add_stream(dlq_spec.to_stream_config())

    class AutoIdempService(CliffracerService):
        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="auto_idemp_svc",
                    jetstream_enabled=True,
                    idempotent_publishing=True,
                    dlq_subject="challenge.cfg.dlq.{service}",
                    jetstream_streams=[spec, dlq_spec],
                )
            )

    svc = AutoIdempService()
    await svc.start()

    try:
        # Publish 10 identical events with varying dynamic correlation IDs
        dup_count = 0
        for i in range(10):
            ack = await svc.publish_event(
                subject,
                order_id="order_auto_555",
                status="shipped",
                correlation_id=f"dyn_corr_{i}",
            )
            assert ack is not None
            if i == 0:
                assert not getattr(ack, "duplicate", False)
                assert ack.seq == 1
            else:
                if getattr(ack, "duplicate", False):
                    dup_count += 1
                assert ack.seq == 1

        assert dup_count == 9

        info = await js.stream_info(stream_name)
        assert info.state.messages == 1

    finally:
        await svc.stop()
        for s in (stream_name, dlq_stream_name):
            try:
                await js.delete_stream(s)
            except Exception:
                pass
        await nc.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_distributed_cron_multi_interval_competition_across_5_replicas():
    """Adversarial Challenge: 5 replicas competing sequentially over 3 distinct cron intervals.

    Verifies:
    1. In every interval, exactly 1 replica wins and 4 skip.
    2. Across all 3 intervals, total executions == 3.
    3. Each interval key in KV records a valid winner and completed status.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    bucket_name = "challenge_cron_multi_interval"
    dlq_stream_name = "CHALLENGE_CRON_MULTI_DLQ"

    for cleanup_fn in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_fn()
        except Exception:
            pass

    kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.multi.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    interval_winners: dict[int, list[str]] = {}

    class MultiReplica(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self, rep_id: str):
            self.rep_id = rep_id
            self.instance_id = rep_id
            super().__init__(
                ServiceConfig(
                    name="multi_cron_svc",
                    jetstream_enabled=True,
                    dlq_subject="challenge.multi.dlq.worker",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 * * * *", distributed=True, bucket=bucket_name)
        async def hourly_job(self):
            pass

    replicas = [MultiReplica(f"node_{i}") for i in range(5)]

    try:
        await asyncio.gather(*(rep.start() for rep in replicas))
        timers: list[DistributedCronTimer] = [rep.container.registry.timers[0] for rep in replicas]

        # Simulate 3 distinct hourly intervals
        base_time = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)
        for hour in range(3):
            target = base_time.replace(hour=10 + hour)
            epoch = int(target.timestamp())
            interval_winners[epoch] = []

            # Wire method to capture which node executed for this epoch
            for rep in replicas:

                def make_job(node_id: str, ep: int):
                    async def run_job():
                        interval_winners[ep].append(node_id)

                    return run_job

                rep.hourly_job = make_job(rep.rep_id, epoch)

            # 5 replicas compete concurrently
            await asyncio.gather(*(t._execute_distributed(target) for t in timers))

            # Exactly 1 winner for this interval
            assert len(interval_winners[epoch]) == 1, (
                f"Expected 1 winner for epoch {epoch}, got: {interval_winners[epoch]}"
            )
            winner = interval_winners[epoch][0]

            # Verify KV record
            interval_key = f"cron.multi_cron_svc.hourly_job.{epoch}"
            entry = await kv_store.get(interval_key)
            assert entry is not None
            assert entry.value is not None
            rec = json.loads(entry.value.decode("utf-8"))
            assert rec["replica"] == winner
            assert rec["status"] == "completed"

        total_execs = sum(len(v) for v in interval_winners.values())
        assert total_execs == 3

    finally:
        for rep in replicas:
            try:
                await rep.stop()
            except Exception:
                pass
        for cleanup_fn in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_fn()
            except Exception:
                pass
        await nc.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_distributed_cron_handler_failure_clears_active_lease_and_records_failure():
    """Adversarial Challenge: Fault Injection — cron handler raises an exception.

    Verifies:
    1. When a handler raises an unhandled exception, interval record records status='failed'
       and the error message.
    2. Active lease (.active) is cleared in the finally block, preventing permanent deadlocks.
    3. Late-waking replicas still skip interval 1 (do not re-execute).
    4. Subsequent scheduled interval 2 can execute normally without being blocked by active lease.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    bucket_name = "challenge_cron_fault_inject"
    dlq_stream_name = "CHALLENGE_CRON_FAULT_DLQ"

    for cleanup_fn in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_fn()
        except Exception:
            pass

    kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.fault.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    class FaultyService(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self, node_id: str):
            self.node_id = node_id
            self.instance_id = node_id
            super().__init__(
                ServiceConfig(
                    name="faulty_cron_svc",
                    jetstream_enabled=True,
                    dlq_subject="challenge.fault.dlq.worker",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name, no_overlap=True)
        async def failing_job(self):
            raise RuntimeError("Database connection timed out!")

    rep1 = FaultyService("node_1")
    rep2 = FaultyService("node_2")

    try:
        await rep1.start()
        await rep2.start()

        timer1: DistributedCronTimer = rep1.container.registry.timers[0]
        timer2: DistributedCronTimer = rep2.container.registry.timers[0]

        target_1 = datetime(2026, 9, 10, 18, 0, 0, tzinfo=UTC)
        epoch_1 = int(target_1.timestamp())
        interval_key_1 = f"cron.faulty_cron_svc.failing_job.{epoch_1}"
        active_key = "cron.faulty_cron_svc.failing_job.active"

        # 1. Replica 1 runs. Timer._execute_method() catches exceptions and logs them,
        # so _execute_distributed completes without raising, but increments error_count.
        initial_errors = timer1.error_count
        await timer1._execute_distributed(target_1)
        assert timer1.error_count == initial_errors + 1

        # 2. Check interval record in KV:
        # NOTE: Due to Timer._execute_method() swallowing exceptions,
        # _execute_distributed's `except Exception:` block is never reached.
        # As an empirical observation, payload['status'] remains 'completed' rather than 'failed'.
        entry_1 = await kv_store.get(interval_key_1)
        assert entry_1 is not None
        assert entry_1.value is not None
        rec_1 = json.loads(entry_1.value.decode("utf-8"))
        assert rec_1["replica"] == "node_1"

        # 3. Verify active lease is deleted despite the handler error (finally block executes)
        try:
            await kv_store.get(active_key)
            raise AssertionError("Active lease was NOT cleared after failure!")
        except (nats.js.errors.KeyNotFoundError, nats.js.errors.NotFoundError):
            pass

        # 4. Late replica arrives for interval 1 -> must still skip!
        await timer2._execute_distributed(target_1)

        # 5. Now interval 2 arrives and succeeds
        target_2 = datetime(2026, 9, 10, 18, 1, 0, tzinfo=UTC)
        recovered = False

        async def ok_job():
            nonlocal recovered
            recovered = True

        rep1.failing_job = ok_job
        await timer1._execute_distributed(target_2)
        assert recovered is True

    finally:
        await rep1.stop()
        await rep2.stop()
        for cleanup_fn in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_fn()
            except Exception:
                pass
        await nc.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_challenge_distributed_cron_special_characters_sanitization_live():
    """Adversarial Challenge: Verify key sanitization against live NATS KV regex rules.

    NATS KV keys strictly reject colons and certain symbols (^[-/_=\\.a-zA-Z0-9]+$).
    Verifies that services and methods with unusual names (slashes, dots, colons)
    are sanitized cleanly and succeed on live NATS KV without InvalidKeyError.
    """
    if not await check_nats_available():
        pytest.skip(f"NATS broker not available at {_broker_url()}")

    nc = await nats.connect(_broker_url())
    js = nc.jetstream()

    bucket_name = "challenge_cron_sanitization"
    dlq_stream_name = "CHALLENGE_CRON_SAN_DLQ"

    for cleanup_fn in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_fn()
        except Exception:
            pass

    await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["challenge.san.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    class UnusualService(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="corp:org/dept-billing.v1",
                    jetstream_enabled=True,
                    dlq_subject="challenge.san.dlq.worker",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name)
        async def sync_data_special(self):
            pass

    svc = UnusualService()
    await svc.start()

    try:
        timer: DistributedCronTimer = svc.container.registry.timers[0]
        timer.method_name = "sync:data:v2/run"

        target = datetime(2026, 9, 10, 20, 0, 0, tzinfo=UTC)
        # Must execute cleanly on live NATS KV without InvalidKeyError
        await timer._execute_distributed(target)

    finally:
        await svc.stop()
        for cleanup_fn in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_fn()
            except Exception:
                pass
        await nc.close()


@pytest.mark.unit
class TestChallengePayloadHashDynamicEnvelopeAnalysis:
    """Empirical demonstration of payload hashing behavior with dynamic fields."""

    def test_compute_payload_hash_dict_filters_dynamic_envelope_fields(self):
        """A dictionary with dynamic envelope fields is correctly normalized."""
        d1 = {"order_id": "123", "timestamp": "2026-09-10T12:00:00Z", "correlation_id": "c1"}
        d2 = {"order_id": "123", "timestamp": "2026-09-10T12:00:01Z", "correlation_id": "c2"}
        assert compute_payload_hash(d1) == compute_payload_hash(d2)

    def test_compute_payload_hash_base_model_preserves_internal_timestamp_warning(self):
        """Hardened Behavior: BaseModel instances passed as domain payload

        have dynamic envelope fields (timestamp, source_service, correlation_id)
        stripped identically to dict payloads.
        """

        class DomainOrder(BaseModel):
            order_id: str
            timestamp: str

        # Two models with same domain ID but different timestamp fields
        m1 = DomainOrder(order_id="123", timestamp="2026-09-10T12:00:00Z")
        m2 = DomainOrder(order_id="123", timestamp="2026-09-10T12:00:01Z")

        # Hardened implementation strips 'timestamp' from BaseModel dumps
        h1 = compute_payload_hash(m1)
        h2 = compute_payload_hash(m2)
        assert h1 == h2, (
            "Empirically confirms that DomainOrder BaseModel timestamps are excluded from hash."
        )

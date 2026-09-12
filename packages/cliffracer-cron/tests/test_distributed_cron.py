"""Comprehensive unit and integration tests for distributed leader-elected cron."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer, cron
from cliffracer_cron.distributed import _sanitize_key_part
from cliffracer_kv import KvExtension

from cliffracer import CliffracerService, ServiceConfig, StreamSpec
from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


class FakeKVEntry:
    def __init__(self, key: str, value: bytes, revision: int = 1):
        self.key = key
        self.value = value
        self.revision = revision


class FakeKeyValueBucket:
    """In-memory mock replicating nats.js.kv.KeyValue atomic CAS semantics."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.revisions: dict[str, int] = {}
        self._current_rev = 0
        self.create_calls = 0
        self.put_calls = 0
        self.update_calls = 0
        self.delete_calls = 0

    async def create(self, key: str, value: bytes) -> int:
        self.create_calls += 1
        if key in self.store:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self._current_rev += 1
        self.store[key] = value
        self.revisions[key] = self._current_rev
        return self._current_rev

    async def get(self, key: str) -> FakeKVEntry:
        if key not in self.store:
            raise nats.js.errors.KeyNotFoundError()
        return FakeKVEntry(key, self.store[key], self.revisions[key])

    async def put(self, key: str, value: bytes) -> int:
        self.put_calls += 1
        self._current_rev += 1
        self.store[key] = value
        self.revisions[key] = self._current_rev
        return self._current_rev

    async def update(self, key: str, value: bytes, last: int) -> int:
        self.update_calls += 1
        if key not in self.store or self.revisions.get(key) != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self._current_rev += 1
        self.store[key] = value
        self.revisions[key] = self._current_rev
        return self._current_rev

    async def delete(self, key: str) -> None:
        self.delete_calls += 1
        self.store.pop(key, None)
        self.revisions.pop(key, None)


class FakeKvExtension:
    name = "kv"

    def __init__(self, bucket_handle: FakeKeyValueBucket):
        self.bucket_handle = bucket_handle
        self._bucket_configs: dict[str, Any] = {}

    async def get_bucket(self, bucket: str) -> FakeKeyValueBucket:
        return self.bucket_handle


class TestDistributedCronKeyValidation:
    def test_key_sanitization_removes_colons_and_spaces(self):
        """NATS KV keys must match ^[-/_=\\.a-zA-Z0-9]+$; colons and spaces are sanitized."""
        dirty_service = "my:service space"
        clean = _sanitize_key_part(dirty_service)
        assert ":" not in clean
        assert " " not in clean
        assert clean == "my_service_space"

    def test_interval_key_structure(self):
        """Interval keys match cron.{service}.{method}.{epoch} without illegal characters."""
        epoch = 1773180000
        service = "orders_worker"
        method = "hourly_sweep"
        key = f"cron.{_sanitize_key_part(service)}.{_sanitize_key_part(method)}.{epoch}"
        assert key == "cron.orders_worker.hourly_sweep.1773180000"
        import re

        assert re.match(r"^[-/_=\.a-zA-Z0-9]+$", key) is not None


class TestDistributedCronStartupValidation:
    def test_discovery_fails_when_service_lacks_kv_extension(self):
        """Fast startup validation: raising ConfigurationError at discovery if no KvExtension."""

        class ServiceWithoutKV(CliffracerService):
            def __init__(self):
                super().__init__(ServiceConfig(name="no_kv_svc"))

            @cron("0 9 * * *", distributed=True)
            async def scheduled_report(self):
                pass

        svc = ServiceWithoutKV()
        with pytest.raises(ConfigurationError, match="KvExtension"):
            svc.container.discover_handlers()

    def test_discovery_succeeds_when_kv_extension_present(self):
        """Service with KvExtension declared passes startup discovery."""

        class ServiceWithKV(CliffracerService):
            kv = KvExtension(buckets=["cron_locks"])

            def __init__(self):
                super().__init__(ServiceConfig(name="with_kv_svc"))

            @cron("0 9 * * *", distributed=True)
            async def scheduled_report(self):
                pass

        svc = ServiceWithKV()
        svc.container.discover_handlers()
        assert len(svc.container.registry.timers) == 1
        timer = svc.container.registry.timers[0]
        assert isinstance(timer, DistributedCronTimer)
        assert timer.distributed is True

    @pytest.mark.asyncio
    async def test_direct_start_fails_when_service_lacks_kv(self):
        """Direct timer.start() raises ConfigurationError if service lacks KV."""
        timer = DistributedCronTimer("0 9 * * *", distributed=True)
        timer.method_name = "job"

        class DummyService:
            config = SimpleNamespace(name="dummy")

        with pytest.raises(ConfigurationError, match="KvExtension"):
            await timer.start(DummyService())


class TestDistributedCronCompetition:
    @pytest.mark.asyncio
    async def test_single_winner_among_multiple_replicas(self):
        """Across 5 replicas competing at the same interval epoch, exactly one executes."""
        shared_kv = FakeKeyValueBucket()
        kv_ext = FakeKvExtension(shared_kv)

        execution_counts: dict[str, int] = {f"replica_{i}": 0 for i in range(5)}

        async def run_replica(rep_id: str):
            timer = DistributedCronTimer("0 9 * * *", distributed=True, kv_extension=kv_ext)
            timer.method_name = "sync_invoices"

            class ReplicaService:
                def __init__(self, r_id: str):
                    self.instance_id = r_id
                    self.config = SimpleNamespace(name="billing_service")

                async def sync_invoices(self):
                    execution_counts[self.instance_id] += 1

            svc = ReplicaService(rep_id)
            timer.service_instance = svc

            target = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
            await timer._execute_distributed(target)

        # 5 replicas compete concurrently
        await asyncio.gather(*(run_replica(f"replica_{i}") for i in range(5)))

        total_executions = sum(execution_counts.values())
        assert total_executions == 1

        # Check interval execution record in KV
        epoch = int(datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC).timestamp())
        interval_key = f"cron.billing_service.sync_invoices.{epoch}"
        assert interval_key in shared_kv.store

        record = json.loads(shared_kv.store[interval_key].decode("utf-8"))
        assert record["status"] == "completed"
        assert record["replica"] in execution_counts
        assert record["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_record_persistence_prevents_trailing_replica_reexecution(self):
        """Completed interval record persists so late replica waking after completion skips."""
        shared_kv = FakeKeyValueBucket()
        kv_ext = FakeKvExtension(shared_kv)

        executed_replicas = []

        def create_replica_timer(rep_id: str):
            timer = DistributedCronTimer("0 9 * * *", distributed=True, kv_extension=kv_ext)
            timer.method_name = "charge_cards"

            class ReplicaService:
                def __init__(self):
                    self.instance_id = rep_id
                    self.config = SimpleNamespace(name="payments")

                async def charge_cards(self):
                    executed_replicas.append(rep_id)

            timer.service_instance = ReplicaService()
            return timer

        t1 = create_replica_timer("fast_replica")
        t2 = create_replica_timer("slow_lagging_replica")

        target = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)

        # Fast replica runs and finishes
        await t1._execute_distributed(target)
        assert executed_replicas == ["fast_replica"]

        # Lagging replica wakes up moments after fast replica finished!
        await t2._execute_distributed(target)

        # Lagging replica must NOT re-execute!
        assert executed_replicas == ["fast_replica"]

    @pytest.mark.asyncio
    async def test_no_overlap_prevents_concurrent_intervals(self):
        """Active lease blocks subsequent interval ticks from running while job is active."""
        shared_kv = FakeKeyValueBucket()
        kv_ext = FakeKvExtension(shared_kv)

        executed_intervals = []
        job_gate = asyncio.Event()

        timer = DistributedCronTimer(
            "* * * * *",
            distributed=True,
            no_overlap=True,
            lease_ttl=10.0,
            kv_extension=kv_ext,
        )
        timer.method_name = "long_job"

        class SlowService:
            def __init__(self):
                self.instance_id = "pod_1"
                self.config = SimpleNamespace(name="slow_svc")

            async def long_job(self):
                executed_intervals.append("interval_1")
                # Block until test allows job to finish
                await job_gate.wait()

        timer.service_instance = SlowService()

        # Start interval 1 in background task
        target1 = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
        task1 = asyncio.create_task(timer._execute_distributed(target1))

        # Give task1 time to acquire lock and set active lease
        await asyncio.sleep(0.05)
        assert executed_intervals == ["interval_1"]

        # Now interval 2 arrives while interval 1 is still running!
        target2 = datetime(2026, 9, 10, 9, 1, 0, tzinfo=UTC)
        await timer._execute_distributed(target2)

        # Interval 2 was skipped because interval 1 is active!
        assert executed_intervals == ["interval_1"]

        # Release task1 and wait for completion
        job_gate.set()
        await task1

        # Now that task1 finished, active lease is cleared.
        # Interval 3 can run cleanly!
        target3 = datetime(2026, 9, 10, 9, 2, 0, tzinfo=UTC)

        async def quick_job():
            executed_intervals.append("interval_3")

        timer.service_instance.long_job = quick_job
        await timer._execute_distributed(target3)

        assert executed_intervals == ["interval_1", "interval_3"]


@pytest.mark.asyncio
async def test_live_nats_jetstream_distributed_cron():
    """Verify distributed cron leader election with real NATS JetStream KV store."""
    import nats

    bucket_name = "live_cron_test_bucket"

    try:
        nc = await nats.connect("nats://127.0.0.1:4222", connect_timeout=2.0)
    except Exception:
        pytest.skip("Local NATS broker not available on nats://127.0.0.1:4222")

    js = nc.jetstream()

    # Clean up test bucket if exists
    # Clean up test bucket and DLQ stream if exists
    dlq_stream_name = "LIVE_CRON_TEST_DLQ"
    for cleanup_op in (
        lambda: js.delete_key_value(bucket_name),
        lambda: js.delete_stream(dlq_stream_name),
    ):
        try:
            await cleanup_op()
        except Exception:
            pass

    # Create bucket with 60s TTL and DLQ stream
    kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["cluster.cron.dlq.*"])
    await js.add_stream(dlq_spec.to_stream_config())

    executions: list[str] = []

    class ClusterWorker(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self, node_id: str):
            self.node_id = node_id
            self.instance_id = node_id
            super().__init__(
                ServiceConfig(
                    name="cluster_worker",
                    nats_url="nats://127.0.0.1:4222",
                    jetstream_enabled=True,
                    dlq_subject="cluster.cron.dlq.worker",
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name, eager=True)
        async def sweep(self):
            executions.append(self.node_id)

    # Spawn 3 replicas
    replicas = [ClusterWorker(f"node_{i}") for i in range(3)]

    try:
        # Start all 3 replicas concurrently
        await asyncio.gather(*(rep.start() for rep in replicas))

        # Allow time for eager distributed execution to coordinate
        await asyncio.sleep(0.3)

        # Exactly ONE replica should have executed the eager distributed sweep!
        assert len(executions) == 1
        winner = executions[0]
        assert winner in ("node_0", "node_1", "node_2")

        # Verify KV record exists and records winner
        entry = await kv_store.get("cron.cluster_worker.sweep.eager")
        assert entry is not None
        record = json.loads(entry.value.decode("utf-8"))
        assert record["replica"] == winner
        assert record["status"] == "completed"

    finally:
        for rep in replicas:
            try:
                await rep.stop()
            except Exception:
                pass
        for cleanup_op in (
            lambda: js.delete_key_value(bucket_name),
            lambda: js.delete_stream(dlq_stream_name),
        ):
            try:
                await cleanup_op()
            except Exception:
                pass
        await nc.close()

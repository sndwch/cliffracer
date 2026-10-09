"""Comprehensive unit and integration tests for distributed leader-elected cron."""

import asyncio
import json
import uuid
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
from conftest import broker_url

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
        self.in_flight = 0
        self.max_in_flight = 0

    async def _arrive(self) -> None:
        """Where a real call would wait on the network: other callers run, then this one's effect
        is applied atomically, as the server applies it. Without a suspension point the callers
        of a `gather` run one after another and nothing competes."""
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1

    async def create(self, key: str, value: bytes) -> int:
        self.create_calls += 1
        await self._arrive()
        if key in self.store:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self._current_rev += 1
        self.store[key] = value
        self.revisions[key] = self._current_rev
        return self._current_rev

    async def get(self, key: str) -> FakeKVEntry:
        await self._arrive()
        if key not in self.store:
            raise nats.js.errors.KeyNotFoundError()
        return FakeKVEntry(key, self.store[key], self.revisions[key])

    async def put(self, key: str, value: bytes) -> int:
        self.put_calls += 1
        await self._arrive()
        self._current_rev += 1
        self.store[key] = value
        self.revisions[key] = self._current_rev
        return self._current_rev

    async def update(self, key: str, value: bytes, last: int) -> int:
        self.update_calls += 1
        await self._arrive()
        if key not in self.store or self.revisions.get(key) != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self._current_rev += 1
        self.store[key] = value
        self.revisions[key] = self._current_rev
        return self._current_rev

    async def delete(self, key: str, last: int | None = None) -> None:
        self.delete_calls += 1
        if last and self.revisions.get(key) != last:
            # What a real bucket answers a compare-and-delete whose revision has moved on with:
            # a plain BadRequestError carrying the API error code, not KeyWrongLastSequenceError.
            raise nats.js.errors.BadRequestError(
                code=400,
                err_code=10071,
                description=f"wrong last sequence: {self.revisions.get(key)}",
            )
        self.store.pop(key, None)
        self.revisions.pop(key, None)


class FakeKvExtension:
    name = "kv"

    def __init__(self, bucket_handle: FakeKeyValueBucket):
        self.bucket_handle = bucket_handle
        self._bucket_configs: dict[str, Any] = {}

    async def get_bucket(self, bucket: str, *, default_config=None) -> FakeKeyValueBucket:
        return self.bucket_handle


class TestDistributedCronKeyValidation:
    def test_key_sanitization_removes_colons_and_spaces(self):
        """NATS KV keys must match ^[-/_=\\.a-zA-Z0-9]+$; colons and spaces are sanitized."""
        dirty_service = "my:service space"
        clean = _sanitize_key_part(dirty_service)
        assert ":" not in clean
        assert " " not in clean
        assert clean == "my_service_space"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("service", "method", "expected"),
        [
            ("orders_worker", "hourly_sweep", "cron.orders_worker.hourly_sweep.1773180000"),
            ("orders worker:v1", "hourly sweep", "cron.orders_worker_v1.hourly_sweep.1773180000"),
        ],
    )
    async def test_interval_key_structure(self, service, method, expected):
        """The interval key `_execute_distributed` writes is `cron.{service}.{method}.{epoch}`,
        sanitized to what a NATS KV key allows: read from the bucket, not rebuilt here."""
        import re

        shared_kv = FakeKeyValueBucket()
        timer = DistributedCronTimer(
            "0 * * * *", distributed=True, kv_extension=FakeKvExtension(shared_kv)
        )
        timer.method_name = method

        class Service:
            def __init__(self):
                self.instance_id = "pod_1"
                self.config = SimpleNamespace(name=service)

        async def job():
            pass

        setattr(Service, method, job)
        timer.service_instance = Service()

        await timer._execute_distributed(datetime.fromtimestamp(1773180000, tz=UTC))

        assert list(shared_kv.store) == [expected]
        assert re.match(r"^[-/_=\.a-zA-Z0-9]+$", expected)


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

    def test_the_timer_discovery_registers_keeps_what_the_decorator_declared(self):
        """Discovery registers `timer.clone()`, never the object the decorator built.

        `distributed=True` is hard-coded by `@cron`, so `timer.distributed` cannot fail; the
        declared bucket, lease and overlap setting can, and only a clone that drops them shows it.
        """

        class ServiceWithKV(CliffracerService):
            kv = KvExtension(buckets=["cron_locks"])

            def __init__(self):
                super().__init__(ServiceConfig(name="declared_svc"))

            @cron("*/5 * * * *", distributed=True, bucket="b1", lease_ttl=7.5, no_overlap=True)
            async def sweep(self):
                pass

        svc = ServiceWithKV()
        svc.container.discover_handlers()
        (timer,) = svc.container.registry.timers

        assert isinstance(timer, DistributedCronTimer)
        assert timer.bucket == "b1"
        assert timer.lease_ttl == 7.5
        assert timer.no_overlap is True
        assert timer.method_name == "sweep"

    def test_clone_keeps_every_distributed_setting(self):
        """A clone of a timer built with non-default values has those values, including an
        explicit KvExtension handle."""
        handle = object()
        original = DistributedCronTimer(
            "*/5 * * * *", bucket="b1", lease_ttl=7.5, no_overlap=False, kv_extension=handle
        )
        original.method_name = "sweep"

        clone = original.clone()

        assert clone is not original
        assert (clone.bucket, clone.lease_ttl, clone.no_overlap) == ("b1", 7.5, False)
        assert clone._explicit_kv is handle
        assert clone.distributed is True
        assert clone.expression == "*/5 * * * *"
        assert clone.method_name == "sweep"

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

        # They did: all five claims were in flight at once, so the claim had to be atomic for
        # exactly one to win.
        assert shared_kv.max_in_flight == 5, shared_kv.max_in_flight
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
        job_started = asyncio.Event()
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
                executed_intervals.append(f"interval_{len(executed_intervals) + 1}")
                job_started.set()
                # Only the first run blocks. A second run, which the overlap check should have
                # prevented, returns at once so the assertion below reports it instead of the
                # test waiting for a gate nobody opens.
                if len(executed_intervals) == 1:
                    await job_gate.wait()

        timer.service_instance = SlowService()

        # Start interval 1 in background task
        target1 = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
        task1 = asyncio.create_task(timer._execute_distributed(target1))

        try:
            # The job starts only once the lease is written, so its start is what to wait for.
            await asyncio.wait_for(job_started.wait(), timeout=5.0)
            assert executed_intervals == ["interval_1"]

            # Now interval 2 arrives while interval 1 is still running!
            target2 = datetime(2026, 9, 10, 9, 1, 0, tzinfo=UTC)
            await timer._execute_distributed(target2)

            # Interval 2 was skipped because interval 1 is active!
            assert executed_intervals == ["interval_1"], (
                f"the overlap check let a second interval run while the first was active: "
                f"{executed_intervals}"
            )
        finally:
            # Release interval 1 whatever happened above, so a failed assertion is not followed by
            # a task that outlives the test.
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


def _live_resource_names(token: str | None = None) -> SimpleNamespace:
    """The names the live test gives everything it creates on the broker, unique to one run.

    A fixed bucket, stream and DLQ subject made two runs against one broker destroy each other's
    state: both deleted the same bucket at both ends, and a second stream over the same subjects is
    refused as overlapping. The token keeps every name out of the other run's way.
    """
    token = token or uuid.uuid4().hex[:10]
    return SimpleNamespace(
        token=token,
        bucket=f"live_cron_{token}",
        stream=f"LIVE_CRON_DLQ_{token.upper()}",
        dlq_subjects=f"cluster.cron.dlq.{token}.*",
        dlq_subject=f"cluster.cron.dlq.{token}.worker",
        service=f"cluster_worker_{token}",
    )


def test_each_live_run_names_its_own_resources():
    first, second = _live_resource_names(), _live_resource_names()

    for field in ("bucket", "stream", "dlq_subjects", "dlq_subject", "service"):
        assert getattr(first, field) != getattr(second, field), field
    assert first.token in first.dlq_subject


def test_the_dlq_subject_a_live_run_publishes_to_is_covered_by_its_own_stream():
    names = _live_resource_names("abc123")

    prefix = names.dlq_subjects.removesuffix("*")
    assert names.dlq_subject.startswith(prefix)
    assert names.dlq_subject.count(".") == names.dlq_subjects.count(".")


def test_two_runs_do_not_share_a_stream_subject_so_neither_stream_overlaps_the_other():
    one, two = _live_resource_names("aaaa"), _live_resource_names("bbbb")

    assert one.dlq_subjects != two.dlq_subjects
    assert not one.dlq_subject.startswith(two.dlq_subjects.removesuffix("*"))
    assert not two.dlq_subject.startswith(one.dlq_subjects.removesuffix("*"))


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_jetstream_distributed_cron():
    """Verify distributed cron leader election with real NATS JetStream KV store.

    The suite's marker and its broker probe decide whether this runs; a broker that stops
    answering after that is a failure to report, not a reason to skip. Everything it creates
    carries a name unique to this run, so two runs on one broker cannot touch each other's.
    """
    import nats

    names = _live_resource_names()
    bucket_name = names.bucket
    dlq_stream_name = names.stream

    nc = await nats.connect(broker_url(), connect_timeout=2.0, allow_reconnect=False)

    js = nc.jetstream()

    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=[names.dlq_subjects])

    executions: list[str] = []

    class ClusterWorker(CliffracerService):
        kv = KvExtension(buckets=[bucket_name])

        def __init__(self, node_id: str):
            self.node_id = node_id
            self.instance_id = node_id
            super().__init__(
                ServiceConfig(
                    name=names.service,
                    nats_url=broker_url(),
                    jetstream_enabled=True,
                    dlq_subject=names.dlq_subject,
                    jetstream_streams=[dlq_spec],
                )
            )

        @cron("0 0 1 1 *", distributed=True, bucket=bucket_name, eager=True, lease_ttl=30.0)
        async def sweep(self):
            executions.append(self.node_id)

    replicas: list[ClusterWorker] = []

    try:
        # Create bucket with 60s TTL and DLQ stream. Inside the try, so a failure half-way
        # still deletes what was made: these names are this run's and nothing else will.
        kv_store = await js.create_key_value(bucket=bucket_name, ttl=60)
        await js.add_stream(dlq_spec.to_stream_config())

        # Spawn 3 replicas
        replicas = [ClusterWorker(f"node_{i}") for i in range(3)]

        # Start all 3 replicas concurrently
        await asyncio.gather(*(rep.start() for rep in replicas))

        # Allow time for eager distributed execution to coordinate
        await asyncio.sleep(0.3)

        # Exactly ONE replica should have executed the eager distributed sweep!
        assert len(executions) == 1
        winner = executions[0]
        assert winner in ("node_0", "node_1", "node_2")

        # Verify KV record exists and records winner
        entry = await kv_store.get(f"cron.{names.service}.sweep.eager")
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


class TestDistributedCronStats:
    def test_the_distributed_stats_carry_the_distributed_settings(self):
        timer = DistributedCronTimer(
            "0 9 * * *",
            distributed=True,
            bucket="billing_cron",
            lease_ttl=12.0,
            no_overlap=True,
            kv_extension=FakeKvExtension(FakeKeyValueBucket()),
        )
        timer.method_name = "sync_invoices"

        stats = timer.get_stats()

        assert stats["expression"] == "0 9 * * *"
        assert stats["distributed"] is True
        assert stats["bucket"] == "billing_cron"
        assert stats["lease_ttl"] == 12.0
        assert stats["no_overlap"] is True
        assert "interval" not in stats


class TestTheFakeBucketCompetes:
    @pytest.mark.asyncio
    async def test_CONTROL_concurrent_creates_interleave_and_one_wins(self):
        """What the single-winner test relies on: the callers are all in flight before any is
        applied, and the create is atomic, so exactly one succeeds."""
        bucket = FakeKeyValueBucket()

        results = await asyncio.gather(
            *(bucket.create("k", b"v") for _ in range(4)), return_exceptions=True
        )

        assert bucket.max_in_flight == 4
        assert sum(isinstance(r, int) for r in results) == 1
        assert sum(isinstance(r, nats.js.errors.KeyWrongLastSequenceError) for r in results) == 3


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_a_job_whose_lease_outlives_its_bucket_is_refused_at_start():
    """The refusal against a real bucket: its TTL is read as seconds, through the service's own lookup.

    A bucket another job opened with a 60 second TTL expires every key after a minute, so a job
    that asked for a ten minute lease is refused by name, and one that asked for thirty seconds is
    not. Everything it creates carries a name unique to this run.
    """
    import nats

    names = _live_resource_names()
    bucket_name = names.bucket
    nc = await nats.connect(broker_url(), connect_timeout=2.0, allow_reconnect=False)
    js = nc.jetstream()

    def service(lease_ttl: float) -> CliffracerService:
        class Worker(CliffracerService):
            kv = KvExtension(buckets=[bucket_name])

            def __init__(self) -> None:
                super().__init__(ServiceConfig(name=names.service, nats_url=broker_url()))

            @cron("0 0 1 1 *", distributed=True, bucket=bucket_name, lease_ttl=lease_ttl)
            async def sweep(self):
                return None

        return Worker()

    started: list[CliffracerService] = []
    try:
        await js.create_key_value(bucket=bucket_name, ttl=60)

        refused = service(600.0)
        started.append(refused)
        with pytest.raises(ConfigurationError, match=r"lease_ttl=600s.*expires its keys after 60s"):
            await refused.start()

        fits = service(30.0)
        started.append(fits)
        await fits.start()
    finally:
        for svc in started:
            try:
                await svc.stop()
            except Exception:
                pass
        try:
            await js.delete_key_value(bucket_name)
        except Exception:
            pass
        await nc.close()

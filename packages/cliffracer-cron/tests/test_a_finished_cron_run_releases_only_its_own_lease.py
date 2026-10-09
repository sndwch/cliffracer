"""A cron run that finishes removes the active lease only if it is still the one it wrote.

`no_overlap` keeps one lease per job, and a run older than `lease_ttl` is run over by the next:
the next run writes its own lease over the stale one. The first run, finishing afterwards, used to
delete the key whatever it held, which removed the second run's lease and let a third run start
beside the second. The release is a compare-and-delete on the revision the run's own write returned.

The bucket here applies `last=` as the server does, including the error it answers with: a plain
`BadRequestError` carrying API error 10071, not `KeyWrongLastSequenceError`.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer, distributed
from loguru import logger

pytestmark = pytest.mark.unit

ACTIVE = "cron.jobs.run.active"
LEASE_TTL = 10.0


class Entry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class Bucket:
    """Create, get, put, update and a revision-checked delete, applied atomically."""

    def __init__(self, delete_error: str = "bad_request") -> None:
        self.entries: dict[str, Entry] = {}
        self.revision = 0
        self.delete_error = delete_error
        self.deletes: list[tuple[str, int | None]] = []
        self.fail_put = False

    def _write(self, key: str, value: bytes) -> int:
        self.revision += 1
        self.entries[key] = Entry(value, self.revision)
        return self.revision

    async def create(self, key: str, value: bytes) -> int:
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._write(key, value)

    async def get(self, key: str) -> Entry:
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        return self.entries[key]

    async def put(self, key: str, value: bytes) -> int:
        if self.fail_put and key == ACTIVE:
            raise nats.js.errors.ServiceUnavailableError()
        return self._write(key, value)

    async def update(self, key: str, value: bytes, last: int) -> int:
        if key not in self.entries or self.entries[key].revision != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._write(key, value)

    async def delete(self, key: str, last: int | None = None) -> None:
        self.deletes.append((key, last))
        if last and (key not in self.entries or self.entries[key].revision != last):
            if self.delete_error == "bad_request":
                raise nats.js.errors.BadRequestError(
                    code=400, err_code=10071, description="wrong last sequence"
                )
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries.pop(key, None)

    def holder(self) -> str | None:
        entry = self.entries.get(ACTIVE)
        return json.loads(entry.value)["replica"] if entry else None


class Replica:
    """One replica of the job. Its run blocks until `release()` is called."""

    def __init__(self, name: str, bucket: Bucket) -> None:
        self.name = name
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.ran = 0
        self.timer = DistributedCronTimer(
            "* * * * *", no_overlap=True, lease_ttl=LEASE_TTL, kv_extension=SimpleNamespace()
        )
        self.timer.method_name = "run"
        self.timer.service_instance = SimpleNamespace(
            instance_id=name, config=SimpleNamespace(name="jobs")
        )

        async def raw_bucket() -> Bucket:
            return bucket

        async def job() -> None:
            self.ran += 1
            self.started.set()
            await self.gate.wait()

        self.timer._get_raw_bucket = raw_bucket  # type: ignore[method-assign]
        self.timer._execute_method = job  # type: ignore[method-assign]

    def fire(self, minute: int) -> "asyncio.Task[None]":
        return asyncio.ensure_future(
            self.timer._execute_distributed(datetime(2026, 9, 10, 9, minute, 0, tzinfo=UTC))
        )

    def release(self) -> None:
        self.gate.set()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(distributed, "time", SimpleNamespace(time=lambda: now[0]))
    return now


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_error", ["bad_request", "key_wrong_last_sequence"])
async def test_a_run_that_finishes_after_being_run_over_leaves_the_later_runs_lease(
    clock, delete_error
):
    bucket = Bucket(delete_error)
    a, b, c = (Replica(n, bucket) for n in ("a", "b", "c"))

    task_a = a.fire(0)
    await asyncio.wait_for(a.started.wait(), 5)
    clock[0] = 1000.0 + LEASE_TTL + 1  # A has now outlived lease_ttl
    task_b = b.fire(1)
    await asyncio.wait_for(b.started.wait(), 5)
    assert bucket.holder() == "b", "the stale lease was not taken over"

    a.release()
    await task_a

    assert bucket.holder() == "b", (
        f"the finished run removed the lease of the run beside it: {bucket.entries}"
    )
    clock[0] += 1
    await c.fire(2)
    assert c.ran == 0, "a third run started beside a second that was still going"

    b.release()
    await task_b
    assert bucket.holder() is None, "the run that holds the lease did not release it"


@pytest.mark.asyncio
async def test_CONTROL_a_run_with_nothing_beside_it_still_clears_its_lease(clock):
    bucket = Bucket()
    a = Replica("a", bucket)

    task = a.fire(0)
    await asyncio.wait_for(a.started.wait(), 5)
    assert bucket.holder() == "a"
    a.release()
    await task

    assert bucket.holder() is None


@pytest.mark.asyncio
async def test_the_release_names_the_revision_the_runs_own_write_returned(clock):
    bucket = Bucket()
    a = Replica("a", bucket)

    task = a.fire(0)
    await asyncio.wait_for(a.started.wait(), 5)
    written = bucket.entries[ACTIVE].revision
    a.release()
    await task

    assert bucket.deletes == [(ACTIVE, written)], bucket.deletes


@pytest.mark.asyncio
async def test_losing_the_lease_to_a_later_run_is_not_logged_as_a_failure(clock):
    bucket = Bucket()
    a, b = Replica("a", bucket), Replica("b", bucket)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name}: {m.record['message']}"))
    try:
        task_a = a.fire(0)
        await asyncio.wait_for(a.started.wait(), 5)
        clock[0] += LEASE_TTL + 1
        task_b = b.fire(1)
        await asyncio.wait_for(b.started.wait(), 5)
        a.release()
        await task_a
        b.release()
        await task_b
    finally:
        logger.remove(sink)

    assert not [line for line in lines if "Failed to clear active lease" in line], lines
    assert any("now belongs to a later run" in line for line in lines), lines


@pytest.mark.asyncio
async def test_a_run_whose_lease_write_failed_deletes_nothing(clock):
    bucket = Bucket()
    bucket.fail_put = True
    a = Replica("a", bucket)
    bucket.entries[ACTIVE] = Entry(
        json.dumps({"status": "running", "started_at": 0.0, "replica": "other"}).encode(), 99
    )

    task = a.fire(0)
    await asyncio.wait_for(a.started.wait(), 5)
    a.release()
    await task

    assert bucket.deletes == [], "a run with no lease of its own deleted the key"
    assert bucket.holder() == "other"


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_a_real_bucket_keeps_the_later_runs_lease_and_answers_a_stale_delete_as_assumed(
    clock,
):
    """The same scenario on a real KV bucket, so the error shape the release tolerates is the
    server's own and not this module's rendering of it."""
    import uuid

    import nats

    from conftest import broker_url

    nc = await nats.connect(broker_url(), connect_timeout=2.0, allow_reconnect=False)
    name = f"lease_{uuid.uuid4().hex[:10]}"
    js = nc.jetstream()
    kv = await js.create_key_value(bucket=name)
    try:
        a, b, c = (Replica(n, kv) for n in ("a", "b", "c"))  # type: ignore[arg-type]
        task_a = a.fire(0)
        await asyncio.wait_for(a.started.wait(), 5)
        clock[0] += LEASE_TTL + 1
        task_b = b.fire(1)
        await asyncio.wait_for(b.started.wait(), 5)

        a.release()
        await task_a
        held = json.loads((await kv.get(ACTIVE)).value)["replica"]
        assert held == "b", "the finished run removed the lease of the run beside it"

        clock[0] += 1
        await c.fire(2)
        assert c.ran == 0

        b.release()
        await task_b
        with pytest.raises(nats.js.errors.KeyNotFoundError):
            await kv.get(ACTIVE)
    finally:
        await js.delete_key_value(name)
        await nc.close()

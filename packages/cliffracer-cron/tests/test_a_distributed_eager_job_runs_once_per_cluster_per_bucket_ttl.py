"""`eager` on a distributed cron job runs once per cluster per bucket TTL, not on every start.

The first start takes the `.eager` key and leaves it, so a restart, or a rolling deploy of several
replicas, inside the bucket's TTL does not run the job again on this replica or any other, and on a
bucket with no TTL it is never run again. That is what `distributed=True` is for, and the
documentation says so: a job that has to run after every start belongs in the service's
`on_startup`. These tests run the timer's loop as a service start does, with the wait for the next
occurrence cut short, and count the eager runs.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer
from loguru import logger

pytestmark = pytest.mark.unit

EAGER_KEY = "cron.svc.job.eager"


class Bucket:
    """The compare-and-set surface a firing uses, in memory, shared by every replica."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self._revisions: dict[str, int] = {}
        self._clock = 0

    def _write(self, key: str, value: bytes) -> int:
        self._clock += 1
        self.store[key] = value
        self._revisions[key] = self._clock
        return self._clock

    async def create(self, key: str, value: bytes) -> int:
        if key in self.store:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._write(key, value)

    async def update(self, key: str, value: bytes, last: int) -> int:
        if self._revisions.get(key) != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._write(key, value)

    async def put(self, key: str, value: bytes) -> int:
        return self._write(key, value)

    async def get(self, key: str) -> Any:
        if key not in self.store:
            raise nats.js.errors.KeyNotFoundError()
        return SimpleNamespace(key=key, value=self.store[key], revision=self._revisions[key])

    async def delete(self, key: str, last: int | None = None) -> None:
        if last and self._revisions.get(key) != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.store.pop(key, None)
        self._revisions.pop(key, None)

    def expire(self, key: str) -> None:
        """What the bucket's TTL does to a key once it is old enough."""
        self.store.pop(key, None)
        self._revisions.pop(key, None)


class Kv:
    name = "kv"

    def __init__(self, bucket: Bucket) -> None:
        self._bucket = bucket

    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> Bucket:
        return self._bucket


class Replica:
    """One service process: its own timer and its own count of eager runs."""

    def __init__(self, bucket: Bucket, name: str) -> None:
        self.ran: list[str] = []
        replica = self
        self.service = SimpleNamespace(config=SimpleNamespace(name="svc"), instance_id=name)

        async def job() -> None:
            replica.ran.append(name)

        self.service.job = job
        self.timer = DistributedCronTimer(
            "0 9 * * *", eager=True, distributed=True, kv_extension=Kv(bucket)
        )
        self.timer.method_name = "job"
        self.timer.service_instance = self.service

    async def start(self) -> None:
        """A service start: the loop's eager firing, then it would wait for the schedule."""

        async def no_wait() -> None:
            return None

        self.timer._wait_for_the_next_occurrence = no_wait  # type: ignore[method-assign]
        self.timer.is_running = True
        await self.timer._timer_loop()


async def _starts(bucket: Bucket, *names: str) -> list[Replica]:
    replicas = [Replica(bucket, name) for name in names]
    for replica in replicas:
        await replica.start()
    return replicas


async def test_a_restart_inside_the_bucket_ttl_does_not_run_the_eager_job_again():
    bucket = Bucket()

    first, second = await _starts(bucket, "replica_a", "replica_b")

    assert first.ran == ["replica_a"]
    assert second.ran == [], "a start inside the TTL ran the eager job again"


async def test_a_restart_of_the_same_replica_does_not_run_it_either():
    bucket = Bucket()

    again = await _starts(bucket, "replica_a", "replica_a")

    assert [replica.ran for replica in again] == [["replica_a"], []]


async def test_a_rolling_deploy_of_several_replicas_runs_it_once():
    bucket = Bucket()

    replicas = await _starts(bucket, "r1", "r2", "r3", "r4")

    assert sum(len(replica.ran) for replica in replicas) == 1


async def test_replicas_that_start_together_run_it_once():
    bucket = Bucket()
    replicas = [Replica(bucket, name) for name in ("r1", "r2", "r3")]

    await asyncio.gather(*(replica.start() for replica in replicas))

    assert sum(len(replica.ran) for replica in replicas) == 1


async def test_the_start_that_skips_it_says_a_peer_has_it():
    bucket = Bucket()
    said: list[str] = []
    sink = logger.add(lambda m: said.append(m.record["message"]), level="INFO")
    try:
        await _starts(bucket, "replica_a", "replica_b")
    finally:
        logger.remove(sink)

    assert any("eager" in line and "already acquired by peer replica" in line for line in said)


async def test_the_eager_key_is_left_in_place_for_the_bucket_to_expire():
    bucket = Bucket()

    await _starts(bucket, "replica_a")

    record = json.loads(bucket.store[EAGER_KEY].decode("utf-8"))
    assert record["status"] == "completed" and record["replica"] == "replica_a"


async def test_a_bucket_that_never_expires_the_key_never_runs_the_eager_job_again():
    """The window is however long the bucket keeps the key: a bucket with no TTL keeps it for ever."""
    bucket = Bucket()
    await _starts(bucket, "replica_a")

    later = await _starts(bucket, "replica_b", "replica_c", "replica_a")

    assert [replica.ran for replica in later] == [[], [], []]
    assert EAGER_KEY in bucket.store


async def test_CONTROL_a_start_after_the_bucket_expired_the_key_runs_it_again():
    bucket = Bucket()
    await _starts(bucket, "replica_a")
    bucket.expire(EAGER_KEY)

    (later,) = await _starts(bucket, "replica_b")

    assert later.ran == ["replica_b"]


async def test_CONTROL_a_start_with_no_eager_runs_nothing_at_start():
    bucket = Bucket()
    replica = Replica(bucket, "replica_a")
    replica.timer.eager = False

    await replica.start()

    assert replica.ran == [] and EAGER_KEY not in bucket.store

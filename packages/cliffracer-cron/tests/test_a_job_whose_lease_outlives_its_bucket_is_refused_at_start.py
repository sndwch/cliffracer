"""A distributed cron job whose `lease_ttl` the bucket cannot honour is refused when it starts.

The bucket's TTL is set by whichever job opened it first, and every key in it, an active lease
included, expires after it. A job that asked for a one-hour lease on a bucket that expires keys after
five minutes had its overlap lease vanish after five, with no sign: `lease_ttl` is documented as how
long a lease lives. A bucket that keeps keys longer than a job asked loses nothing and is not
refused.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer

from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

FIRST = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
SECOND = datetime(2026, 9, 11, 9, 0, 0, tzinfo=UTC)


class FakeKeyValueBucket:
    """The compare-and-set surface of a KV bucket that a firing uses, in memory."""

    def __init__(self, ttl: float | None = None) -> None:
        self.store: dict[str, bytes] = {}
        self._revisions: dict[str, int] = {}
        self._clock = 0
        self._ttl = ttl

    def _write(self, key: str, value: bytes) -> int:
        self._clock += 1
        self.store[key] = value
        self._revisions[key] = self._clock
        return self._clock

    async def status(self) -> Any:
        return SimpleNamespace(ttl=self._ttl)

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


class FakeKvExtension:
    name = "kv"

    def __init__(self, bucket: FakeKeyValueBucket) -> None:
        self._bucket = bucket

    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> FakeKeyValueBucket:
        return self._bucket


class Service:
    def __init__(self) -> None:
        self.config = SimpleNamespace(name="reports")
        self.instance_id = "replica_a"

    async def nightly(self) -> None:
        return None


def _timer(bucket: FakeKeyValueBucket, lease_ttl: float) -> DistributedCronTimer:
    timer = DistributedCronTimer(
        "0 9 * * *",
        distributed=True,
        kv_extension=FakeKvExtension(bucket),
        bucket="cron_locks",
        lease_ttl=lease_ttl,
    )
    timer.method_name = "nightly"
    return timer


async def test_a_lease_longer_than_the_bucket_keeps_a_key_is_refused_and_the_error_names_both():
    timer = _timer(FakeKeyValueBucket(ttl=300.0), lease_ttl=3600.0)

    try:
        with pytest.raises(ConfigurationError) as refused:
            await timer.start(Service())
    finally:
        await timer.stop()

    text = str(refused.value)
    assert "nightly" in text and "cron_locks" in text
    assert "3600s" in text and "300s" in text
    assert "whichever job opened it first" in text
    assert not timer.is_running


class HoldsKv(Service):
    """A service that declares its KvExtension as an attribute, as a real one does."""

    def __init__(self, bucket: FakeKeyValueBucket) -> None:
        super().__init__()
        self.kv = FakeKvExtension(bucket)


async def test_the_bucket_is_found_through_the_service_the_job_is_started_on():
    """A real timer is given no extension of its own: it finds the bucket through the service.

    The check ran before the service was recorded on the timer, so that lookup found nothing, the
    TTL went unread, and the job started on every live broker. The tests above give the timer its
    extension directly and cannot see that.
    """
    timer = DistributedCronTimer(
        "0 9 * * *", distributed=True, bucket="cron_locks", lease_ttl=3600.0
    )
    timer.method_name = "nightly"

    try:
        with pytest.raises(ConfigurationError, match="3600s"):
            await timer.start(HoldsKv(FakeKeyValueBucket(ttl=300.0)))
    finally:
        await timer.stop()


async def test_CONTROL_a_job_found_through_the_service_starts_when_the_bucket_holds_its_lease():
    timer = DistributedCronTimer(
        "0 9 * * *", distributed=True, bucket="cron_locks", lease_ttl=300.0
    )
    timer.method_name = "nightly"

    await timer.start(HoldsKv(FakeKeyValueBucket(ttl=300.0)))
    try:
        assert timer.is_running
    finally:
        await timer.stop()


@pytest.mark.parametrize(
    ("bucket_ttl", "lease_ttl"),
    [(300.0, 300.0), (3600.0, 300.0), (300.0, 60.0), (0, 3600.0), (None, 3600.0)],
    ids=["equal", "bucket longer", "lease shorter", "no expiry (0)", "no expiry (None)"],
)
async def test_CONTROL_a_bucket_that_holds_the_lease_starts_the_job(bucket_ttl, lease_ttl):
    timer = _timer(FakeKeyValueBucket(ttl=bucket_ttl), lease_ttl=lease_ttl)

    await timer.start(Service())
    try:
        assert timer.is_running
    finally:
        await timer.stop()


async def test_a_bucket_whose_ttl_cannot_be_read_does_not_stop_the_job_starting():
    class Unreadable(FakeKeyValueBucket):
        async def status(self) -> Any:
            raise RuntimeError("no such stream")

    timer = _timer(Unreadable(ttl=300.0), lease_ttl=3600.0)

    await timer.start(Service())
    try:
        assert timer.is_running
    finally:
        await timer.stop()


async def test_a_job_without_no_overlap_holds_no_lease_and_is_not_refused_for_one():
    """The check protects the active lease, which only `no_overlap` writes."""
    timer = DistributedCronTimer(
        "0 9 * * *",
        distributed=True,
        kv_extension=FakeKvExtension(FakeKeyValueBucket(ttl=300.0)),
        bucket="cron_locks",
        lease_ttl=3600.0,
        no_overlap=False,
    )
    timer.method_name = "nightly"

    await timer.start(Service())
    try:
        assert timer.is_running
    finally:
        await timer.stop()

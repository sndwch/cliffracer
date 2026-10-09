"""A distributed cron run that the service cancels is recorded as cancelled, not as completed.

`Timer.stop()` cancels an in-flight run once its grace has passed, so this is the ordinary
shutdown path. The record says `running`, `failed`, `refused` or `completed`, and a cancelled run
matched none of the branches that set the first three, so it was written as `completed`: the one
durable account of the interval said a killed run had succeeded. The cancellation is still raised,
the active lease is still released, and the interval record stays so a peer replica does not run an
interval that was cancelled.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer

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


def _record(bucket: FakeKeyValueBucket, when: datetime) -> dict:
    return json.loads(bucket.store[f"cron.reports.nightly.{int(when.timestamp())}"])


class Waits:
    """A service whose `nightly` handler runs until it is cancelled."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(name="reports")
        self.instance_id = "replica_a"
        self.started = asyncio.Event()
        self.calls = 0

    async def nightly(self) -> None:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


class Finishes:
    def __init__(self) -> None:
        self.config = SimpleNamespace(name="reports")
        self.instance_id = "replica_a"

    async def nightly(self) -> None:
        return None


def _timer(service: Any) -> tuple[DistributedCronTimer, FakeKeyValueBucket]:
    bucket = FakeKeyValueBucket()
    timer = DistributedCronTimer(
        "0 9 * * *", distributed=True, kv_extension=FakeKvExtension(bucket)
    )
    timer.method_name = "nightly"
    timer.service_instance = service
    return timer, bucket


async def _cancelled_mid_run() -> tuple[Waits, DistributedCronTimer, FakeKeyValueBucket]:
    service = Waits()
    timer, bucket = _timer(service)
    task = asyncio.create_task(timer._execute_distributed(FIRST))
    await asyncio.wait_for(service.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return service, timer, bucket


async def test_a_run_cancelled_mid_flight_leaves_a_cancelled_record():
    _, _, bucket = await _cancelled_mid_run()

    record = _record(bucket, FIRST)

    assert record["status"] == "cancelled"
    assert "error" not in record and "refusal" not in record
    assert record["duration_ms"] >= 0 and record["completed_at"] >= record["started_at"]


async def test_the_cancellation_reaches_the_caller_and_the_lease_is_released():
    _, _, bucket = await _cancelled_mid_run()

    assert "cron.reports.nightly.active" not in bucket.store


async def test_a_cancelled_interval_is_not_run_again_by_a_peer():
    service, timer, _ = await _cancelled_mid_run()

    await timer._execute_distributed(FIRST)

    assert service.calls == 1, "the interval that was cancelled ran again"


async def test_CONTROL_a_run_that_finishes_is_still_recorded_as_completed():
    timer, bucket = _timer(Finishes())

    await timer._execute_distributed(FIRST)

    assert _record(bucket, FIRST)["status"] == "completed"

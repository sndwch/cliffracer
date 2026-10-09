"""The interval record a distributed cron firing leaves in KV says whether it failed.

The record is the only durable account of a run, so a handler that raised has to
leave ``status: failed`` and what failed there, and a handler that did not has to
leave neither. What failed is the exception's type, and its text too only where
``expose_internal_errors`` lets an exception's text leave the process: the record
is read by whoever can read the bucket.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer

pytestmark = pytest.mark.unit

FIRST = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
SECOND = datetime(2026, 9, 11, 9, 0, 0, tzinfo=UTC)


class FakeKeyValueBucket:
    """The compare-and-set surface of a KV bucket that a firing uses, in memory."""

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


class FakeKvExtension:
    name = "kv"

    def __init__(self, bucket: FakeKeyValueBucket) -> None:
        self._bucket = bucket

    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> FakeKeyValueBucket:
        return self._bucket


class Reports:
    """A service whose ``nightly`` handler raises for the failing epochs."""

    def __init__(self, failures: dict[str, Exception], *, expose: bool | None = None) -> None:
        self.config = SimpleNamespace(name="reports")
        if expose is not None:
            self.config.expose_internal_errors = expose
        self.instance_id = "replica_a"
        self._failures = failures
        self.calls = 0

    async def nightly(self) -> None:
        self.calls += 1
        failure = self._failures.get(str(self.calls))
        if failure is not None:
            raise failure


def _timer(service: Reports) -> tuple[DistributedCronTimer, FakeKeyValueBucket]:
    bucket = FakeKeyValueBucket()
    timer = DistributedCronTimer(
        "0 9 * * *", distributed=True, kv_extension=FakeKvExtension(bucket)
    )
    timer.method_name = "nightly"
    timer.service_instance = service
    return timer, bucket


def _record(bucket: FakeKeyValueBucket, when: datetime) -> dict:
    key = f"cron.reports.nightly.{int(when.timestamp())}"
    return json.loads(bucket.store[key].decode("utf-8"))


@pytest.mark.asyncio
async def test_a_handler_that_raises_leaves_a_failed_record_naming_its_type_and_not_its_text():
    timer, bucket = _timer(Reports({"1": ValueError("ledger closed early")}))

    await timer._execute_distributed(FIRST)

    record = _record(bucket, FIRST)
    assert record["status"] == "failed"
    assert record["error"] == "ValueError"


@pytest.mark.asyncio
@pytest.mark.parametrize("expose", [None, False])
async def test_a_credential_in_an_exception_does_not_reach_the_record_unless_exposed(expose):
    failure = RuntimeError("could not log in to db: password=hunter2")
    timer, bucket = _timer(Reports({"1": failure}, expose=expose))

    await timer._execute_distributed(FIRST)

    stored = bucket.store[f"cron.reports.nightly.{int(FIRST.timestamp())}"].decode("utf-8")
    assert "hunter2" not in stored
    assert _record(bucket, FIRST)["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_expose_internal_errors_puts_the_text_in_the_record_as_well():
    timer, bucket = _timer(Reports({"1": ValueError("ledger closed early")}, expose=True))

    await timer._execute_distributed(FIRST)

    record = _record(bucket, FIRST)
    assert record["status"] == "failed"
    assert "ledger closed early" in record["error"]
    assert "ValueError" in record["error"]


@pytest.mark.asyncio
async def test_a_failure_that_escapes_the_timer_is_gated_the_same_way(monkeypatch):
    for expose, expected in ((False, "RuntimeError"), (True, "RuntimeError: password=hunter2")):
        timer, bucket = _timer(Reports({}, expose=expose))

        async def escapes() -> None:
            raise RuntimeError("password=hunter2")

        monkeypatch.setattr(timer, "_execute_method", escapes)

        with pytest.raises(RuntimeError):
            await timer._execute_distributed(FIRST)

        assert _record(bucket, FIRST)["error"] == expected


@pytest.mark.asyncio
async def test_the_gate_reads_the_flag_itself_and_not_a_truthy_stand_in():
    timer, bucket = _timer(Reports({"1": ValueError("ledger closed early")}))
    timer.service_instance.config.expose_internal_errors = MagicMock()  # truthy, and not True

    await timer._execute_distributed(FIRST)

    assert _record(bucket, FIRST)["error"] == "ValueError"


@pytest.mark.asyncio
async def test_a_handler_that_raises_is_still_counted_once_and_does_not_escape():
    timer, _ = _timer(Reports({"1": RuntimeError("boom")}))

    await timer._execute_distributed(FIRST)

    assert timer.error_count == 1


@pytest.mark.asyncio
async def test_an_exception_with_no_message_still_leaves_a_failed_record():
    timer, bucket = _timer(Reports({"1": ValueError()}))

    await timer._execute_distributed(FIRST)

    record = _record(bucket, FIRST)
    assert record["status"] == "failed"
    assert record["error"] == "ValueError"


@pytest.mark.asyncio
async def test_a_handler_that_succeeds_leaves_a_completed_record_without_an_error():
    timer, bucket = _timer(Reports({}))

    await timer._execute_distributed(FIRST)

    record = _record(bucket, FIRST)
    assert record["status"] == "completed"
    assert "error" not in record


@pytest.mark.asyncio
async def test_a_failure_does_not_leak_into_the_next_interval_record():
    timer, bucket = _timer(Reports({"1": RuntimeError("first run failed")}))

    await timer._execute_distributed(FIRST)
    await timer._execute_distributed(SECOND)

    assert _record(bucket, FIRST)["status"] == "failed"
    second = _record(bucket, SECOND)
    assert second["status"] == "completed"
    assert "error" not in second

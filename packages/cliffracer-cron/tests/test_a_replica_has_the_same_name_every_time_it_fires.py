"""The replica a distributed cron record names is one process, and the same one on every firing.

`instance_id` and `_instance_id` are attributes nothing in the framework defines, so for a real
service the name fell through to a random id made on each firing: `prior run still active on
replica 'replica-3fa9...'` matched no pod and no log line, and one replica had a different name in
every interval's record. It is `<hostname>-<pid>` unless the service sets one.
"""

import json
import os
import socket
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


class Anonymous:
    """A service that defines no instance id, as a real one does not."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(name="reports")

    async def nightly(self) -> None:
        return None


class Named(Anonymous):
    def __init__(self, **attributes: str) -> None:
        super().__init__()
        for name, value in attributes.items():
            setattr(self, name, value)


async def _replicas_named(service: Any) -> list[str]:
    bucket = FakeKeyValueBucket()
    timer = DistributedCronTimer(
        "0 9 * * *", distributed=True, kv_extension=FakeKvExtension(bucket)
    )
    timer.method_name = "nightly"
    timer.service_instance = service
    names = []
    for when in (FIRST, SECOND):
        await timer._execute_distributed(when)
        names.append(_record(bucket, when)["replica"])
    return names


async def test_a_service_with_no_instance_id_is_the_same_replica_on_every_firing():
    first, second = await _replicas_named(Anonymous())

    assert first == second == f"{socket.gethostname()}-{os.getpid()}"


async def test_the_replica_is_named_for_the_host_and_process_not_a_random_value():
    first, _ = await _replicas_named(Anonymous())

    assert not first.startswith("replica-")


@pytest.mark.parametrize("attribute", ["instance_id", "_instance_id"])
async def test_CONTROL_an_instance_id_the_service_sets_is_used(attribute):
    names = await _replicas_named(Named(**{attribute: "pod-7"}))

    assert names == ["pod-7", "pod-7"]

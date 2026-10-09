"""Two apps that share a broker, a bucket and a service name do not take each other's cron locks.

`namespace` is what lets multiple apps run on one NATS server under the same service names. The
distributed cron keys were `cron.<service>.<method>.<epoch>` and `cron.<service>.<method>.active`,
so two apps with one service name and one schedule contended for the same key at every firing, and
one app's job never ran. The namespace is now part of every key built for a job, in the same bucket.
A service with no namespace keeps the keys it always had.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer

pytestmark = pytest.mark.unit

FIRING = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
EPOCH = str(int(FIRING.timestamp()))


class Entry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class Bucket:
    """The calls `_execute_distributed` makes, with `create` as the compare-and-set on absence."""

    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}
        self.revision = 0

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
        return self._write(key, value)

    async def update(self, key: str, value: bytes, last: int | None = None) -> int:
        return self._write(key, value)

    async def delete(self, key: str, last: int | None = None) -> None:
        self.entries.pop(key, None)


class Replica:
    def __init__(self, bucket: Bucket, *, namespace: str | None, service: str = "billing") -> None:
        self.ran = 0
        self.timer = DistributedCronTimer(
            "* * * * *", no_overlap=True, kv_extension=SimpleNamespace()
        )
        self.timer.method_name = "tick"
        self.timer.service_instance = SimpleNamespace(
            instance_id=f"{namespace}-{service}",
            config=SimpleNamespace(name=service, namespace=namespace),
        )

        async def raw_bucket() -> Bucket:
            return bucket

        async def job() -> None:
            self.ran += 1

        self.timer._get_raw_bucket = raw_bucket  # type: ignore[method-assign]
        self.timer._execute_method = job  # type: ignore[method-assign]

    async def fire(self, *, eager: bool = False) -> None:
        await self.timer._execute_distributed(FIRING, eager=eager)


@pytest.mark.asyncio
async def test_two_apps_with_one_service_name_each_run_the_same_firing():
    bucket = Bucket()
    app1, app2 = Replica(bucket, namespace="app1"), Replica(bucket, namespace="app2")

    await app1.fire()
    await app2.fire()

    assert (app1.ran, app2.ran) == (1, 1), sorted(bucket.entries)


@pytest.mark.asyncio
async def test_the_keys_name_the_namespace_and_differ_between_apps():
    bucket = Bucket()
    await Replica(bucket, namespace="app1").fire()
    await Replica(bucket, namespace="app2").fire()

    interval = {k for k in bucket.entries if k.endswith(f".{EPOCH}")}
    assert interval == {f"cron.app1.billing.tick.{EPOCH}", f"cron.app2.billing.tick.{EPOCH}"}


@pytest.mark.asyncio
async def test_the_lease_keys_of_two_apps_differ():
    seen: list[str] = []

    class Spy(Bucket):
        async def put(self, key: str, value: bytes) -> int:
            seen.append(key)
            return await super().put(key, value)

    bucket = Spy()
    await Replica(bucket, namespace="app1").fire()
    await Replica(bucket, namespace="app2").fire()

    assert seen == ["cron.app1.billing.tick.active", "cron.app2.billing.tick.active"]


@pytest.mark.asyncio
async def test_the_eager_lock_carries_the_namespace_too():
    bucket = Bucket()
    app1, app2 = Replica(bucket, namespace="app1"), Replica(bucket, namespace="app2")

    await app1.fire(eager=True)
    await app2.fire(eager=True)

    assert (app1.ran, app2.ran) == (1, 1)
    assert {"cron.app1.billing.tick.eager", "cron.app2.billing.tick.eager"} <= set(bucket.entries)


@pytest.mark.asyncio
async def test_replicas_of_one_app_still_share_one_lock():
    """The control: the namespace separates apps, not the replicas of one app."""
    bucket = Bucket()
    first, second = Replica(bucket, namespace="app1"), Replica(bucket, namespace="app1")

    await first.fire()
    await second.fire()

    assert (first.ran, second.ran) == (1, 0)


@pytest.mark.asyncio
async def test_a_service_without_a_namespace_keeps_the_keys_it_had():
    bucket = Bucket()
    await Replica(bucket, namespace=None).fire()

    assert f"cron.billing.tick.{EPOCH}" in bucket.entries
    assert not any(key.startswith("cron.None") for key in bucket.entries)


@pytest.mark.asyncio
async def test_a_service_config_without_a_namespace_attribute_is_a_service_without_one():
    bucket = Bucket()
    replica = Replica(bucket, namespace=None)
    replica.timer.service_instance = SimpleNamespace(config=SimpleNamespace(name="billing"))

    await replica.fire()

    assert f"cron.billing.tick.{EPOCH}" in bucket.entries


@pytest.mark.asyncio
async def test_the_key_of_each_shape_is_pinned_and_the_two_shapes_that_collide_are_named():
    """Both key shapes, spelled out, and the one pair of services that share a key.

    The keys are not escaped. A service named `a.b` with no namespace and a service named `b` in
    the namespace `a` build the same key, so only one of them runs a firing. Escaping the dots
    would give every un-namespaced app new keys and run each firing twice on upgrade, so the limit
    is stated (the fragment, the API reference and the decision record) and pinned here: if the
    key changes, this changes with the documents.
    """
    namespaced, dotted = Bucket(), Bucket()
    await Replica(namespaced, namespace="a", service="b").fire()
    await Replica(dotted, namespace=None, service="a.b").fire()

    assert f"cron.a.b.tick.{EPOCH}" in namespaced.entries
    assert f"cron.a.b.tick.{EPOCH}" in dotted.entries

    shared = Bucket()
    first = Replica(shared, namespace="a", service="b")
    second = Replica(shared, namespace=None, service="a.b")
    await first.fire()
    await second.fire()
    assert (first.ran, second.ran) == (1, 0)

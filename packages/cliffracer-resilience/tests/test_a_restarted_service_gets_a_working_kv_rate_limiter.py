"""A `KvRateLimiter` shared by the services declared with it follows a live connection.

The limiter opens its bucket on the first service's connection. `ServiceOrchestrator` builds a new
service from the class on every restart and the declaration hands it the SAME limiter, and a
sibling service sharing the declaration keeps running when the first one stops. `init_kv` used to
keep the first handle, whose connection had closed, so every limited dispatch failed closed until
the process restarted. Here the broker is a dictionary that outlives its connections, and a bucket
handle raises what nats-py raises once the connection it was opened on is closed.
"""

import nats.errors
import nats.js.errors
import pytest
from cliffracer_resilience import KvRateLimiter, RateLimiterUnavailableError

pytestmark = pytest.mark.unit


class Entry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class Connection:
    def __init__(self) -> None:
        self.is_closed = False

    def close(self) -> None:
        self.is_closed = True


class Broker:
    """The state that outlives any one connection: bucket name -> key -> entry."""

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, Entry]] = {}
        self.revision = 0


class Bucket:
    def __init__(self, broker: Broker, name: str, connection: Connection) -> None:
        self._entries = broker.buckets[name]
        self._broker = broker
        self._connection = connection

    def _live(self) -> None:
        if self._connection.is_closed:
            raise nats.errors.ConnectionClosedError()

    async def get(self, key: str) -> Entry:
        self._live()
        if key not in self._entries:
            raise nats.js.errors.KeyNotFoundError()
        return self._entries[key]

    async def create(self, key: str, value: bytes) -> int:
        self._live()
        if key in self._entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._put(key, value)

    async def update(self, key: str, value: bytes, last: int) -> int:
        self._live()
        if key not in self._entries or self._entries[key].revision != last:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._put(key, value)

    def _put(self, key: str, value: bytes) -> int:
        self._broker.revision += 1
        self._entries[key] = Entry(value, self._broker.revision)
        return self._broker.revision


class JetStream:
    """A JetStream context on one connection, as nats-py builds it (`_nc` is its connection)."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._nc = Connection()
        self.opened = 0

    async def key_value(self, name: str) -> Bucket:
        if self._nc.is_closed:
            raise nats.errors.ConnectionClosedError()
        if name not in self._broker.buckets:
            raise nats.js.errors.BucketNotFoundError()
        self.opened += 1
        return Bucket(self._broker, name, self._nc)

    async def create_key_value(self, bucket: str, **_: object) -> Bucket:
        self._broker.buckets.setdefault(bucket, {})
        return await self.key_value(bucket)


def limiter() -> KvRateLimiter:
    return KvRateLimiter(bucket_name="limits")


async def test_a_limiter_opened_again_on_a_new_connection_counts_in_the_same_bucket():
    broker, shared = Broker(), limiter()
    first = JetStream(broker)
    await shared.init_kv(js=first)
    assert await shared.acquire("caller", 1, 60.0) is True

    first._nc.close()
    second = JetStream(broker)
    await shared.init_kv(js=second)

    assert await shared.acquire("caller", 1, 60.0) is False, "the budget is the broker's, not ours"
    assert second.opened == 1
    assert shared.health_details()["status"] == "distributed"


async def test_a_sibling_service_carries_on_when_the_one_that_opened_the_bucket_stops():
    broker, shared = Broker(), limiter()
    a, b = JetStream(broker), JetStream(broker)
    await shared.init_kv(js=a)
    await shared.init_kv(js=b)
    assert await shared.acquire("caller", 5, 60.0) is True

    a._nc.close()

    assert await shared.acquire("caller", 5, 60.0) is True
    assert b.opened == 1, "the bucket was reopened on the connection that is still open"


async def test_services_that_are_both_running_do_not_move_the_bucket_between_them():
    broker, shared = Broker(), limiter()
    a, b = JetStream(broker), JetStream(broker)
    await shared.init_kv(js=a)
    await shared.init_kv(js=b)
    await shared.acquire("caller", 5, 60.0)

    assert (a.opened, b.opened) == (1, 0)


async def test_a_limiter_with_no_open_connection_still_fails_closed():
    broker, shared = Broker(), limiter()
    only = JetStream(broker)
    await shared.init_kv(js=only)
    only._nc.close()

    with pytest.raises(RateLimiterUnavailableError):
        await shared.acquire("caller", 5, 60.0)


async def test_a_new_connection_that_is_already_closed_still_fails_closed_and_says_why():
    broker, shared = Broker(), limiter()
    first = JetStream(broker)
    await shared.init_kv(js=first)
    first._nc.close()
    second = JetStream(broker)
    second._nc.close()
    await shared.init_kv(js=second)

    with pytest.raises(RateLimiterUnavailableError):
        await shared.acquire("caller", 5, 60.0)
    assert shared.health_details()["last_error_type"] == "ConnectionClosedError"


async def test_a_broker_that_cannot_open_the_bucket_on_the_new_connection_fails_closed():
    class Refusing(JetStream):
        async def key_value(self, name: str) -> Bucket:
            raise nats.errors.TimeoutError()

    broker, shared = Broker(), limiter()
    first = JetStream(broker)
    await shared.init_kv(js=first)
    first._nc.close()

    with pytest.raises(RateLimiterUnavailableError):
        await shared.init_kv(js=Refusing(broker))


async def test_a_bucket_handed_in_is_never_replaced():
    broker = Broker()
    connection = JetStream(broker)
    handed = await connection.create_key_value("handed_in")
    shared = KvRateLimiter(kv=handed, bucket_name="limits")
    replacement = JetStream(broker)

    await shared.init_kv(js=replacement)

    assert replacement.opened == 0
    assert await shared.acquire("caller", 5, 60.0) is True

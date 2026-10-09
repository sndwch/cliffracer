"""What a distributed cron job checks at start, how it reads and writes its lease and interval record.

A bucket that is not a string is refused when the timer is built. The extensions handed to the
dependency check are the ones searched, and a stand-in named `kv` is found. A start or a firing with
no KV extension is a configuration error naming the service. A bucket TTL that cannot be read at
start is reported, and one of a second refuses a longer lease. A job without `no_overlap` ignores a
running lease; an empty lease record is no lease; a lease started exactly one lease ahead is
honoured. A failed interval-record create raises its own error and the handler does not run. A
write given up on has unwound before the firing returns. A clone keeps the headers. A failure is
reported at its level: a failed firing as an error, a lease that cannot be recorded and a lease
release refused for a reason other than a later run as warnings.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer
from cliffracer_cron import distributed as distributed_module
from loguru import logger

from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

WHEN = datetime(2026, 9, 14, tzinfo=UTC)
ACTIVE_KEY = "cron.jobs.run.active"


class Entry:
    def __init__(self, value: bytes, revision: int = 1) -> None:
        self.value = value
        self.revision = revision


class Bucket:
    """A minimal in-memory KV bucket; each operation may be overridden per test."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.rev = 0

    async def get(self, key: str) -> Any:
        if key not in self.store:
            raise nats.js.errors.KeyNotFoundError()
        return Entry(self.store[key])

    async def create(self, key: str, value: bytes) -> int:
        if key in self.store:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.rev += 1
        self.store[key] = value
        return self.rev

    async def put(self, key: str, value: bytes) -> int:
        self.rev += 1
        self.store[key] = value
        return self.rev

    async def update(self, key: str, value: bytes, last: int) -> int:
        self.rev += 1
        self.store[key] = value
        return self.rev

    async def delete(self, key: str, last: int | None = None) -> None:
        self.store.pop(key, None)

    async def status(self) -> Any:
        return SimpleNamespace(ttl=3600.0)


class Kv:
    name = "kv"

    def __init__(self, bucket: Any) -> None:
        self.bucket = bucket

    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> Any:
        return self.bucket


class Service:
    def __init__(self) -> None:
        self.config = SimpleNamespace(name="jobs")
        self.instance_id = "this"
        self.runs = 0

    async def run(self) -> None:
        self.runs += 1


def _timer(bucket: Any, **kwargs: Any) -> tuple[DistributedCronTimer, Service]:
    timer = DistributedCronTimer("* * * * *", kv_extension=Kv(bucket), **kwargs)
    timer.method_name = "run"
    service = Service()
    timer.service_instance = service
    return timer, service


class _Logs:
    def __init__(self, level: str) -> None:
        self.records: list[tuple[str, str]] = []
        self._level = level

    def __enter__(self) -> "_Logs":
        self._sink = logger.add(
            lambda m: self.records.append((m.record["level"].name, m.record["message"])),
            level=self._level,
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        logger.remove(self._sink)

    def at(self, level: str) -> list[str]:
        return [msg for lvl, msg in self.records if lvl == level]


# --- options and construction ----------------------------------------------------------------


def test_a_bucket_that_is_not_a_string_is_refused_without_cliffracer_kv(monkeypatch):
    monkeypatch.setitem(sys.modules, "cliffracer_kv", None)
    with pytest.raises(
        ConfigurationError, match=r"@cron bucket must be a bucket name, got 5 \(int\)"
    ):
        DistributedCronTimer("* * * * *", bucket=5)


def test_a_clone_keeps_the_headers():
    timer = DistributedCronTimer("* * * * *", headers={"x-team": "ops"})
    assert timer.clone().headers == {"x-team": "ops"}


# --- finding the KvExtension ---------------------------------------------------------------


def test_the_extensions_handed_to_the_dependency_check_are_the_ones_searched():
    from cliffracer_kv import KvExtension

    timer = DistributedCronTimer("* * * * *")
    # The service has no container: only the list handed in holds the store.
    timer.check_declared_dependencies(SimpleNamespace(), "run", [KvExtension(buckets=["x"])])


def test_a_stand_in_named_kv_is_found_without_cliffracer_kv(monkeypatch):
    monkeypatch.setitem(sys.modules, "cliffracer_kv", None)
    timer = DistributedCronTimer("* * * * *")
    timer.check_declared_dependencies(SimpleNamespace(), "run", [Kv(Bucket())])


async def test_a_firing_with_no_kv_extension_is_a_configuration_error():
    timer = DistributedCronTimer("* * * * *")
    timer.method_name = "run"
    timer.service_instance = Service()
    with pytest.raises(ConfigurationError, match="no KvExtension is registered"):
        await timer._execute_distributed(WHEN)


async def test_a_start_with_no_kv_extension_names_the_service():
    timer = DistributedCronTimer("0 0 1 1 *")
    timer.method_name = "job"
    with pytest.raises(ConfigurationError, match="on service 'orders' declares"):
        await timer.start(SimpleNamespace(config=SimpleNamespace(name="orders")))

    class Nameless:
        pass

    timer = DistributedCronTimer("0 0 1 1 *")
    timer.method_name = "job"
    with pytest.raises(ConfigurationError, match="on service 'Nameless' declares"):
        await timer.start(Nameless())


# --- the TTL check at start ----------------------------------------------------------------


class UnreadableKv(Kv):
    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> Any:
        raise RuntimeError("broker gone")


@pytest.mark.parametrize("no_overlap", [True, False])
async def test_a_bucket_ttl_that_cannot_be_read_at_start_is_reported(no_overlap):
    timer = DistributedCronTimer(
        "0 0 1 1 *", kv_extension=UnreadableKv(None), no_overlap=no_overlap, lease_ttl=42
    )
    timer.method_name = "job"
    with _Logs("WARNING") as logs:
        await timer.start(SimpleNamespace(config=SimpleNamespace(name="svc")))
        await timer.stop()
    reported = [
        m for m in logs.at("WARNING") if "could not read the TTL of bucket 'cron_locks'" in m
    ]
    assert len(reported) == 1
    assert reported[0].endswith(": broker gone")
    # Only a job that holds a lease has a lease_ttl the check would have tested.
    assert ("its lease_ttl of 42s is unchecked" in reported[0]) is no_overlap


async def test_a_bucket_whose_ttl_is_one_second_refuses_a_longer_lease():
    bucket = Bucket()

    async def status() -> Any:
        return SimpleNamespace(ttl=1)

    bucket.status = status  # type: ignore[method-assign]
    timer = DistributedCronTimer("0 0 1 1 *", kv_extension=Kv(bucket), lease_ttl=300.0)
    timer.method_name = "job"
    with pytest.raises(ConfigurationError, match="expires its keys after 1s"):
        await timer.start(SimpleNamespace(config=SimpleNamespace(name="svc")))


# --- a firing ------------------------------------------------------------------------------


async def test_a_job_without_no_overlap_ignores_a_running_lease():
    bucket = Bucket()
    bucket.store[ACTIVE_KEY] = json.dumps(
        {"status": "running", "started_at": __import__("time").time(), "replica": "other"}
    ).encode()
    timer, service = _timer(bucket, no_overlap=False)
    await timer._execute_distributed(WHEN)
    assert service.runs == 1


async def test_an_empty_lease_record_is_read_as_no_lease_without_a_warning():
    bucket = Bucket()
    bucket.store[ACTIVE_KEY] = b""
    timer, service = _timer(bucket)
    with _Logs("WARNING") as logs:
        await timer._execute_distributed(WHEN)
    assert service.runs == 1
    assert logs.at("WARNING") == []


async def test_a_lease_started_exactly_one_lease_ahead_is_honoured(monkeypatch):
    monkeypatch.setattr(distributed_module, "time", SimpleNamespace(time=lambda: 1000.0))
    bucket = Bucket()
    bucket.store[ACTIVE_KEY] = json.dumps(
        {"status": "running", "started_at": 1300.0, "replica": "other"}
    ).encode()
    timer, service = _timer(bucket, lease_ttl=300.0)
    await timer._execute_distributed(WHEN)
    assert service.runs == 0


async def test_a_failed_interval_record_create_raises_its_own_error():
    bucket = Bucket()

    async def create(key: str, value: bytes) -> int:
        raise RuntimeError("stream unavailable")

    bucket.create = create  # type: ignore[method-assign]
    timer, service = _timer(bucket)
    try:
        await timer._execute_distributed(WHEN)
    except BaseException as exc:  # noqa: BLE001 - the type is what is asserted
        caught: BaseException | None = exc
    else:
        caught = None
    assert type(caught) is RuntimeError
    assert service.runs == 0


async def test_a_write_given_up_on_has_unwound_before_the_firing_returns():
    bucket = Bucket()
    unwound: list[bool] = []

    async def update(key: str, value: bytes, last: int) -> int:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwound.append(True)
            raise
        return 0

    bucket.update = update  # type: ignore[method-assign]
    timer, service = _timer(bucket, no_overlap=False)
    timer.finish_timeout = 0.05
    await asyncio.wait_for(timer._execute_distributed(WHEN), timeout=5)
    seen = list(unwound)
    await asyncio.sleep(0.01)  # let a write left behind finish before the test ends
    assert service.runs == 1
    assert seen == [True]


# --- the loop ------------------------------------------------------------------------------


async def test_a_failed_firing_is_logged_as_an_error():
    timer = DistributedCronTimer("* * * * *", kv_extension=Kv(Bucket()), error_backoff=0)
    timer.method_name = "run"
    timer.service_instance = Service()
    timer.is_running = True
    calls = 0

    async def wait() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return WHEN, WHEN
        timer.is_running = False
        return None

    async def fire(target: datetime, eager: bool = False) -> None:
        raise RuntimeError("firing broke")

    timer._wait_for_the_next_occurrence = wait  # type: ignore[method-assign]
    timer._execute_distributed = fire  # type: ignore[method-assign]
    with _Logs("ERROR") as logs:
        await asyncio.wait_for(timer._timer_loop(), timeout=5)
    assert timer.error_count == 1
    assert [m for m in logs.at("ERROR") if "run" in m and m.endswith("firing broke")]


async def test_a_lease_that_cannot_be_recorded_is_reported():
    bucket = Bucket()

    async def put(key: str, value: bytes) -> int:
        raise RuntimeError("put refused")

    bucket.put = put  # type: ignore[method-assign]
    timer, service = _timer(bucket)
    with _Logs("WARNING") as logs:
        await timer._execute_distributed(WHEN)
    assert service.runs == 1
    assert any("run" in m and m.endswith("put refused") for m in logs.at("WARNING"))


async def test_a_lease_release_refused_for_another_reason_is_a_warning():
    bucket = Bucket()

    async def delete(key: str, last: int | None = None) -> None:
        raise nats.js.errors.BadRequestError(code=400, err_code=10003, description="bad request")

    bucket.delete = delete  # type: ignore[method-assign]
    timer, service = _timer(bucket)
    with _Logs("INFO") as logs:
        await timer._execute_distributed(WHEN)
    assert service.runs == 1
    assert [m for m in logs.at("WARNING") if ACTIVE_KEY in m and "bad request" in m]
    assert not [m for m in logs.at("INFO") if "belongs to a later run" in m]

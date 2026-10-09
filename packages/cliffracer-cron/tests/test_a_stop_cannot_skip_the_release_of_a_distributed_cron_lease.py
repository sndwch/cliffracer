"""A stop cannot leave a distributed cron run's lease and `running` record behind.

`Timer.stop(grace)` waits for a run only while the handler is executing. `_execute_distributed`
still has two steps after the handler returns: it updates the interval record with the outcome and
it releases the `.active` lease. A stop that landed in that window cancelled the task at the
update, step 6 never ran, and the lease and a `running` record stayed for `lease_ttl`: every
replica skipped every firing as "prior run still active" until it expired.

The two steps now run to their end whatever cancels the task meanwhile, and the cancellation is
raised after they have. The same holds for the window between creating the interval record and
starting the handler.
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

TARGET = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
INTERVAL = f"cron.svc.job.{int(TARGET.timestamp())}"
ACTIVE = "cron.svc.job.active"


class SlowBucket:
    """The compare-and-set surface a firing uses, in memory.

    A write takes effect at once and its reply is what is slow, as on a broker behind a slow link:
    a task cancelled while it waits for the reply has still made the write and does not know it.
    `before_send` is the other window, the time before a request leaves: a task cancelled in it has
    made no write.
    """

    def __init__(
        self, delays: dict[str, float] | None = None, before_send: dict[str, float] | None = None
    ) -> None:
        self.before_send = before_send or {}
        self.store: dict[str, bytes] = {}
        self._revisions: dict[str, int] = {}
        self._clock = 0
        self.delays = delays or {}
        self.began: list[str] = []
        self.ops: list[str] = []
        self.abandoned: list[str] = []

    async def _send(self, op: str) -> None:
        self.began.append(f"{op} (sending)")
        await asyncio.sleep(self.before_send.get(op, 0.0))

    async def _reply(self, op: str) -> None:
        self.began.append(op)
        try:
            await asyncio.sleep(self.delays.get(op, 0.0))
        except asyncio.CancelledError:
            self.abandoned.append(op)
            raise

    def _write(self, key: str, value: bytes) -> int:
        self._clock += 1
        self.store[key] = value
        self._revisions[key] = self._clock
        return self._clock

    async def create(self, key: str, value: bytes) -> int:
        await self._send("create")
        if key in self.store:
            await self._reply("create")
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.ops.append(f"create {key}")
        revision = self._write(key, value)
        await self._reply("create")
        return revision

    async def update(self, key: str, value: bytes, last: int) -> int:
        await self._send("update")
        if self._revisions.get(key) != last:
            await self._reply("update")
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.ops.append(f"update {key}")
        revision = self._write(key, value)
        await self._reply("update")
        return revision

    async def put(self, key: str, value: bytes) -> int:
        await self._send("put")
        self.ops.append(f"put {key}")
        revision = self._write(key, value)
        await self._reply("put")
        return revision

    async def get(self, key: str) -> Any:
        if key not in self.store:
            raise nats.js.errors.KeyNotFoundError()
        return SimpleNamespace(key=key, value=self.store[key], revision=self._revisions[key])

    async def delete(self, key: str, last: int | None = None) -> None:
        await self._send("delete")
        if last and self._revisions.get(key) != last:
            await self._reply("delete")
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.ops.append(f"delete {key}")
        self.store.pop(key, None)
        self._revisions.pop(key, None)
        await self._reply("delete")


class FakeKvExtension:
    name = "kv"

    def __init__(self, bucket: SlowBucket) -> None:
        self._bucket = bucket

    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> SlowBucket:
        return self._bucket


class Service:
    def __init__(self) -> None:
        self.config = SimpleNamespace(name="svc")
        self.instance_id = "replica_a"
        self.ran = asyncio.Event()

    async def job(self) -> None:
        self.ran.set()


def _timer(bucket: SlowBucket, service: Service) -> DistributedCronTimer:
    timer = DistributedCronTimer(
        "0 9 * * *", distributed=True, kv_extension=FakeKvExtension(bucket)
    )
    timer.method_name = "job"
    timer.service_instance = service
    return timer


def _record(bucket: SlowBucket) -> dict:
    return json.loads(bucket.store[INTERVAL].decode("utf-8"))


async def _until(predicate, *, within: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "the awaited step never began"
        await asyncio.sleep(0.005)


async def _firing(timer: DistributedCronTimer) -> asyncio.Task[None]:
    return asyncio.ensure_future(timer._execute_distributed(TARGET))


# --- a stop while the outcome is being recorded (the issue's window) -----------------------------------


@pytest.mark.parametrize("grace", [5.0, 0.0, None], ids=["grace", "no-grace", "unbounded-grace"])
async def test_a_stop_during_the_record_update_still_completes_the_record_and_releases_the_lease(
    grace,
):
    bucket = SlowBucket({"update": 0.3})
    service = Service()
    timer = _timer(bucket, service)
    timer.is_running = True
    timer.task = await _firing(timer)
    await _until(lambda: "update" in bucket.began)
    assert not timer._executing, "the handler has returned: this is the window"

    await timer.stop(grace=grace)

    assert ACTIVE not in bucket.store, "the lease was left for lease_ttl"
    assert _record(bucket)["status"] == "completed"
    assert timer.task.done()


async def test_a_stop_during_the_lease_release_still_releases_it():
    bucket = SlowBucket({"delete": 0.3})
    service = Service()
    timer = _timer(bucket, service)
    timer.is_running = True
    timer.task = await _firing(timer)
    await _until(lambda: "delete" in bucket.began)

    await timer.stop(grace=0.0)

    assert ACTIVE not in bucket.store
    assert _record(bucket)["status"] == "completed"


async def test_a_stop_before_the_lease_release_is_sent_still_releases_it():
    bucket = SlowBucket(before_send={"delete": 0.2})
    service = Service()
    timer = _timer(bucket, service)
    task = await _firing(timer)
    await _until(lambda: "delete (sending)" in bucket.began)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ACTIVE not in bucket.store, "the cancelled task never sent the release"
    assert _record(bucket)["status"] == "completed"


async def test_a_stop_before_the_record_update_is_sent_still_completes_it():
    bucket = SlowBucket(before_send={"update": 0.2})
    timer = _timer(bucket, Service())
    task = await _firing(timer)
    await _until(lambda: "update (sending)" in bucket.began)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _record(bucket)["status"] == "completed"
    assert ACTIVE not in bucket.store


async def test_two_cancellations_during_the_cleanup_do_not_skip_it():
    bucket = SlowBucket({"update": 0.2, "delete": 0.2})
    service = Service()
    timer = _timer(bucket, service)
    task = await _firing(timer)
    await _until(lambda: "update" in bucket.began)

    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ACTIVE not in bucket.store
    assert _record(bucket)["status"] == "completed"
    assert bucket.ops[-2:] == [f"update {INTERVAL}", f"delete {ACTIVE}"]


async def test_the_cancellation_is_still_raised_once_the_cleanup_is_done():
    bucket = SlowBucket({"update": 0.1})
    timer = _timer(bucket, Service())
    task = await _firing(timer)
    await _until(lambda: "update" in bucket.began)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert ACTIVE not in bucket.store


# --- a stop before the handler starts ---------------------------------------------------------------------


async def test_a_stop_during_the_lease_write_records_the_run_as_cancelled_and_releases_the_lease():
    bucket = SlowBucket({"put": 0.2})
    service = Service()
    timer = _timer(bucket, service)
    task = await _firing(timer)
    await _until(lambda: "put" in bucket.began)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not service.ran.is_set(), "the handler must not start after the cancellation"
    assert ACTIVE not in bucket.store, "the lease the write made was not released"
    assert _record(bucket)["status"] == "cancelled"


async def test_a_stop_during_the_interval_create_does_not_leave_a_running_record():
    bucket = SlowBucket({"create": 0.2})
    service = Service()
    timer = _timer(bucket, service)
    task = await _firing(timer)
    await _until(lambda: "create" in bucket.began)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not service.ran.is_set()
    assert "put" not in bucket.began, "no lease is written for a run that was cancelled"
    assert ACTIVE not in bucket.store
    assert _record(bucket)["status"] == "cancelled"


async def test_a_stop_that_lost_the_create_race_leaves_the_winners_record_alone():
    bucket = SlowBucket({"create": 0.2})
    winner = json.dumps({"status": "running", "replica": "replica_b"}).encode("utf-8")
    bucket.store[INTERVAL] = winner
    bucket._revisions[INTERVAL] = 99
    timer = _timer(bucket, Service())
    task = await _firing(timer)
    await _until(lambda: "create" in bucket.began)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bucket.store[INTERVAL] == winner
    assert ACTIVE not in bucket.store


# --- what does not change ----------------------------------------------------------------------------------


async def test_CONTROL_a_firing_nobody_stops_completes_the_record_and_releases_the_lease():
    bucket = SlowBucket({"update": 0.05})
    service = Service()
    timer = _timer(bucket, service)

    await timer._execute_distributed(TARGET)

    assert service.ran.is_set()
    assert ACTIVE not in bucket.store
    assert _record(bucket)["status"] == "completed"
    assert bucket.ops == [
        f"create {INTERVAL}",
        f"put {ACTIVE}",
        f"update {INTERVAL}",
        f"delete {ACTIVE}",
    ]


async def test_CONTROL_a_cancel_during_the_handler_is_still_recorded_as_cancelled():
    bucket = SlowBucket()
    service = Service()
    gate = asyncio.Event()

    async def job() -> None:
        service.ran.set()
        await gate.wait()

    service.job = job  # type: ignore[method-assign]
    timer = _timer(bucket, service)
    task = await _firing(timer)
    await asyncio.wait_for(service.ran.wait(), 2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ACTIVE not in bucket.store
    assert _record(bucket)["status"] == "cancelled"


# --- a write that never returns cannot hold a stop open -----------------------------------------------------

HANG = 4.0
BOUND = 0.2
WITHIN = 3.0


def _bounded(bucket: SlowBucket) -> tuple[DistributedCronTimer, Service]:
    service = Service()
    timer = _timer(bucket, service)
    timer.finish_timeout = BOUND
    return timer, service


async def _warnings_during(run) -> list[str]:
    from loguru import logger

    said: list[str] = []
    sink = logger.add(lambda m: said.append(m.record["message"]), level="WARNING")
    try:
        await run()
    finally:
        logger.remove(sink)
    return said


async def test_a_record_update_that_never_returns_cannot_hold_a_stop_open_and_the_lease_is_still_released():
    bucket = SlowBucket({"update": HANG})
    timer, _ = _bounded(bucket)
    timer.is_running = True
    timer.task = await _firing(timer)
    await _until(lambda: "update" in bucket.began)

    async def stop() -> None:
        await asyncio.wait_for(timer.stop(grace=0.0), WITHIN)

    said = await _warnings_during(stop)

    assert timer.task.done()
    assert bucket.abandoned == ["update"], "the write that never returned was left running"
    assert ACTIVE not in bucket.store, "the release was not attempted after the update timed out"
    assert any("cron completion record" in line for line in said), said


async def test_a_lease_release_that_never_returns_cannot_hold_a_stop_open():
    bucket = SlowBucket({"delete": HANG})
    timer, _ = _bounded(bucket)
    timer.is_running = True
    timer.task = await _firing(timer)
    await _until(lambda: "delete" in bucket.began)

    async def stop() -> None:
        await asyncio.wait_for(timer.stop(grace=0.0), WITHIN)

    said = await _warnings_during(stop)

    assert timer.task.done()
    assert bucket.abandoned == ["delete"], "the write that never returned was left running"
    assert any("active lease" in line for line in said), said


async def test_an_interval_create_that_never_returns_ends_the_cancelled_task_within_the_bound():
    bucket = SlowBucket({"create": HANG})
    timer, service = _bounded(bucket)
    task = await _firing(timer)
    await _until(lambda: "create" in bucket.began)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WITHIN)

    assert not service.ran.is_set()


async def test_CONTROL_a_slow_write_under_the_bound_is_not_cut_short():
    bucket = SlowBucket({"update": 0.1})
    timer, service = _bounded(bucket)
    timer.finish_timeout = 1.0

    await asyncio.wait_for(timer._execute_distributed(TARGET), WITHIN)

    assert _record(bucket)["status"] == "completed", (
        "a write slower than nothing but under the bound"
    )
    assert ACTIVE not in bucket.store


@pytest.mark.parametrize("seconds", [0, 0.0, -1, -0.5, float("nan"), float("inf"), True, None, "5"])
def test_finish_timeout_refuses_a_value_that_is_not_a_finite_wait(seconds):
    timer = _timer(SlowBucket(), Service())

    with pytest.raises(ValueError, match="finish_timeout"):
        timer.finish_timeout = seconds  # type: ignore[assignment]

    assert timer.finish_timeout == 10.0, "a refused value must not replace the bound"


def test_finish_timeout_is_ten_seconds_and_a_clone_keeps_what_it_was_set_to():
    timer = _timer(SlowBucket(), Service())
    assert timer.finish_timeout == 10.0

    timer.finish_timeout = 2

    assert timer.finish_timeout == 2.0
    assert timer.clone().finish_timeout == 2.0

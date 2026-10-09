"""A cron timer's own lines are bound to the service it belongs to, so its log stream carries them.

A NATS log sink publishes only the records bound to its service. The cron timers wrote through the
bare loguru logger, so a skipped occurrence, a lease that could not be recorded and a failed loop
reached no service's `logs.<service>.<level>` stream. They now write through the timer's own
logger, which is bound once the timer belongs to a service. In each test the line must be in
`orders`' sink and must not be in `billing`'s; a timer with no service has nothing to bind.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_cron import CronTimer, DistributedCronTimer
from cliffracer_logging import LoggingConfig
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.testing import wait_until

pytestmark = pytest.mark.unit


class RecordingNats:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.messages.append(f"{subject} {payload.decode()}")


@pytest.fixture(autouse=True)
def isolated_logger():
    logger.remove()
    yield
    logger.remove()


def _two_services_streaming() -> tuple[RecordingNats, RecordingNats]:
    """A sink for `orders` and one for `billing`, each filtered to its own service."""
    orders, billing = RecordingNats(), RecordingNats()
    for name, nc in (("orders", orders), ("billing", billing)):
        config = ServiceConfig(name=name, subject_prefix=None, health_port=0)
        LoggingConfig.add_nats_sink(name, nc, config=config, log_level="DEBUG")
    return orders, billing


async def _reaches_orders_and_not_billing(
    marker: str, orders: RecordingNats, billing: RecordingNats
) -> None:
    """The line is in `orders`' sink, and, once everything queued has been sent, not in `billing`'s."""
    await wait_until(
        lambda: (logger.complete(), any(marker in m for m in orders.messages))[1],
        within=5.0,
        reason=f"{marker!r} on orders",
    )
    await asyncio.sleep(0.2)
    logger.complete()
    assert not [m for m in billing.messages if marker in m], billing.messages


def _belongs_to(timer, name: str | None) -> None:
    timer.method_name = "tick"
    if name is not None:
        timer.service_instance = SimpleNamespace(config=SimpleNamespace(name=name))


async def test_the_occurrences_a_cron_loop_missed_are_in_its_services_stream():
    orders, billing = _two_services_streaming()
    timer = CronTimer("* * * * *")
    _belongs_to(timer, "orders")

    timer._report_the_occurrences_missed(
        datetime(2026, 9, 10, 9, 0, tzinfo=UTC), datetime(2026, 9, 10, 9, 5, tzinfo=UTC)
    )

    await _reaches_orders_and_not_billing("wall clock stepped", orders, billing)


class _BucketThatRefusesALease:
    """A KV bucket whose lease write fails, which is what the timer warns about."""

    def __init__(self) -> None:
        self.entries: dict[str, bytes] = {}

    async def create(self, key: str, value: bytes) -> int:
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        self.entries[key] = value
        return 1

    async def get(self, key: str):
        raise nats.js.errors.KeyNotFoundError()

    async def put(self, key: str, value: bytes) -> int:
        raise RuntimeError("the bucket is down")

    async def update(self, key: str, value: bytes, last: int) -> int:
        self.entries[key] = value
        return last + 1

    async def delete(self, key: str, last: int | None = None) -> None:
        self.entries.pop(key, None)


async def _distributed_firing(name: str | None) -> None:
    timer = DistributedCronTimer("* * * * *", no_overlap=True, kv_extension=SimpleNamespace())
    _belongs_to(timer, name)

    async def bucket() -> _BucketThatRefusesALease:
        return _BucketThatRefusesALease()

    async def job() -> None:
        return None

    timer._get_raw_bucket = bucket  # type: ignore[method-assign]
    timer._execute_method = job  # type: ignore[method-assign]
    await timer._execute_distributed(datetime(2026, 9, 10, 9, 0, tzinfo=UTC))


async def test_a_lease_the_distributed_timer_could_not_record_is_in_its_services_stream():
    orders, billing = _two_services_streaming()

    await _distributed_firing("orders")

    await _reaches_orders_and_not_billing("Failed to record active lease", orders, billing)


async def test_CONTROL_a_timer_with_no_service_writes_a_line_no_service_streams():
    orders, billing = _two_services_streaming()

    await _distributed_firing(None)
    logger.complete()
    await asyncio.sleep(0.2)
    logger.complete()

    assert not [m for m in orders.messages + billing.messages if "Failed to record" in m]


async def _ttl_unreadable(name: str | None) -> None:
    timer = DistributedCronTimer("* * * * *", no_overlap=True, kv_extension=SimpleNamespace())
    _belongs_to(timer, name)

    async def bucket():
        raise RuntimeError("the bucket cannot be read")

    timer._get_raw_bucket = bucket  # type: ignore[method-assign]
    await timer._open_the_bucket_and_refuse_a_lease_it_cannot_hold()


async def test_the_warning_that_a_buckets_ttl_could_not_be_read_is_in_its_services_stream():
    orders, billing = _two_services_streaming()

    await _ttl_unreadable("orders")

    await _reaches_orders_and_not_billing("could not read the TTL", orders, billing)

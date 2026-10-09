"""A lease the distributed cron cannot read is reported, whatever words the failure uses."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_cron import DistributedCronTimer
from cliffracer_kv import KvExtension
from loguru import logger

pytestmark = pytest.mark.unit

ACTIVE_KEY = "cron.jobs.run.active"


async def _fire_with_a_lease_read_that_raises(error: BaseException):
    """One distributed firing whose read of the active lease raises `error`.

    Returns what was logged at WARNING, the service, and the bucket.
    """
    bucket = AsyncMock()
    bucket.get.side_effect = error
    bucket.create.return_value = 12
    js = AsyncMock()
    js.key_value.return_value = bucket
    service = SimpleNamespace(
        config=SimpleNamespace(name="jobs"), instance_id="this", run=AsyncMock()
    )
    timer = DistributedCronTimer(
        "* * * * *", no_overlap=True, lease_ttl=10, kv_extension=KvExtension(js=js)
    )
    timer.method_name = "run"
    timer.service_instance = service

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        await timer._execute_distributed(datetime(2026, 9, 14, tzinfo=UTC))
    finally:
        logger.remove(sink)
    return warnings, service, bucket


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("index deleted"),
        RuntimeError("consumer not found"),
        ValueError("lease record not found in the encoded payload"),
        ConnectionError("connection lost"),
    ],
    ids=lambda e: str(e),
)
async def test_an_unreadable_lease_is_warned_about_and_the_run_proceeds(error):
    """Words like "not found" and "deleted" are not a classification of absence.

    The typed absence signals are handled before this; anything else that fails
    the read is a lease the cron could not check, and the operator is told.
    """
    warnings, service, bucket = await _fire_with_a_lease_read_that_raises(error)

    assert warnings == [f"Error checking active lease {ACTIVE_KEY}: {error}"]
    assert service.run.await_count == 1
    assert bucket.create.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "absent",
    [
        nats.js.errors.KeyNotFoundError(),
        nats.js.errors.NotFoundError(),
        nats.js.errors.KeyDeletedError(),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_CONTROL_a_lease_that_is_simply_absent_is_not_a_warning(absent):
    """The typed signals are the absence classification; they stay quiet and the run proceeds."""
    warnings, service, bucket = await _fire_with_a_lease_read_that_raises(absent)

    assert warnings == []
    assert service.run.await_count == 1
    assert bucket.create.await_count == 1

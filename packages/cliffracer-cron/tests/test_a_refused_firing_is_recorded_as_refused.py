"""A distributed firing an extension refused is recorded as refused, not as completed or failed.

The interval record says what became of the firing. A refusal stopped counting as an error, so
without its own status it would be recorded as `completed` for a method that never ran.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_cron import DistributedCronTimer
from cliffracer_kv import KvExtension

from cliffracer.core.extension import RejectMessage

pytestmark = pytest.mark.unit


async def _fire(*, refuse: bool = False, fail: bool = False):
    bucket = AsyncMock()
    bucket.get.return_value = None
    bucket.create.return_value = 12
    js = AsyncMock()
    js.key_value.return_value = bucket

    async def run():
        if fail:
            raise RuntimeError("the method broke")

    async def worker(ctx, call):
        if refuse:
            raise RejectMessage("not today")
        return await call()

    service = SimpleNamespace(
        config=SimpleNamespace(name="jobs"), instance_id="this", run=run, _run_worker=worker
    )
    timer = DistributedCronTimer("* * * * *", kv_extension=KvExtension(js=js))
    timer.method_name = "run"
    timer.service_instance = service
    await timer._execute_distributed(datetime(2026, 9, 14, tzinfo=UTC))
    return json.loads(bucket.update.await_args.args[1]), timer


@pytest.mark.asyncio
async def test_a_refused_firing_is_recorded_as_refused_with_its_reason():
    record, timer = await _fire(refuse=True)

    assert record["status"] == "refused"
    assert record["refusal"] == "not today"
    assert "error" not in record
    assert (timer.error_count, timer.refusal_count) == (0, 1)


@pytest.mark.asyncio
async def test_a_firing_that_fails_is_still_recorded_as_failed_with_its_error_type():
    record, timer = await _fire(fail=True)

    assert record["status"] == "failed"
    assert record["error"] == "RuntimeError"
    assert "refusal" not in record
    assert (timer.error_count, timer.refusal_count) == (1, 0)


@pytest.mark.asyncio
async def test_CONTROL_a_firing_that_succeeds_is_still_recorded_as_completed():
    record, _ = await _fire()

    assert record["status"] == "completed"
    assert "refusal" not in record and "error" not in record

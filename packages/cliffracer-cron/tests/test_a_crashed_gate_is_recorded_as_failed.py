"""A distributed firing a gate crashed on is recorded as failed, not as refused.

The interval record derives its status from what the timer reports. A crashed gate used to be
reported as a refusal, so the record said `refused` for a service that was broken; it now follows
the timer and says `failed`, with the error.
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


async def _fire(*, crash: bool = False, refuse: bool = False):
    bucket = AsyncMock()
    bucket.get.return_value = None
    bucket.create.return_value = 12
    js = AsyncMock()
    js.key_value.return_value = bucket
    ran: list[int] = []

    async def run():
        ran.append(1)

    async def worker(ctx, call):
        if crash:
            raise RejectMessage("extension gate failed: internal error", hook_crash=True)
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
    return json.loads(bucket.update.await_args.args[1]), timer, ran


async def test_a_firing_a_gate_crashed_on_is_recorded_as_failed_with_its_error():
    record, timer, ran = await _fire(crash=True)

    assert ran == [], "the method must not run behind a gate that crashed"
    assert record["status"] == "failed", record
    # The record holds the exception's type and not its text unless `expose_internal_errors` lets the
    # text leave the process, so a crashed gate gets the same record a failing method does.
    assert record["error"] == "RejectMessage"
    assert timer.last_error_type == "RejectMessage"
    assert "refusal" not in record
    assert (timer.error_count, timer.refusal_count) == (1, 0)


async def test_CONTROL_a_firing_a_gate_refused_is_still_recorded_as_refused():
    record, timer, ran = await _fire(refuse=True)

    assert ran == []
    assert record["status"] == "refused" and record["refusal"] == "not today"
    assert "error" not in record
    assert (timer.error_count, timer.refusal_count) == (0, 1)

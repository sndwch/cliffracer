"""A running lease whose `started_at` cannot be read is reported, and the interval runs.

Reading a missing `started_at` as 0 made the lease infinitely old and it was run over in silence;
reading `Infinity` or a date far ahead made it infinitely fresh and it skipped every interval. A
lease whose age is unknown gets the policy every other unreadable lease gets.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_cron import DistributedCronTimer, distributed
from cliffracer_kv import KvExtension
from loguru import logger

pytestmark = pytest.mark.unit

NOW = 1000.0
TTL = 10.0
KEY = "cron.jobs.run.active"


async def _fire(record: bytes, monkeypatch):
    monkeypatch.setattr(distributed, "time", SimpleNamespace(time=lambda: NOW))
    bucket = AsyncMock()
    bucket.get.return_value = SimpleNamespace(value=record)
    bucket.create.return_value = 12
    js = AsyncMock()
    js.key_value.return_value = bucket
    service = SimpleNamespace(
        config=SimpleNamespace(name="jobs"), instance_id="this", run=AsyncMock()
    )
    timer = DistributedCronTimer(
        "* * * * *", no_overlap=True, lease_ttl=TTL, kv_extension=KvExtension(js=js)
    )
    timer.method_name = "run"
    timer.service_instance = service
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        await timer._execute_distributed(datetime(2026, 9, 14, tzinfo=UTC))
    finally:
        logger.remove(sink)
    return service.run.await_count, warnings


def _lease(**fields) -> bytes:
    return json.dumps({"status": "running", "replica": "other", **fields}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "reason"),
    [
        pytest.param(_lease(), "it has none", id="missing"),
        pytest.param(_lease(started_at=None), "it has none", id="null"),
        pytest.param(_lease(started_at="soon"), "'soon' is not a number", id="text"),
        pytest.param(_lease(started_at=True), "True is not a number", id="boolean"),
        pytest.param(_lease(started_at={"a": 1}), "{'a': 1} is not a number", id="object"),
        pytest.param(
            b'{"status": "running", "replica": "other", "started_at": NaN}',
            "nan is not finite",
            id="NaN",
        ),
        pytest.param(
            b'{"status": "running", "replica": "other", "started_at": Infinity}',
            "inf is not finite",
            id="Infinity",
        ),
        pytest.param(
            b'{"status": "running", "replica": "other", "started_at": -Infinity}',
            "-inf is not finite",
            id="minus-Infinity",
        ),
        pytest.param(
            _lease(started_at=NOW + 10 * TTL),
            "more than a lease (10s) in the future",
            id="far-in-the-future",
        ),
    ],
)
async def test_a_lease_whose_age_cannot_be_read_is_reported_and_the_interval_runs(
    monkeypatch, record, reason
):
    runs, warnings = await _fire(record, monkeypatch)

    assert runs == 1
    assert len(warnings) == 1, warnings
    assert KEY in warnings[0] and "no readable started_at" in warnings[0], warnings[0]
    assert reason in warnings[0], warnings[0]


@pytest.mark.asyncio
async def test_CONTROL_a_fresh_lease_still_skips_the_interval_and_says_nothing_is_unreadable(
    monkeypatch,
):
    runs, warnings = await _fire(_lease(started_at=NOW - 1), monkeypatch)

    assert runs == 0
    assert all("no readable started_at" not in line for line in warnings), warnings
    assert any("skipped: prior run still active" in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_CONTROL_an_expired_lease_still_runs_and_is_not_reported(monkeypatch):
    runs, warnings = await _fire(_lease(started_at=NOW - 60), monkeypatch)

    assert (runs, warnings) == (1, [])


@pytest.mark.asyncio
async def test_CONTROL_a_numeric_string_is_read_as_the_number_it_spells(monkeypatch):
    runs, warnings = await _fire(_lease(started_at=str(NOW - 1)), monkeypatch)

    assert runs == 0
    assert all("no readable started_at" not in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_CONTROL_a_start_a_little_ahead_of_this_clock_is_skew_not_damage(monkeypatch):
    """Replicas' clocks differ by seconds; a lease up to one `lease_ttl` ahead is still readable."""
    runs, warnings = await _fire(_lease(started_at=NOW + TTL / 2), monkeypatch)

    assert runs == 0
    assert all("no readable started_at" not in line for line in warnings), warnings


@pytest.mark.asyncio
async def test_CONTROL_a_lease_that_is_not_running_is_not_asked_for_its_age(monkeypatch):
    runs, warnings = await _fire(
        json.dumps({"status": "completed", "replica": "other"}).encode(), monkeypatch
    )

    assert (runs, warnings) == (1, [])

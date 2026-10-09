"""Distributed cron evaluates the recorded lease age at its boundary."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_cron import DistributedCronTimer, distributed
from cliffracer_kv import BucketConfig, KvExtension

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_runs", [(9.9, 0), (10.0, 1), (10.1, 1)])
async def test_recorded_lease_age_decides_whether_the_next_interval_runs(
    monkeypatch, age, expected_runs
):
    clock = 100.0
    monkeypatch.setattr(distributed, "time", SimpleNamespace(time=lambda: clock))
    bucket = AsyncMock()
    bucket.get.return_value = SimpleNamespace(
        value=json.dumps(
            {
                "status": "running",
                "started_at": clock - age,
                "replica": "other",
            }
        ).encode()
    )
    bucket.create.return_value = 12
    js = AsyncMock()
    js.key_value.return_value = bucket
    kv = KvExtension(js=js)
    service = SimpleNamespace(
        config=SimpleNamespace(name="jobs"), instance_id="this", run=AsyncMock()
    )
    timer = DistributedCronTimer("* * * * *", no_overlap=True, lease_ttl=10, kv_extension=kv)
    timer.method_name = "run"
    timer.service_instance = service
    await timer._execute_distributed(datetime(2026, 9, 14, tzinfo=UTC))
    assert service.run.await_count == expected_runs
    assert bucket.create.await_count == expected_runs
    if expected_runs:
        payload = json.loads(bucket.update.await_args.args[1])
        assert payload["status"] == "completed"
        assert bucket.update.await_args.kwargs["last"] == 12
    else:
        bucket.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_default_retention_uses_the_public_bucket_api():
    kv = SimpleNamespace(get_bucket=AsyncMock())
    timer = DistributedCronTimer("* * * * *", bucket="leases", lease_ttl=450, kv_extension=kv)
    await timer._get_raw_bucket()
    kv.get_bucket.assert_awaited_once_with(
        "leases",
        default_config=BucketConfig(name="leases", ttl=450),
    )

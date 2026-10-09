"""A distributed cron timer opens its bucket when it starts, which is what the ADR says.

`docs/decisions.md` said the lock bucket was opened when the first firing needed it. The check that
refuses a lease its bucket cannot hold reads the bucket's TTL from `start()`, so the bucket is opened,
and created with `max(lease_ttl, 300)` seconds when it does not exist, before any firing. The row
now says so, and a bucket that cannot be opened is reported at start.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cliffracer_cron import DistributedCronTimer

pytestmark = pytest.mark.unit

DECISIONS = Path(__file__).resolve().parents[3] / "docs" / "decisions.md"


class Bucket:
    async def status(self) -> Any:
        return SimpleNamespace(ttl=3600.0)


class RecordingKv:
    name = "kv"

    def __init__(self) -> None:
        self.opened: list[tuple[str, float | None]] = []

    async def get_bucket(self, bucket: str, *, default_config: Any = None) -> Bucket:
        self.opened.append((bucket, getattr(default_config, "ttl", None)))
        return Bucket()


async def test_start_opens_the_bucket_before_any_firing():
    kv = RecordingKv()
    timer = DistributedCronTimer("0 0 1 1 *", distributed=True, kv_extension=kv, lease_ttl=3600.0)
    timer.method_name = "job"
    service = SimpleNamespace(config=SimpleNamespace(name="svc"))

    await timer.start(service)
    try:
        assert kv.opened == [("cron_locks", 3600.0)]
    finally:
        await timer.stop()


@pytest.mark.parametrize("no_overlap", [True, False])
@pytest.mark.parametrize(("lease_ttl", "bucket_ttl"), [(3600.0, 3600.0), (30.0, 300.0)])
async def test_start_opens_the_bucket_whether_or_not_the_job_holds_a_lease(
    no_overlap, lease_ttl, bucket_ttl
):
    """The bucket a timer creates keeps a key `max(lease_ttl, 300)` seconds, so 30 gives 300."""
    kv = RecordingKv()
    timer = DistributedCronTimer(
        "0 0 1 1 *", distributed=True, kv_extension=kv, lease_ttl=lease_ttl, no_overlap=no_overlap
    )
    timer.method_name = "job"

    await timer.start(SimpleNamespace(config=SimpleNamespace(name="svc")))
    try:
        assert kv.opened == [("cron_locks", bucket_ttl)]
    finally:
        await timer.stop()


def test_the_adr_row_says_the_bucket_opens_when_the_timer_starts():
    row = next(
        line
        for line in DECISIONS.read_text().splitlines()
        if "Distributed cron lock bucket" in line
    )

    assert "opened when the timer starts" in row
    assert "first firing" not in row

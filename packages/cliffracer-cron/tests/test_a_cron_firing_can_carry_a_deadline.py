"""A cron job takes `deadline=` as `@timer` does, and a distributed one keeps it inside its lease.

A distributed job with `no_overlap` refuses a deadline longer than its `lease_ttl`: a firing still
running when its lease ends is overlapped by another replica's, which `no_overlap` prevents.
"""

import asyncio

import pytest
from cliffracer_cron import cron
from cliffracer_cron.cron import CronTimer
from cliffracer_cron.distributed import DistributedCronTimer

from cliffracer import ConfigurationError

pytestmark = pytest.mark.unit

DEADLINE = 0.05
NEVER = 10.0


def test_a_cron_job_takes_and_keeps_a_deadline():
    assert CronTimer("* * * * *", deadline=2.0).clone().deadline == 2.0
    assert DistributedCronTimer("* * * * *", deadline=2.0).clone().deadline == 2.0

    async def job(self) -> None: ...

    marked = cron("* * * * *", deadline=3.0)(job)
    assert [t.deadline for t in marked._cliffracer_timers] == [3.0]


def test_a_bad_cron_deadline_is_refused_when_declared():
    with pytest.raises(ConfigurationError, match="Timer deadline must be a finite number"):
        CronTimer("* * * * *", deadline=0)


def test_a_distributed_no_overlap_job_refuses_a_deadline_past_its_lease():
    with pytest.raises(ConfigurationError, match=r"deadline=301s is longer than lease_ttl=300s"):
        DistributedCronTimer("* * * * *", lease_ttl=300.0, deadline=301.0)

    assert DistributedCronTimer("* * * * *", lease_ttl=300.0, deadline=300.0).deadline == 300.0
    assert (
        DistributedCronTimer(
            "* * * * *", lease_ttl=300.0, deadline=301.0, no_overlap=False
        ).deadline
        == 301.0
    )


class Host:
    async def run(self) -> None:
        await asyncio.sleep(5.0)


async def test_a_cron_firing_is_cut_off_at_its_deadline():
    t = CronTimer("* * * * *", deadline=DEADLINE)
    t.method_name = "run"
    t.service_instance = Host()

    await asyncio.wait_for(t._execute_method(), NEVER)

    assert (t.error_count, t.last_error_type) == (1, "DeadlineExceeded")

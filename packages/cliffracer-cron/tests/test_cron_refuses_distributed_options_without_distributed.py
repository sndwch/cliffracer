"""`@cron(bucket=..., lease_ttl=..., no_overlap=...)` without `distributed=True` is refused.

It built a plain timer and dropped those options, so a job that read like a coordinated one ran
uncoordinated on every replica, with no error and nothing in its stats. It is refused when the
decorator is applied, as `refuse_bare_use` refuses a bare `@cron`.
"""

import pytest
from cliffracer_cron import cron

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "option",
    [{"bucket": "locks"}, {"lease_ttl": 60.0}, {"no_overlap": False}],
    ids=["bucket", "lease_ttl", "no_overlap"],
)
def test_a_distributed_option_without_distributed_is_refused_where_it_is_declared(option):
    with pytest.raises(ValueError, match="distributed=True"):
        cron("*/15 * * * *", **option)


@pytest.mark.parametrize(
    "option",
    [{"bucket": "locks"}, {"lease_ttl": 60.0}, {"no_overlap": False}],
    ids=["bucket", "lease_ttl", "no_overlap"],
)
def test_the_same_option_with_distributed_is_accepted(option):
    cron("*/15 * * * *", distributed=True, **option)


def test_a_plain_cron_and_the_defaults_spelled_out_are_accepted():
    cron("0 9 * * *")
    cron("0 9 * * *", bucket="cron_locks", lease_ttl=300.0, no_overlap=True)

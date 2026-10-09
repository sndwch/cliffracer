"""A distributed cron job refuses an option it cannot run with, where the job is declared.

`Timer` and `CronTimer` refuse a bad option when they are built. `DistributedCronTimer` stored its own
as given: a `lease_ttl` of zero or less made `no_overlap` skip nothing (a lease is honoured while its
age is below it), `nan` and a string failed at every firing, and a bucket name the Key-Value layer
refuses came back from `start()` as a warning and failed at the first firing.
"""

import math
from types import SimpleNamespace

import pytest
from cliffracer_cron import DistributedCronTimer, cron

from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

BAD_LEASES = [0, -5, -0.001, math.nan, math.inf, -math.inf, True, False, "300", None, [300]]
GOOD_LEASES = [0.001, 1, 7.5, 300, 3600.0]
BAD_BUCKETS = ["", "a.b", "has space", "a/b", 5, None, b"cron_locks"]
GOOD_BUCKETS = ["cron_locks", "my-locks", "my_locks_2", "Jobs"]


def _timer(**options) -> DistributedCronTimer:
    return DistributedCronTimer("* * * * *", distributed=True, **options)


@pytest.mark.parametrize("lease_ttl", BAD_LEASES, ids=repr)
def test_a_lease_ttl_that_is_not_a_finite_number_above_zero_is_refused(lease_ttl):
    with pytest.raises(ConfigurationError, match="lease_ttl must be a finite number of seconds"):
        _timer(lease_ttl=lease_ttl)


@pytest.mark.parametrize("lease_ttl", GOOD_LEASES)
def test_CONTROL_a_lease_ttl_that_is_a_finite_number_above_zero_is_accepted(lease_ttl):
    assert _timer(lease_ttl=lease_ttl).lease_ttl == lease_ttl


@pytest.mark.parametrize("no_overlap", ["yes", 1, 0, None, "false"], ids=repr)
def test_a_no_overlap_that_is_not_a_bool_is_refused(no_overlap):
    with pytest.raises(ConfigurationError, match="no_overlap must be True or False"):
        _timer(no_overlap=no_overlap)


@pytest.mark.parametrize("no_overlap", [True, False])
def test_CONTROL_a_bool_no_overlap_is_accepted(no_overlap):
    assert _timer(no_overlap=no_overlap).no_overlap is no_overlap


@pytest.mark.parametrize("bucket", BAD_BUCKETS, ids=repr)
def test_a_bucket_name_the_kv_layer_would_refuse_is_refused(bucket):
    with pytest.raises(ConfigurationError, match="bucket"):
        _timer(bucket=bucket)


@pytest.mark.parametrize("bucket", GOOD_BUCKETS)
def test_CONTROL_a_bucket_name_the_kv_layer_accepts_is_accepted(bucket):
    assert _timer(bucket=bucket).bucket == bucket


def test_the_decorator_refuses_a_bad_option_when_the_class_is_defined():
    with pytest.raises(ConfigurationError, match="lease_ttl"):

        class Bad:
            @cron("* * * * *", distributed=True, lease_ttl=-5)
            async def job(self):
                return None

    with pytest.raises(ConfigurationError, match="bucket"):

        class AlsoBad:
            @cron("* * * * *", distributed=True, bucket="has space")
            async def job(self):
                return None


def test_a_clone_keeps_the_options_and_is_checked_the_same_way():
    timer = _timer(bucket="jobs", lease_ttl=45.0, no_overlap=False)

    copy = timer.clone()

    assert (copy.bucket, copy.lease_ttl, copy.no_overlap) == ("jobs", 45.0, False)


def test_the_errors_name_the_value_and_not_a_credential():
    with pytest.raises(ConfigurationError) as refused:
        _timer(lease_ttl="300")

    assert "'300'" in str(refused.value) and "str" in str(refused.value)
    assert not isinstance(refused.value, SimpleNamespace)

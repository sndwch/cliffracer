"""A cron timer takes the base timer's checks on drift and backoff, and has no interval to check."""

import pytest
from cliffracer_cron import CronTimer

from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("option", ["max_drift", "error_backoff"])
def test_a_negative_drift_or_backoff_is_refused(option):
    with pytest.raises(ConfigurationError, match=f"Timer {option} must be"):
        CronTimer("* * * * *", **{option: -1})


def test_CONTROL_a_cron_timer_builds_without_an_interval():
    """Its interval is a placeholder the base class is handed, not an option a user gives."""
    timer = CronTimer("* * * * *")

    assert timer.interval == 0
    assert timer.clone().interval == 0

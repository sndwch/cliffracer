"""A timer option the loop cannot honour is refused where the timer is declared."""

import math

import pytest

from cliffracer import CliffracerService, timer
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit

BAD_INTERVALS = [0, 0.0, -1, -0.5, "5", "", None, True, False, [1], math.nan, math.inf, -math.inf]
BAD_NON_NEGATIVE = [-1, -0.1, "1", None, True, math.nan, math.inf]


@pytest.mark.parametrize("interval", BAD_INTERVALS, ids=repr)
def test_a_timer_with_an_interval_that_is_not_a_positive_finite_number_is_refused(interval):
    with pytest.raises(ConfigurationError, match="Timer interval must be"):
        Timer(interval=interval)


@pytest.mark.parametrize("interval", BAD_INTERVALS, ids=repr)
def test_the_decorator_refuses_it_where_it_is_applied(interval):
    """At class definition, not later inside the loop, where a string failed at `+=`."""
    with pytest.raises(ConfigurationError, match="Timer interval must be"):

        class Svc(CliffracerService):
            @timer(interval=interval)
            async def tick(self) -> None:
                pass


@pytest.mark.parametrize("option", ["max_drift", "error_backoff"])
@pytest.mark.parametrize("value", BAD_NON_NEGATIVE, ids=repr)
def test_drift_and_backoff_must_be_non_negative_finite_numbers(option, value):
    with pytest.raises(ConfigurationError, match=f"Timer {option} must be"):
        Timer(interval=1, **{option: value})


def test_the_message_names_the_value_and_its_type():
    with pytest.raises(ConfigurationError) as caught:
        Timer(interval="5")

    assert "'5' (str)" in str(caught.value), caught.value


@pytest.mark.parametrize("interval", [0.001, 0.1, 1, 60, 3600.5])
def test_CONTROL_a_positive_finite_interval_is_accepted(interval):
    assert Timer(interval=interval).interval == interval


@pytest.mark.parametrize("option", ["max_drift", "error_backoff"])
@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 30])
def test_CONTROL_zero_and_positive_drift_and_backoff_are_accepted(option, value):
    assert getattr(Timer(interval=1, **{option: value}), option) == value


def test_CONTROL_the_decorator_still_builds_a_timer_from_valid_options():
    class Svc(CliffracerService):
        @timer(interval=2.5, eager=True, max_drift=0, error_backoff=0)
        async def tick(self) -> None:
            pass

    (declared,) = Svc.tick._cliffracer_timers
    assert (declared.interval, declared.eager, declared.max_drift, declared.error_backoff) == (
        2.5,
        True,
        0,
        0,
    )

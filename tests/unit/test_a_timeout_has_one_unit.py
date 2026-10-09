"""`validate_timeout` reads its argument as seconds, whatever the magnitude.

It used to infer the unit from the value:

    timeout_ms = timeout * 1000 if timeout < 1000 else timeout

so any timeout of 1000 or more, expressed in seconds, was reinterpreted as
milliseconds and returned a thousand times smaller -- no error, no warning, no
log line. That also made `NumericBounds.MAX_TIMEOUT_MS`, commented "1 hour",
unreachable from this side: `validate_timeout(3600)` returned 3.6, and the
largest value the function could produce from a seconds argument was just under
1000.

MEASURED BEFORE THE FIX:

    validate_timeout(3600) -> 3.6      validate_timeout(1500) -> 1.5
    validate_timeout(1000) -> 1.0      validate_timeout(999)  -> 999.0

and through the one production caller:

    BatchProcessor(batch_timeout_ms=1000000) -> stored 1000 ms, reported success

THE IN-TREE CALLER PASSES SECONDS -- `batch_processor.py` divides its
milliseconds by 1000 on the way in -- so it did not rely on the milliseconds
branch and committing to seconds breaks nothing. The unit is the parameter's, not the value's.

The bounds are still milliseconds, because `NumericBounds` states them that way
and `MIN_TIMEOUT_MS = 1` has no whole-second equivalent. Units are therefore
mixed on purpose, which is why every message names which one it means.
"""

from __future__ import annotations

import math

import pytest

from cliffracer.core.validation import NumericBounds, ValidationError, validate_timeout

pytestmark = pytest.mark.unit


# --- the value is seconds at every magnitude ---------------------------------


def test_a_thousand_seconds_is_a_thousand_seconds():
    """The boundary the old heuristic turned over. 1000, not 1.0."""
    assert validate_timeout(1000) == 1000.0


@pytest.mark.parametrize("seconds", [1000, 1000.0, 1001, 1500, 2000, 3600])
def test_no_value_in_the_old_milliseconds_band_is_divided(seconds):
    """Every value the heuristic used to reinterpret comes back as itself.

    A single boundary case would pass on an off-by-one in the comparison it
    replaced, so the whole band up to the documented maximum is asserted.
    """
    assert validate_timeout(seconds) == float(seconds)


def test_the_documented_one_hour_maximum_is_reachable():
    """`MAX_TIMEOUT_MS = 3600000  # 1 hour` was a bound no caller could ask for.

    Derived from the constant rather than written as 3600, so raising the
    constant cannot leave this asserting a maximum the code no longer has.
    """
    an_hour_in_seconds = NumericBounds.MAX_TIMEOUT_MS / 1000

    assert validate_timeout(an_hour_in_seconds) == an_hour_in_seconds


@pytest.mark.parametrize("seconds", [0.001, 0.5, 1, 1.5, 30.0, 60, 999, 999.999])
def test_CONTROL_ordinary_timeouts_pass_through_unchanged(seconds):
    """The values callers actually use, below the old boundary.

    These were correct before and must stay correct: a fix that changed them
    would be a different defect wearing this one's clothes.
    """
    assert validate_timeout(seconds) == float(seconds)


# Values that a round trip through milliseconds does NOT preserve. Chosen by
# searching for them: the first version of the test below used 0.1, 0.3, 1/3 and
# 1/7, every one of which survives `x * 1000 / 1000` unchanged, so it passed
# with the round trip put back and asserted nothing at all.
#
#   0.00105 * 1000 / 1000 == 0.0010500000000000002
#   2390.948275556623     -> 2390.9482755566234
#
# Each is also INSIDE the default bounds. The first list I wrote led with
# 0.00071, which is 0.71ms and below `MIN_TIMEOUT_MS`, so it failed on the fixed
# code for a reason that had nothing to do with round trips.
DRIFT_UNDER_A_ROUND_TRIP = [0.00105, 0.0021, 1662.1035689838707, 2390.948275556623]


@pytest.mark.parametrize("seconds", DRIFT_UNDER_A_ROUND_TRIP)
def test_the_returned_value_is_not_a_round_trip_through_milliseconds(seconds):
    """The old code returned `timeout_ms / 1000`, so a caller's value came back
    multiplied and divided. These are values where that is observable."""
    assert validate_timeout(seconds) == seconds


def test_CONTROL_these_values_really_do_drift_through_milliseconds():
    """Otherwise the test above is satisfied by any implementation.

    This is the assertion that makes the parametrization above a discriminator
    rather than a restatement: each value must be changed by the operation the
    fix stopped doing.
    """
    for seconds in DRIFT_UNDER_A_ROUND_TRIP:
        assert seconds * 1000 / 1000 != seconds, (
            f"{seconds!r} survives a round trip, so it cannot show that the "
            "implementation stopped making one"
        )


# --- a refusal names the value and the unit ----------------------------------


def test_a_timeout_past_the_maximum_is_refused_naming_the_value_and_both_units():
    """The refusal has to be readable by someone who passed seconds.

    The bounds are milliseconds and the argument is seconds, so a message
    quoting one number is ambiguous exactly where the old defect was.
    """
    with pytest.raises(ValidationError) as refused:
        validate_timeout(100000)

    message = str(refused.value)
    assert "100000 seconds" in message, message
    assert "100000000ms" in message, message
    assert f"{NumericBounds.MAX_TIMEOUT_MS}ms" in message, message


def test_a_timeout_below_the_minimum_is_refused_naming_the_value_and_both_units():
    """The other end, where the seconds value is a fraction and the bound is 1ms."""
    with pytest.raises(ValidationError) as refused:
        validate_timeout(0.0005)

    message = str(refused.value)
    assert "0.0005 seconds" in message, message
    assert "0.5ms" in message, message


def test_a_negative_timeout_is_refused():
    with pytest.raises(ValidationError, match="-5 seconds"):
        validate_timeout(-5)


def test_CONTROL_the_bounds_are_read_as_milliseconds():
    """So "seconds in, milliseconds for the bounds" is asserted, not just written
    in the docstring.

    Half a second against a 600ms floor must be refused; against a 400ms floor
    it must pass. If the bounds were read as seconds, the first would pass.
    """
    with pytest.raises(ValidationError):
        validate_timeout(0.5, min_ms=600)

    assert validate_timeout(0.5, min_ms=400) == 0.5


# --- the same silent-failure family, found while measuring -------------------


def test_a_nan_timeout_is_refused_rather_than_returned():
    """nan compares False against both bounds, so it passed the range check
    untouched and came back as nan. A nan reaching `asyncio.wait_for` is a hang
    rather than a timeout, which is the same failure mode as the division: the
    caller is told nothing."""
    with pytest.raises(ValidationError, match="finite"):
        validate_timeout(float("nan"))


def test_an_infinite_timeout_is_refused_as_a_number_not_as_a_range():
    """inf was already refused, but by the range check, whose message said
    "got infms" -- a bound problem rather than the value not being a duration."""
    with pytest.raises(ValidationError, match="finite"):
        validate_timeout(float("inf"))

    with pytest.raises(ValidationError, match="finite"):
        validate_timeout(float("-inf"))


def test_a_bool_is_not_a_duration():
    """`isinstance(True, int)` is True, so `validate_timeout(True)` returned 1.0
    second. A flag arriving where a duration belongs is a caller bug, and one
    second is a plausible-looking answer that hides it."""
    with pytest.raises(ValidationError, match="got bool"):
        validate_timeout(True)

    with pytest.raises(ValidationError, match="got bool"):
        validate_timeout(False)


@pytest.mark.parametrize("value", ["30", None, [30], {"seconds": 30}])
def test_CONTROL_a_non_numeric_timeout_is_still_refused_by_type(value):
    """Unchanged behaviour, asserted because the bool guard is new code on the
    same branch: a string timeout must still be refused for being a string."""
    with pytest.raises(ValidationError, match="must be numeric"):
        validate_timeout(value)


def test_CONTROL_math_isfinite_is_what_the_guard_uses():
    """The guard rests on `math.isfinite`, so this states the property it relies
    on rather than leaving the nan test to imply it."""
    assert math.isfinite(3600.0)
    assert not math.isfinite(float("nan"))
    assert not math.isfinite(float("inf"))


# --- through the one production caller ---------------------------------------


def test_the_batch_processor_refuses_a_flush_window_it_cannot_honour():
    """The issue's end-to-end evidence, now loud.

    `BatchProcessor` validates with `max_ms=60000`, so a one-thousand-second
    flush window is out of its own bounds. It used to be reinterpreted to one
    second and stored, with the constructor reporting success.
    """
    from cliffracer_metrics.batch_processor import BatchProcessor

    with pytest.raises(ValidationError) as refused:
        BatchProcessor(batch_timeout_ms=1000000)

    message = str(refused.value)
    assert "1000.0 seconds" in message, message
    assert "60000ms" in message, message


@pytest.mark.parametrize("requested_ms", [1, 50, 1000, 60000])
def test_CONTROL_the_batch_processor_stores_the_window_it_was_given(requested_ms):
    """Including 1000ms, which crosses the old boundary after the division by
    1000 the caller does on the way in -- so it is the value most likely to
    have been disturbed by either the defect or the fix."""
    from cliffracer_metrics.batch_processor import BatchProcessor

    assert BatchProcessor(batch_timeout_ms=requested_ms).batch_timeout_ms == requested_ms

"""`validate_timeout` honours an explicit bound of 0, and `validate_username` says what it accepts.

`min_ms = min_ms or MIN_TIMEOUT_MS` replaced an explicit `min_ms=0`, the natural way to say "no
lower bound", with 1: the parameter is typed `int | None`, which advertises `None` as the
sentinel. A bool is refused by both validators (that half was already fixed and is pinned
here as a control).

`validate_username` accepts any Unicode letter or digit and lowercases the result, with no
cross-script normalisation: a Latin and a Cyrillic "a" are different names. Its docstring says
so, and this pins that it is true.
"""

import pytest

from cliffracer.core.validation import (
    ValidationError,
    validate_batch_size,
    validate_timeout,
    validate_username,
)

pytestmark = pytest.mark.unit


def test_an_explicit_zero_lower_bound_is_honoured():
    assert validate_timeout(0, min_ms=0) == 0.0


def test_an_explicit_zero_upper_bound_is_honoured_not_replaced_by_the_default():
    with pytest.raises(ValidationError):
        validate_timeout(1, max_ms=0)


def test_CONTROL_the_default_bounds_apply_when_none_is_given():
    with pytest.raises(ValidationError):
        validate_timeout(0)
    assert validate_timeout(3600) == 3600.0


@pytest.mark.parametrize("flag", [True, False])
def test_CONTROL_a_bool_is_neither_a_timeout_nor_a_batch_size(flag):
    with pytest.raises(ValidationError):
        validate_timeout(flag)
    with pytest.raises(ValidationError):
        validate_batch_size(flag)


def test_a_lookalike_in_another_script_is_a_different_username():
    latin, cyrillic = "alice", "аlice"

    assert validate_username(latin) == "alice"
    assert validate_username(cyrillic) == "аlice"
    assert validate_username(latin) != validate_username(cyrillic)


def test_the_docstring_says_so():
    doc = validate_username.__doc__ or ""

    assert "any Unicode letters and numbers" in doc
    assert "different usernames" in doc

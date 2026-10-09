"""A `CircuitBreakerConfig` that could never behave as written is refused where it is built.

`failure_threshold=0` tripped on the first failure, as 1 does; `half_open_max_calls=0` refused every
probe so the circuit could never close; a negative `recovery_timeout` was half-open from the start;
a bare exception class survived construction and raised `TypeError` at the first exception, in a
resilience path, far from the mistake.
"""

import pytest
from cliffracer_resilience import CircuitBreakerConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("failure_threshold", 0, "failure_threshold must be at least 1"),
        ("failure_threshold", -3, "failure_threshold must be at least 1"),
        ("recovery_timeout", -1.0, "recovery_timeout must be 0 or more"),
        ("recovery_timeout", float("nan"), "recovery_timeout must be 0 or more"),
        ("half_open_max_calls", 0, "half_open_max_calls must be at least 1"),
    ],
)
def test_a_number_that_cannot_work_is_refused_by_name(field, value, fragment):
    with pytest.raises(ValueError, match=fragment):
        CircuitBreakerConfig(**{field: value})


def test_a_bare_exception_class_is_refused_and_says_to_use_a_tuple():
    with pytest.raises(TypeError, match=r"tuple or list of exception classes.*\(.*ValueError.*,\)"):
        CircuitBreakerConfig(monitored_exceptions=ValueError)  # type: ignore[arg-type]


@pytest.mark.parametrize("entry", ["TimeoutError", int, 5], ids=["a name", "a class", "a value"])
def test_something_that_is_not_an_exception_class_is_refused(entry):
    with pytest.raises(TypeError, match="exception classes"):
        CircuitBreakerConfig(monitored_exceptions=[ValueError, entry])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_threshold": 1},
        {"recovery_timeout": 0.0},
        {"recovery_timeout": float("inf")},
        {"half_open_max_calls": 1},
        {"monitored_exceptions": (ValueError,)},
        {"monitored_exceptions": [ValueError, KeyError]},
        {"monitored_exceptions": ()},
        {},
    ],
    ids=lambda kw: ",".join(f"{k}={v}" for k, v in kw.items()) or "defaults",
)
def test_the_edge_of_what_works_is_accepted(kwargs):
    CircuitBreakerConfig(**kwargs)


def test_a_list_of_classes_is_still_stored_as_a_tuple():
    assert CircuitBreakerConfig(monitored_exceptions=[ValueError]).monitored_exceptions == (
        ValueError,
    )

"""A `ServiceClient` refuses, when it is built, a timeout that is not a positive, finite number.

A zero or negative one failed every call for a reason that was not the real one ("did not answer
within 0s", or "was not sent ... no time left" inside a request with time to spare), and NaN raised
a bare `ValueError` from the budget's conversion to milliseconds. `connect_timeout` is held to the
same rule, with `None` still meaning no bound, as `ServiceConfig.connect_timeout` is.
"""

import math

import pytest

from cliffracer import ServiceClient

pytestmark = pytest.mark.unit

BAD = [
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(True, id="a-bool"),
    pytest.param("5", id="text"),
]


@pytest.mark.parametrize("bad", BAD)
def test_a_timeout_that_is_not_a_positive_finite_number_is_refused_by_name(bad):
    with pytest.raises(ValueError) as refused:
        ServiceClient(service="orders", timeout=bad)

    assert str(refused.value) == (
        f"timeout must be a positive, finite number of seconds, not {bad!r}"
    )


@pytest.mark.parametrize("bad", BAD)
def test_a_connect_timeout_that_is_not_a_positive_finite_number_is_refused_by_name(bad):
    with pytest.raises(ValueError) as refused:
        ServiceClient(service="orders", connect_timeout=bad)

    assert str(refused.value) == (
        f"connect_timeout must be a positive, finite number of seconds, not {bad!r}"
    )


@pytest.mark.parametrize(
    ("timeout", "connect_timeout"),
    [
        pytest.param(0.001, 0.001, id="a-millisecond"),
        pytest.param(7200, None, id="two-hours-and-no-connect-bound"),
    ],
)
def test_a_positive_finite_timeout_and_no_connect_bound_are_taken(timeout, connect_timeout):
    client = ServiceClient(service="orders", timeout=timeout, connect_timeout=connect_timeout)

    assert (client.timeout, client.connect_timeout) == (timeout, connect_timeout)

"""The edges of the correlation-id rules: the length limit, the logged excerpt, the header, the scan.

Each case sits on a boundary the other correlation tests step over: an id of exactly the limit and
of one over it, an excerpt of exactly the cut and of one past it, a send with no id to inject, and a
decorated call whose last argument carries nothing while an earlier one carries the id.
"""

import pytest
from loguru import logger

from cliffracer.core.correlation import (
    CorrelationContext,
    refusal_of,
    with_correlation_id,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(
        lambda message: seen.append(str(message)), level="WARNING", format="{message}"
    )
    yield seen
    logger.remove(sink)


def test_an_id_of_exactly_the_limit_is_accepted_and_one_over_is_refused():
    """The limit is written out: a bound read from the constant moves with the constant."""
    assert refusal_of("a" * 256) is None
    assert refusal_of("a" * 257) is not None


def test_a_refused_id_is_logged_cut_at_64_characters_with_a_mark_only_when_cut(warnings):
    at_the_cut = "a" * 63 + "\x1b"  # 64 characters, refused for its control character
    one_past = "c" * 64 + "\x1b"  # 65 characters
    far_past = "b" * 70 + "\x1b"

    for given in (at_the_cut, one_past, far_past):
        CorrelationContext.extract_from_headers({"X-Correlation-ID": given})

    whole, one, far = warnings
    assert repr(at_the_cut) in whole and "..." not in whole, whole
    assert repr("c" * 64) + "..." in one, one
    assert repr("b" * 64) + "..." in far and "b" * 65 not in far, far


def test_a_send_with_no_id_to_inject_leaves_the_headers_as_they_were():
    CorrelationContext.clear()

    assert CorrelationContext.inject_into_headers({"x": "y"}) == {"x": "y"}
    assert CorrelationContext.inject_into_headers({}, "") == {}


def test_CONTROL_a_given_id_is_injected():
    assert CorrelationContext.inject_into_headers({}, "abc") == {"X-Correlation-ID": "abc"}


class _Request:
    def __init__(self, headers):
        self.headers = headers


@with_correlation_id
def _id_in_force(*args):
    return CorrelationContext.get()


def test_the_scan_goes_on_past_a_last_argument_whose_headers_carry_no_id():
    earlier = _Request({"X-Correlation-ID": "from-the-earlier-request"})
    last = _Request({})

    assert _id_in_force(earlier, last) == "from-the-earlier-request"


def test_the_scan_goes_on_past_a_last_dict_whose_id_is_empty():
    assert _id_in_force({"correlation_id": "from-the-earlier-dict"}, {"correlation_id": ""}) == (
        "from-the-earlier-dict"
    )


def test_the_last_dict_with_an_id_wins_over_an_earlier_one():
    assert _id_in_force({"correlation_id": "first"}, {"correlation_id": "last"}) == "last"

"""What a dead letter reads from its record, and what a filter reads from a duration.

A cause the record names wins over its shape, and a cause that is not one of the three is ignored.
The Content-Type header is found by any case, read without its parameters, and decides how the body
is read. A service or original subject that is not text is None, and deliveries that are not an int
are None. An empty error is an empty line; errors with no mapping entry give no line; one schema
error has no count of more. A dead letter and a filter are frozen. One second is a duration, and a
filter that leaves a dead letter out answers `False`.
"""

from __future__ import annotations

import dataclasses
import datetime
import json

import msgpack
import pytest
from cliffracer_dlq.filters import Filters, parse_duration
from cliffracer_dlq.records import DeadLetter, classify
from nats.js.api import RawStreamMsg

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)


def _letter(record, *, time=WHEN, headers=None, problem=None, seq=7) -> DeadLetter:
    return DeadLetter(
        sequence=seq,
        time=time,
        subject="dlq.orders",
        headers=headers or {},
        record=record,
        problem=problem,
    )


def test_a_named_cause_wins_over_the_shape():
    assert classify({"cause": "invalid", "deliveries": 3, "error": "x"}) == "invalid"


def test_a_cause_that_is_not_one_of_the_three_is_ignored():
    assert classify({"cause": "bogus", "errors": []}) == "invalid"


def test_the_content_type_header_is_found_by_any_case_and_read_without_parameters():
    message = RawStreamMsg(
        subject="dlq.orders",
        seq=1,
        data=msgpack.packb({"errors": [], "service": "orders"}),
        headers={"X-Other": "text/plain", "content-TYPE": " Application/MsgPack ; v=1"},
        time=WHEN,
    )

    assert DeadLetter.from_message(message).record == {"errors": [], "service": "orders"}


def test_the_content_type_header_decides_how_the_body_is_read():
    """A JSON body under a msgpack header is not read as JSON."""
    message = RawStreamMsg(
        subject="dlq.orders",
        seq=1,
        data=json.dumps({"errors": []}).encode(),
        headers={"Content-Type": "application/msgpack"},
        time=WHEN,
    )

    letter = DeadLetter.from_message(message)

    assert letter.record is None and letter.problem.startswith("cannot decode the message")


@pytest.mark.parametrize("value", [5, b"orders"])
def test_a_service_or_original_subject_that_is_not_text_is_none(value):
    letter = _letter({"errors": [], "service": value, "original_subject": value})

    assert (letter.service, letter.original_subject) == (None, None)


@pytest.mark.parametrize("value", [True, "3", 2.0])
def test_deliveries_that_are_not_an_int_are_none(value):
    assert _letter({"deliveries": value}).deliveries is None


def test_an_empty_error_is_an_empty_line():
    assert _letter({"deliveries": 1, "error": ""}).error_line == ""


@pytest.mark.parametrize("errors", [[], ["text"]])
def test_errors_with_no_entry_to_read_give_no_line(errors):
    assert _letter({"errors": errors}).error_line is None


def test_one_schema_error_has_no_count_of_more():
    errors = [{"loc": ["n"], "msg": "bad"}]

    assert _letter({"errors": errors}).error_line == "n: bad"


def test_a_dead_letter_and_a_filter_are_frozen():
    for value in (_letter({"errors": []}), Filters()):
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.subject = "x"  # type: ignore[misc]


def test_one_second_is_a_duration():
    assert parse_duration("1s") == datetime.timedelta(seconds=1)


@pytest.mark.parametrize("field", ["service", "cause", "since"])
def test_a_filter_that_does_not_match_answers_false(field):
    """`matches` is public and declared `-> bool`: a dead letter it leaves out is `False`."""
    value = {"since": WHEN + datetime.timedelta(days=1)}.get(field, "nothing.matches")
    letter = _letter({"deliveries": 5, "service": "orders", "original_subject": "events.order"})

    assert Filters(**{field: value}).matches(letter) is False

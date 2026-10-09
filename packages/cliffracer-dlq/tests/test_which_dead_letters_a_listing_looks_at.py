"""A dead letter is kept when it satisfies every filter that is set, and a message that is not a record satisfies only `since`."""

import datetime

import pytest
from cliffracer_dlq import DeadLetter, Filters, parse_duration

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)


def _letter(
    *, service="orders", original="events.order.created", minutes_ago=5, **record
) -> DeadLetter:
    body = {"service": service, "original_subject": original, "deliveries": 3, "error": "boom"}
    body.update(record)
    return DeadLetter(
        sequence=1,
        time=NOW - datetime.timedelta(minutes=minutes_ago),
        subject="dlq.orders",
        headers={},
        record=body,
    )


def _unreadable(minutes_ago=5) -> DeadLetter:
    return DeadLetter(
        sequence=2,
        time=NOW - datetime.timedelta(minutes=minutes_ago),
        subject="dlq.orders",
        headers={},
        record=None,
        problem="cannot decode the message",
    )


def test_no_filter_keeps_everything_including_what_is_unreadable():
    assert Filters().matches(_letter()) and Filters().matches(_unreadable())


def test_service_keeps_only_that_service():
    keep = Filters(service="orders")

    assert keep.matches(_letter()) and not keep.matches(_letter(service="billing"))


def test_cause_keeps_only_that_cause():
    invalid = _letter(errors=[{"loc": [], "msg": "bad"}])
    del invalid.record["deliveries"]  # type: ignore[union-attr]
    keep = Filters(cause="invalid")

    assert keep.matches(invalid) and not keep.matches(_letter())


@pytest.mark.parametrize(
    ("pattern", "kept"),
    [
        ("events.order.created", True),
        ("events.order.*", True),
        ("events.>", True),
        ("events.order", False),
        ("events.payment.*", False),
    ],
)
def test_original_subject_is_matched_as_a_nats_subject(pattern, kept):
    assert Filters(original_subject=pattern).matches(_letter()) is kept


def test_since_keeps_what_was_stored_at_or_after_the_cutoff():
    cutoff = NOW - datetime.timedelta(minutes=10)
    keep = Filters(since=cutoff)

    assert keep.matches(_letter(minutes_ago=5))
    assert keep.matches(_letter(minutes_ago=10))
    assert not keep.matches(_letter(minutes_ago=11))


def test_every_filter_that_is_set_must_hold():
    keep = Filters(service="orders", original_subject="events.order.*")

    assert keep.matches(_letter())
    assert not keep.matches(_letter(service="billing"))
    assert not keep.matches(_letter(original="events.payment.created"))


@pytest.mark.parametrize(
    "filters",
    [Filters(service="orders"), Filters(cause="decode"), Filters(original_subject="events.>")],
)
def test_a_message_that_is_not_a_record_satisfies_no_record_filter(filters):
    assert not filters.matches(_unreadable())


def test_a_message_that_is_not_a_record_is_still_judged_by_its_time():
    cutoff = NOW - datetime.timedelta(minutes=10)

    assert Filters(since=cutoff).matches(_unreadable(minutes_ago=1))
    assert not Filters(since=cutoff).matches(_unreadable(minutes_ago=30))


def test_a_message_with_no_time_does_not_satisfy_since():
    letter = DeadLetter(
        sequence=1, time=None, subject="dlq.x", headers={}, record={"deliveries": 1}
    )

    assert not Filters(since=NOW).matches(letter)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("15m", 900), ("2h", 7200), ("1d", 86400), ("1d3h5m2s", 86400 + 10800 + 300 + 2)],
)
def test_a_duration_is_read_in_days_hours_minutes_and_seconds(text, seconds):
    assert parse_duration(text) == datetime.timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "banana", "10", "5x", "0s", "-5m", "1.5h", " 5m"])
def test_anything_else_is_not_a_duration(text):
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)

"""What `ls`, `show` and `count` print, exactly.

A time is stamped in UTC as `%Y-%m-%dT%H:%M:%SZ`, and a missing one is `-` in text and null in JSON.
An error is clipped to 90 characters, and a missing or empty one is `-`. `show` prints a problem only
when there is one, headers sorted, the record indented 2 with sorted keys, and the `nats stream get`
line only with both a stream and its sequence. `show --json` has sorted keys and a null record.
Count rows tie on service, then cause.
"""

from __future__ import annotations

import datetime
import json

import pytest
from cliffracer_dlq import render
from cliffracer_dlq.records import DeadLetter

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)
LATER = datetime.timezone(datetime.timedelta(hours=2))


def _letter(record, *, time=WHEN, headers=None, problem=None, seq=7) -> DeadLetter:
    return DeadLetter(
        sequence=seq,
        time=time,
        subject="dlq.orders",
        headers=headers or {},
        record=record,
        problem=problem,
    )


LIMIT = {
    "original_subject": "events.order",
    "error": "TypeError: unlucky\nsecond line",
    "service": "orders",
    "deliveries": 5,
    "stream": "EVENTS",
    "stream_sequence": 42,
}


def test_a_time_is_stamped_in_utc_and_a_missing_one_is_a_dash():
    assert render._stamp(WHEN.astimezone(LATER)) == "2026-10-02T16:00:00Z"
    assert render._stamp(None) == "-"


@pytest.mark.parametrize(
    ("text", "clipped"),
    [
        (None, "-"),
        ("", "-"),
        ("x" * 90, "x" * 90),
        ("x" * 91, "x" * 89 + "…"),
    ],
)
def test_an_error_is_clipped_to_90_characters(text, clipped):
    assert render._clip(text) == clipped


def test_a_list_line_says_deliveries_dash_when_there_are_none():
    line = render.list_line(_letter({"errors": [{"loc": ["n"], "msg": "bad"}], "service": "s"}))

    assert "deliveries=-  n: bad" in line


def test_show_text_is_exact_for_a_record():
    text = render.show_text(_letter(LIMIT, headers={"b": "2", "a": "1"}))

    assert text == "\n".join(
        [
            "sequence: 7",
            "time:     2026-10-02T16:00:00Z",
            "subject:  dlq.orders",
            "cause:    delivery-limit",
            "headers:",
            "  a: 1",
            "  b: 2",
            "record:",
            json.dumps(LIMIT, indent=2, sort_keys=True),
            "original message, while EVENTS still holds it: nats stream get EVENTS 42",
        ]
    )


def test_show_text_of_an_unreadable_message_has_no_record_and_no_headers():
    text = render.show_text(_letter(None, problem="cannot decode"))

    assert text.splitlines()[3:] == ["cause:    unreadable", "problem:  cannot decode"]


@pytest.mark.parametrize(
    "fields", [{"stream": "EVENTS"}, {"stream_sequence": 42}], ids=["stream", "sequence"]
)
def test_show_text_names_no_command_without_both_a_stream_and_its_sequence(fields):
    text = render.show_text(_letter({"errors": [], **fields}))

    assert "nats stream get" not in text


@pytest.mark.parametrize("time", [WHEN, None])
def test_summary_and_show_json_stamp_a_time_and_keep_none_as_none(time):
    letter = _letter(LIMIT, time=time)
    stamp = None if time is None else "2026-10-02T16:00:00Z"

    assert render.summary(letter)["time"] == stamp
    assert json.loads(render.show_json(letter))["time"] == stamp


def test_show_json_has_sorted_keys_and_a_null_record():
    text = render.show_json(_letter(None, problem="p"))

    assert text == (
        '{"headers": {}, "problem": "p", "record": null, "sequence": 7, '
        '"subject": "dlq.orders", "time": "2026-10-02T16:00:00Z"}'
    )


def test_count_rows_tie_on_service_then_cause():
    letters = [
        _letter({"errors": [], "service": "b"}),
        _letter({"errors": [], "service": "a"}),
        _letter({"deliveries": 1, "error": "x", "service": "a"}),
    ]

    rows, total = render.count_table(letters)

    assert rows == [("a", "delivery-limit", 1), ("a", "invalid", 1), ("b", "invalid", 1)]
    assert total == 3

"""A dead letter's cause comes from the record's shape, and a message that is not a record is reported, not raised on."""

import datetime
import json

import msgpack
import pytest
from cliffracer_dlq import DeadLetter, classify
from nats.js.api import RawStreamMsg

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)


def _message(body, *, headers=None, seq=7, subject="dlq.orders", packed=False) -> RawStreamMsg:
    data = (
        body
        if isinstance(body, bytes)
        else (msgpack.packb(body) if packed else json.dumps(body).encode())
    )
    return RawStreamMsg(
        subject=subject,
        seq=seq,
        data=data,
        headers=headers if headers is not None else {"Content-Type": "application/json"},
        time=WHEN,
    )


DECODE = {
    "original_subject": "events.order",
    "payload": {"raw": "{not json"},
    "error": "Decode error: Expecting property name",
    "service": "orders",
    "deliveries": 1,
    "correlation_id": "c-1",
}
LIMIT = {
    "original_subject": "events.order",
    "payload": {"number": 1},
    "error": "TypeError: unlucky",
    "service": "orders",
    "deliveries": 5,
    "delivery_limit": "config jetstream_max_deliver 5",
    "stream": "EVENTS",
    "stream_sequence": 42,
    "consumer": "orders-durable",
}
INVALID = {
    "original_subject": "events.order",
    "payload": {"number": "x"},
    "errors": [
        {"type": "int_parsing", "loc": ["number"], "msg": "Input should be a valid integer"},
        {"type": "missing", "loc": ["item", "sku"], "msg": "Field required"},
    ],
    "service": "orders",
    "schema": "Order",
}


@pytest.mark.parametrize(
    ("record", "cause"),
    [(DECODE, "decode"), (LIMIT, "delivery-limit"), (INVALID, "invalid")],
)
def test_the_shape_of_the_record_is_its_cause(record, cause):
    assert classify(record) == cause
    assert DeadLetter.from_message(_message(record)).cause == cause


def test_a_record_that_is_none_of_the_three_has_no_cause():
    assert classify({"service": "orders", "error": "boom"}) is None


def test_a_decode_prefix_on_an_invalid_record_does_not_make_it_a_decode_error():
    record = {**INVALID, "error": "Decode error: not this"}

    assert classify(record) == "invalid"


def test_the_fields_a_listing_reads_come_from_the_record():
    letter = DeadLetter.from_message(_message(LIMIT))

    assert (letter.sequence, letter.time, letter.subject) == (7, WHEN, "dlq.orders")
    assert (letter.service, letter.original_subject, letter.deliveries) == (
        "orders",
        "events.order",
        5,
    )
    assert letter.field("stream_sequence") == 42
    assert letter.error_line == "TypeError: unlucky"


def test_an_invalid_records_error_line_is_its_first_error_and_how_many_more_there_are():
    letter = DeadLetter.from_message(_message(INVALID))

    assert letter.error_line == "number: Input should be a valid integer (+1 more)"


def test_a_record_written_before_the_delivery_fields_reads_with_none_for_them():
    old = {k: v for k, v in LIMIT.items() if k not in ("stream", "stream_sequence", "consumer")}

    letter = DeadLetter.from_message(_message(old))

    assert letter.cause == "delivery-limit"
    assert letter.field("stream") is None and letter.field("consumer") is None


def test_a_msgpack_record_is_read_by_its_content_type():
    letter = DeadLetter.from_message(
        _message(LIMIT, headers={"content-type": "application/msgpack"}, packed=True)
    )

    assert letter.cause == "delivery-limit" and letter.service == "orders"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"\xff\xfe not json at all", "cannot decode the message"),
        (b"[1, 2, 3]", "the body is a list"),
        (b'{"service": "orders"}', "no `errors` and no `deliveries`"),
        (b"", "no `errors` and no `deliveries`"),
    ],
)
def test_a_message_that_is_not_a_record_is_kept_with_its_reason(body, reason):
    letter = DeadLetter.from_message(_message(body))

    assert letter.record is None and letter.cause is None
    assert letter.problem is not None and reason in letter.problem
    assert (letter.sequence, letter.subject) == (7, "dlq.orders")


def test_a_message_with_no_headers_is_read_as_json():
    message = _message(LIMIT)
    message.headers = None

    assert DeadLetter.from_message(message).cause == "delivery-limit"


def test_errors_take_precedence_over_deliveries_in_a_record_that_has_both():
    assert classify({"errors": [], "deliveries": 3, "error": "boom"}) == "invalid"


def test_a_decoder_that_fails_with_something_other_than_a_value_error_is_still_reported(
    monkeypatch,
):
    def broken(*_args, **_kwargs):
        raise TypeError("the decoder is wrong, not the message")

    monkeypatch.setattr("cliffracer_dlq.records.deserialize_payload", broken)

    letter = DeadLetter.from_message(_message({"deliveries": 1}))

    assert letter.record is None
    assert (
        letter.problem
        == "cannot decode the message: TypeError: the decoder is wrong, not the message"
    )

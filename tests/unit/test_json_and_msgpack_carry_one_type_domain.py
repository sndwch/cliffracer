"""JSON and MessagePack decode a payload to the same values.

`serialization_format` chooses an encoding. It must not change what a handler
receives, and it did: the JSON branch normalised the whole payload with
`pydantic_core.to_jsonable_python`, while the msgpack branch used it only as a
fallback for values msgpack could not encode itself. So under msgpack a payload
with integer map keys was packed and then refused by the RECEIVER
(`strict_map_key`), after it was on the wire, and bytes arrived as `bytes`
where JSON delivered `str`.

Both formats now normalise the whole payload first. Integer keys become strings
and UTF-8 bytes become `str` under either format, and bytes that are not UTF-8
are refused by the SENDER under either format, before anything is published.
"""

import datetime
import decimal
import enum
import uuid

import pytest
from pydantic import BaseModel

from cliffracer.core.validation import deserialize_payload, serialize_payload

pytestmark = pytest.mark.unit


class _Colour(enum.Enum):
    RED = "red"


class _Line(BaseModel):
    sku: str
    price: decimal.Decimal


class _Order(BaseModel):
    order_id: uuid.UUID
    placed_at: datetime.datetime
    lines: list[_Line]


def _round_trip(value, fmt):
    raw, content_type = serialize_payload(value, format=fmt)
    return deserialize_payload(raw, content_type=content_type)


SHAPES = {
    "integer map keys": {1: "a", 2: "b"},
    "integer keys nested": {"counts": {7: 1, 8: 2}},
    "utf-8 bytes": {"blob": b"abc"},
    "bytes that are control characters": {"blob": b"\x00\x01\x02"},
    "tuple": {"pair": (1, 2)},
    "set": {"tags": {"x"}},
    "enum": {"colour": _Colour.RED},
    "model with uuid, datetime and decimal": _Order(
        order_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        placed_at=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        lines=[_Line(sku="a", price=decimal.Decimal("1.50"))],
    ),
    "plain json types": {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None, "l": [1, "two"]},
}


@pytest.mark.parametrize("value", SHAPES.values(), ids=SHAPES.keys())
def test_both_formats_decode_to_the_same_value(value):
    assert _round_trip(value, "msgpack") == _round_trip(value, "json")


def test_integer_keys_arrive_as_strings_under_msgpack():
    assert _round_trip({1: "a"}, "msgpack") == {"1": "a"}


def test_utf8_bytes_arrive_as_str_under_msgpack():
    assert _round_trip({"blob": b"abc"}, "msgpack") == {"blob": "abc"}


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
def test_bytes_that_are_not_utf8_are_refused_by_the_sender(fmt):
    with pytest.raises(UnicodeDecodeError):
        serialize_payload({"blob": b"\xff\xfe"}, format=fmt)

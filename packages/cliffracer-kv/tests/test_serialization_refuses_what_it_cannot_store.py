"""A value is stored as something that can be read back, or refused; never as the text of its repr."""

import dataclasses
import datetime
import decimal
import enum
import json
import uuid

import pytest
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import BaseModel

pytestmark = pytest.mark.unit


@dataclasses.dataclass
class Point:
    x: int
    y: int


class Colour(enum.Enum):
    RED = "red"


class Model(BaseModel):
    n: int


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (Point(1, 2), {"x": 1, "y": 2}),
        (datetime.datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
        (datetime.date(2026, 1, 2), "2026-01-02"),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        (Colour.RED, "red"),
        (
            {"when": datetime.date(2026, 1, 2), "at": Point(3, 4)},
            {"when": "2026-01-02", "at": {"x": 3, "y": 4}},
        ),
        ([Point(5, 6)], [{"x": 5, "y": 6}]),
        ((1, 2), [1, 2]),
        ({7}, [7]),
    ],
    ids=[
        "dataclass",
        "datetime",
        "date",
        "uuid",
        "enum",
        "dict-of-types",
        "list-of-dataclass",
        "tuple",
        "set",
    ],
)
def test_a_value_with_a_json_form_is_stored_as_that_json(value, stored):
    assert json.loads(serialize_value(value)) == stored


@pytest.mark.parametrize(
    "value", [object(), lambda: 1, Exception("boom"), {"x": object()}, [object()]], ids=repr
)
def test_a_value_with_no_json_form_is_refused_naming_its_type(value):
    with pytest.raises(TypeError) as caught:
        serialize_value(value)
    assert "cannot store a " in str(caught.value), caught.value


def test_a_stored_dataclass_reads_back_as_the_structure_it_was():
    assert deserialize_value(serialize_value(Point(1, 2))) == {"x": 1, "y": 2}
    assert Point(**deserialize_value(serialize_value(Point(1, 2)))) == Point(1, 2)


@pytest.mark.parametrize(
    ("value", "raw"),
    [
        ("text", b"text"),
        (b"\x00\x01", b"\x00\x01"),
        (bytearray(b"ab"), b"ab"),
        ({"a": 1}, b'{"a": 1}'),
        ([1, "b", None], b'[1, "b", null]'),
        (3, b"3"),
        (1.5, b"1.5"),
        (True, b"true"),
        (None, b"null"),
        (Model(n=1), b'{"n":1}'),
    ],
    ids=repr,
)
def test_CONTROL_the_documented_types_are_stored_as_before(value, raw):
    assert serialize_value(value) == raw


def test_a_decimal_is_stored_as_a_string_so_no_digit_is_lost():
    digits = "0.1000000000000000055511151231257827"
    assert json.loads(serialize_value(decimal.Decimal(digits))) == digits

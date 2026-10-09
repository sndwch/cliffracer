"""Bytes that are also a dataclass are stored as plain bytes are, or refused when they hold fields.

A `bytes` or `bytearray` subclass that is also a dataclass with fields holds two different values,
its bytes and its fields, and a write cannot know which is meant: it is refused wherever it sits,
naming its type and place, so neither is lost silently (alone it was stored as its bytes, its
fields dropped; in a container as its fields, a secret among them written as the mask). One with no
fields is bytes with a type, stored exactly as plain bytes are in each position. A str that is also
a dataclass is stored as its text, as before.
"""

import dataclasses
import re

import pytest
from cliffracer_kv.serialization import serialize_value
from pydantic import SecretStr

pytestmark = pytest.mark.unit


@dataclasses.dataclass
class Counted(bytes):
    n: int = 5


@dataclasses.dataclass
class CountedArray(bytearray):
    n: int = 5


@dataclasses.dataclass
class Secretive(bytes):
    x: SecretStr = SecretStr("pw")


@dataclasses.dataclass
class Typed(bytes):
    pass


def _typed(raw: bytes) -> Typed:
    value = bytes.__new__(Typed, raw)
    value.__init__()
    return value


@pytest.mark.parametrize(
    "value", [Counted(), CountedArray(), Secretive()], ids=lambda v: type(v).__name__
)
@pytest.mark.parametrize("where", ["alone", "in-a-list", "in-a-dict"])
def test_bytes_with_fields_are_refused_wherever_they_sit(value, where):
    stored = {"alone": value, "in-a-list": [value], "in-a-dict": {"k": value}}[where]
    place = {"alone": type(value).__name__, "in-a-list": "list[0]", "in-a-dict": "dict['k']"}[where]

    with pytest.raises(
        TypeError,
        match=rf"cannot store a {type(value).__name__} .*\({re.escape(place)}\).*two different values",
    ):
        serialize_value(stored)


@pytest.mark.parametrize(
    ("value", "plain"),
    [
        (_typed(b"ab"), b"ab"),
        ([_typed(b"ab")], [b"ab"]),
        ({"k": _typed(b"ab")}, {"k": b"ab"}),
    ],
    ids=["alone", "in-a-list", "in-a-dict"],
)
def test_bytes_with_a_type_and_no_fields_are_stored_as_plain_bytes_are(value, plain):
    assert serialize_value(value) == serialize_value(plain)


@dataclasses.dataclass
class Labelled(str):
    x: SecretStr = SecretStr("pw")


def test_CONTROL_a_str_that_is_a_dataclass_is_still_stored_as_its_text():
    assert serialize_value(Labelled("label")) == b"label"
    assert serialize_value([Labelled("label")]) == b'["label"]'

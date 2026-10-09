"""The secret walk at its edges, and what a typed read does with bytes it cannot decode.

A str is stored as its text, so a str that is also a dataclass is stored as its text and its fields
are not walked. A dict or a list holding itself is walked once and refused as circular. An extra
named by an object other than a str is spelled as the walk passes it, as a dict key is.

`deserialize_value` decodes undecodable bytes with replacement when asked for a str, and raises when
asked for another type. An `as_type` that is not callable, such as a union, reads as inferred.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from cliffracer_kv.serialization import _refuse_a_secret, deserialize_value, serialize_value
from pydantic import BaseModel, ConfigDict, SecretStr

pytestmark = pytest.mark.unit


@dataclasses.dataclass
class Tag(str):
    note: Any


def test_a_str_that_is_a_dataclass_is_stored_as_its_text_and_its_fields_are_not_walked():
    tag = Tag("label")
    tag.note = SecretStr("pw")

    assert serialize_value(tag) == b"label"


def test_a_dict_that_holds_itself_is_refused_as_circular():
    d: dict[str, Any] = {}
    d["self"] = d

    with pytest.raises(TypeError, match="Circular reference"):
        serialize_value(d)


def test_a_list_that_holds_itself_is_refused_as_circular():
    items: list[Any] = []
    items.append(items)

    with pytest.raises(TypeError, match="Circular reference"):
        serialize_value(items)


class Key:
    spelled = 0

    def __str__(self) -> str:
        Key.spelled += 1
        return "k"


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")


def test_an_extra_named_by_a_non_str_is_spelled_during_the_walk():
    m = Open()
    m.__pydantic_extra__[Key()] = 1  # type: ignore[index]
    Key.spelled = 0

    _refuse_a_secret(m, "Open")

    assert Key.spelled == 1


def test_undecodable_bytes_read_as_str_are_decoded_with_replacement():
    assert deserialize_value(b"\xff", as_type=str) == "�"


def test_undecodable_bytes_read_as_another_type_raise():
    with pytest.raises(UnicodeDecodeError):
        deserialize_value(b"\xff", as_type=dict)


def test_an_as_type_that_is_not_callable_reads_as_inferred():
    assert deserialize_value(b"1", as_type=int | None) == 1  # type: ignore[arg-type]

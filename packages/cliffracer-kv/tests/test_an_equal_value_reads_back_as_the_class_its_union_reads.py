"""A value equal to what it reads back as is stored, even as another class its union allows.

A KV write compares a typed position by value. A `str`-mixin enum or `StrEnum` member at
`Colour | str`, or an `IntEnum` member at `Level | int`, is written as its value, and
`get(as_type=...)` reads that as the plain `str` or `int`, which equals the member, so the model is
stored. Declaring the field as the enum alone, or the union with `union_mode="left_to_right"`, reads
the member back. A plain `Enum` member reads back as another value and is refused.
"""

import enum
from typing import Annotated

import pytest
from cliffracer_kv import ModelDoesNotReadBackError
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import BaseModel, Field

pytestmark = pytest.mark.unit


class MixinColour(str, enum.Enum):
    RED = "red"


class StrColour(enum.StrEnum):
    RED = "red"


class Level(enum.IntEnum):
    HIGH = 1


class Colour(enum.Enum):
    RED = "red"


class MixinOrText(BaseModel):
    value: MixinColour | str


class StrEnumOrText(BaseModel):
    value: StrColour | str


class LevelOrNumber(BaseModel):
    value: Level | int


class MixinAlone(BaseModel):
    value: MixinColour


class MixinFirst(BaseModel):
    value: Annotated[MixinColour | str, Field(union_mode="left_to_right")]


class LevelFirst(BaseModel):
    value: Annotated[Level | int, Field(union_mode="left_to_right")]


class ColourOrText(BaseModel):
    value: Colour | str


def read_back(model: BaseModel):
    return deserialize_value(serialize_value(model), as_type=type(model)).value


@pytest.mark.parametrize(
    ("model", "base"),
    [
        pytest.param(MixinOrText(value=MixinColour.RED), str, id="str-mixin-enum"),
        pytest.param(StrEnumOrText(value=StrColour.RED), str, id="strenum"),
        pytest.param(LevelOrNumber(value=Level.HIGH), int, id="intenum"),
    ],
)
def test_an_enum_member_beside_its_own_base_type_is_stored_and_reads_back_as_that_type(model, base):
    value = read_back(model)

    assert type(value) is base
    assert value == model.value


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(MixinAlone(value=MixinColour.RED), id="the-enum-alone"),
        pytest.param(MixinFirst(value=MixinColour.RED), id="left-to-right"),
        pytest.param(LevelFirst(value=Level.HIGH), id="left-to-right-int"),
    ],
)
def test_the_member_reads_back_where_the_field_reads_the_enum_first(model):
    value = read_back(model)

    assert value is model.value


def test_other_text_still_reads_back_as_text_where_the_enum_is_read_first():
    assert read_back(MixinFirst(value="blue")) == "blue"


def test_CONTROL_a_plain_enum_member_beside_str_reads_back_as_another_value_and_is_refused():
    model = ColourOrText(value=Colour.RED)
    assert ColourOrText.model_validate_json(model.model_dump_json()).value == "red"

    with pytest.raises(ModelDoesNotReadBackError, match="ColourOrText"):
        serialize_value(model)

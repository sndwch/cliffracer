"""A value that equals what the service reads back is sent, even as another class its union allows.

A `str`-mixin enum or `StrEnum` member at `Colour | str`, or an `IntEnum` member at `Level | int`, is
written as its value, and pydantic's smart union reads that value as the plain `str` or `int`. That
equals the member, so the call is sent, and the handler receives the plain value: no form can carry
the member, since its value is exactly a `str` or an `int`. Declaring the field as the enum alone, or
the union with `union_mode="left_to_right"`, reads the member back. A plain `Enum` member reads back
as another value, and is refused before sending.

Both send paths are covered: a generated client (`ServiceClient._encode`, through the declared
annotation) and `wire_models` (`call_rpc`, `call_async`, `RpcProxy`, `publish_event` and
`broadcast_message`). Each form is put through JSON, as the broker carries it, and read as the
service reads a message.
"""

import enum
import json
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import read_python_then_json, wire_models

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


def received(path: str, model: BaseModel) -> Any:
    """What the service's handler holds at `value` after `model` is sent along `path`."""
    cls = type(model)
    form = (
        ServiceClient(service="svc", verify=False)._encode(model, cls)
        if path == "client"
        else wire_models(model)
    )
    carried = json.loads(json.dumps(form))
    return read_python_then_json(carried, cls.model_validate, cls.model_validate_json).value


PATHS = pytest.mark.parametrize("path", ["client", "wire"])


@PATHS
@pytest.mark.parametrize(
    ("model", "base"),
    [
        pytest.param(MixinOrText(value=MixinColour.RED), str, id="str-mixin-enum"),
        pytest.param(StrEnumOrText(value=StrColour.RED), str, id="strenum"),
        pytest.param(LevelOrNumber(value=Level.HIGH), int, id="intenum"),
    ],
)
def test_an_enum_member_beside_its_own_base_type_arrives_as_that_type(path, model, base):
    value = received(path, model)

    assert type(value) is base
    assert value == model.value


@PATHS
@pytest.mark.parametrize(
    "model",
    [
        pytest.param(MixinAlone(value=MixinColour.RED), id="the-enum-alone"),
        pytest.param(MixinFirst(value=MixinColour.RED), id="left-to-right"),
        pytest.param(LevelFirst(value=Level.HIGH), id="left-to-right-int"),
    ],
)
def test_the_member_arrives_where_the_field_reads_the_enum_first(path, model):
    value = received(path, model)

    assert type(value) is type(model.value)
    assert value is model.value


@PATHS
def test_other_text_still_arrives_as_text_where_the_enum_is_read_first(path):
    assert received(path, MixinFirst(value="blue")) == "blue"


@PATHS
def test_CONTROL_a_plain_enum_member_beside_str_reads_back_as_another_value_and_is_refused(path):
    """`Colour.RED != "red"`, so no form reads back as the argument, and the value would be read as
    another value: the call is refused before it is sent."""
    model = ColourOrText(value=Colour.RED)
    assert ColourOrText.model_validate_json(model.model_dump_json()).value == "red"

    with pytest.raises(RpcValidationError, match="value"):
        received(path, model)

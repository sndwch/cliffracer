"""A value holding a dataclass with a NaN is sent, and read back as the value it was.

A NaN is not equal to itself, so judging whether a form reads back as the argument compares field
by field with NaN equal to NaN (`_same`). A standard or Pydantic dataclass inside a model is
compared the same way, by the values of its `compare=True` fields whatever `__eq__` its class
defines, so a form holding one with a NaN reads back as the argument and is sent, on the client and
on the wire, not refused as misread. So is an `eq=False` dataclass, which its own `==` compares by
identity, and a dataclass whose own `__eq__` calls two equal values different.
"""

import dataclasses
import math
from datetime import UTC, datetime

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SerializationInfo, field_serializer
from pydantic.dataclasses import dataclass as pydantic_dataclass

from cliffracer.client import RpcValidationError, ServiceClient
from cliffracer.core.validation import _same, read_python_then_json, wire_models

pytestmark = pytest.mark.unit


@dataclasses.dataclass
class Point:
    g: float = 0.0
    h: int = 0


@pydantic_dataclass(config=ConfigDict(ser_json_inf_nan="constants"))
class PydanticPoint:
    g: float = 0.0
    h: int = 0


class HoldsPoint(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")
    f: float = 0.0
    d: Point = Point()


class HoldsPydanticPoint(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")
    f: float = 0.0
    d: PydanticPoint = PydanticPoint()


class StrictHoldsPoint(BaseModel):
    """Strict, so python mode refuses the ISO form of `when` and the read goes through JSON mode,
    which reads a NaN back as a float of its own."""

    model_config = ConfigDict(strict=True, ser_json_inf_nan="constants")
    f: float = 0.0
    when: datetime = datetime(2026, 1, 2, tzinfo=UTC)
    d: Point = Point()


class Hidden(BaseModel):
    """`b` is read under "A", which is `a`'s serialization alias; by field name the serializer writes
    `b` as `a`, by alias it writes `b` itself."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    a: int = Field(0, serialization_alias="A", validation_alias=AliasChoices("A", "a"))
    b: int = Field(0, validation_alias=AliasChoices("A", "b"))

    @field_serializer("b")
    def _b(self, value: int, info: SerializationInfo) -> int:
        return value if info.by_alias else self.a


class HoldsPointAndHidden(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")
    d: Point = Point()
    inner: Hidden = Hidden()


class NoConnection:
    pass


def _sent(path, value):
    if path == "wire":
        return wire_models(value)
    return ServiceClient(NoConnection(), service="s", verify=False)._encode(value, type(value))


@pytest.mark.parametrize("path", ["client", "wire"])
@pytest.mark.parametrize(
    "value",
    [
        HoldsPoint(f=1.0, d=Point(g=math.nan, h=2)),
        HoldsPydanticPoint(f=1.0, d=PydanticPoint(g=math.nan, h=2)),
    ],
    ids=["dataclass", "pydantic-dataclass"],
)
def test_a_dataclass_holding_nan_is_sent_and_read_back_as_the_value(path, value):
    sent = _sent(path, value)

    back = read_python_then_json(sent, type(value).model_validate, type(value).model_validate_json)
    assert back.f == 1.0 and back.d.h == 2 and math.isnan(back.d.g)


@pytest.mark.parametrize("path", ["client", "wire"])
def test_CONTROL_a_dataclass_holding_nan_at_model_level_only_was_already_sent(path):
    """The NaN comes back through JSON mode as a float of its own, so `_same`'s NaN rule decides."""
    value = StrictHoldsPoint(f=math.nan, d=Point(g=1.5, h=2))

    back = read_python_then_json(
        _sent(path, value), StrictHoldsPoint.model_validate, StrictHoldsPoint.model_validate_json
    )
    assert math.isnan(back.f) and back.d == Point(g=1.5, h=2)


@pytest.mark.parametrize("path", ["client", "wire"])
def test_CONTROL_a_dataclass_holding_nan_beside_a_field_every_form_misreads_is_refused(path):
    """Without this, the cases above could pass because nothing is ever refused."""
    value = HoldsPointAndHidden(d=Point(g=math.nan, h=2), inner=Hidden(a=5, b=7))

    with pytest.raises(RpcValidationError):
        _sent(path, value)


def test_two_distinct_nans_are_the_same_and_a_nan_is_no_other_value():
    nan, other_nan = float("nan"), float("nan")
    assert nan is not other_nan

    assert _same(nan, other_nan)
    assert not _same(nan, 1.0)
    assert not _same(1.0, other_nan)
    assert not _same(nan, None)
    assert not _same(None, other_nan)


def test_two_dataclasses_of_one_class_are_the_same_when_their_compared_fields_are():
    @dataclasses.dataclass
    class Tagged:
        g: float
        note: str = dataclasses.field(default="", compare=False)

    assert _same(Point(g=math.nan, h=1), Point(g=math.nan, h=1))
    assert not _same(Point(g=math.nan, h=1), Point(g=math.nan, h=2))
    assert _same(Tagged(math.nan, "a"), Tagged(math.nan, "b"))


def test_dataclasses_of_different_classes_are_not_the_same():
    @dataclasses.dataclass
    class OtherPoint:
        g: float = 0.0
        h: int = 0

    assert not _same(Point(g=1.0, h=1), OtherPoint(g=1.0, h=1))


@dataclasses.dataclass(eq=False)
class ByIdentity:
    g: float = 0.0


@dataclasses.dataclass(eq=False)
class NeverEqual:
    g: float = 0.0

    def __eq__(self, other: object) -> bool:
        return False

    __hash__ = object.__hash__


class HoldsByIdentity(BaseModel):
    d: ByIdentity = ByIdentity()


class HoldsNeverEqual(BaseModel):
    d: NeverEqual = NeverEqual()


@pytest.mark.parametrize("path", ["client", "wire"])
def test_an_eq_false_dataclass_with_a_finite_value_is_sent(path):
    """Its own `==` is identity, which a copy read back never has; its field values decide."""
    value = HoldsByIdentity(d=ByIdentity(g=1.5))

    assert _sent(path, value) == {"d": {"g": 1.5}}


@pytest.mark.parametrize("path", ["client", "wire"])
def test_a_dataclass_whose_own_eq_calls_equal_values_different_is_sent_by_its_values(path):
    """Field values decide, as for models, not the class's `__eq__`."""
    value = HoldsNeverEqual(d=NeverEqual(g=1.5))

    assert _sent(path, value) == {"d": {"g": 1.5}}
